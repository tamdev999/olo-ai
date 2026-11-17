"""Tests for olo.google.http — error mapping, 401 retry, redaction, allow-list."""
from __future__ import annotations

import threading
import urllib.request
from pathlib import Path

import pytest

from olo.google.http import (
    GoogleClient,
    GoogleRateLimited,
    GmailHistoryExpired,
    CalendarSyncTokenExpired,
    CalendarPageTokenInvalid,
    _assert_allowed,
)
from olo.google.credentials import GoogleAuthRevoked
from olo.adapters import SourceError
from tests.olo.fake_google import FakeGoogleServer


# ── §4.4 allow-list ───────────────────────────────────────────────────────────

def test_allowed_calls_pass() -> None:
    for method, url in [
        ("GET", "http://x/gmail/v1/users/me/profile"),
        ("GET", "http://x/gmail/v1/users/me/history"),
        ("GET", "http://x/gmail/v1/users/me/messages/abc123"),
        ("GET", "http://x/calendar/v3/calendars/primary/events"),
        ("POST", "http://x/token"),
        ("POST", "http://x/revoke"),
    ]:
        _assert_allowed(method, url)   # no exception


def test_disallowed_calls_raise() -> None:
    with pytest.raises(AssertionError):
        _assert_allowed("POST", "http://x/gmail/v1/users/me/messages/send")
    with pytest.raises(AssertionError):
        _assert_allowed("DELETE", "http://x/calendar/v3/calendars/primary/events/123")
    with pytest.raises(AssertionError):
        _assert_allowed("GET", "http://x/drive/v3/files")
    with pytest.raises(AssertionError):
        _assert_allowed("PATCH", "http://x/gmail/v1/users/me/messages/123/modify")


# ── 401 refresh-retry-once ────────────────────────────────────────────────────

def test_401_triggers_refresh_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        fake.inject_401()
        # First call → 401 → refresh → retry succeeds
        profile = client.get_json("gmail", "/users/me/profile", [])
        assert "emailAddress" in profile


def test_double_401_raises_auth_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        # Inject invalid_grant so refresh fails → second 401 on retry
        fake.inject_invalid_grant()
        with pytest.raises((GoogleAuthRevoked, SourceError)):
            client.get_json("gmail", "/users/me/profile", [])


# ── Error mapping §4.2 ────────────────────────────────────────────────────────

def test_429_raises_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        fake.inject_429()
        with pytest.raises(GoogleRateLimited):
            client.get_json("gmail", "/users/me/profile", [])


def test_history_404_raises_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        fake.set_min_history_id("9999999")
        client = GoogleClient(fake.make_credentials("alias"))
        with pytest.raises(GmailHistoryExpired):
            client.get_json("gmail", "/users/me/history",
                            [("startHistoryId", "1000")])


def test_500_raises_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        fake.inject_500()
        with pytest.raises(SourceError, match="google http 500"):
            client.get_json("gmail", "/users/me/profile", [])


def test_malformed_raises_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        fake.inject_malformed()
        with pytest.raises(SourceError, match="malformed"):
            client.get_json("gmail", "/users/me/profile", [])


# ── Redaction ──────────────────────────────────────────────────────────────────

def test_error_message_does_not_contain_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the fake echoes back the token, our error message must redact it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        fake.inject_500()
        try:
            client.get_json("gmail", "/users/me/profile", [])
        except SourceError as exc:
            msg = str(exc)
            # Must not contain "ya29." or the specific fake access token
            assert "ya29." not in msg
            assert "access_fake" not in msg


# ── get_message_or_none ────────────────────────────────────────────────────────

def test_get_message_or_none_returns_none_for_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        result = client.get_message_or_none(
            "nonexistent_msg_id",
            ["Subject", "From", "To", "Date", "Message-ID"],
        )
        assert result is None


def test_get_message_returns_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.gmail.add_message("msg1", subject="Hello World")
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        client = GoogleClient(fake.make_credentials("alias"))
        result = client.get_message_or_none(
            "msg1",
            ["Subject", "From", "To", "Date", "Message-ID", "In-Reply-To"],
        )
        assert result is not None
        assert result["id"] == "msg1"


# ── Cross-origin redirect drops Authorization ─────────────────────────────────

def test_cross_origin_redirect_drops_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redirect to a different origin must not forward Authorization."""
    import http.server
    import threading

    received_headers: dict = {}
    done = threading.Event()

    class _SecondServer(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_headers.update(dict(self.headers))
            done.set()
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_) -> None:
            pass

    second = http.server.HTTPServer(("127.0.0.1", 0), _SecondServer)
    second_port = second.server_address[1]
    t = threading.Thread(target=second.serve_forever, daemon=True)
    t.start()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        # First server redirects to second (cross-origin)
        fake.inject_cross_origin_redirect(f"http://127.0.0.1:{second_port}/")
        client = GoogleClient(fake.make_credentials("alias"))
        try:
            client.get_json("gmail", "/users/me/profile", [])
        except Exception:
            pass

    done.wait(timeout=3)
    second.shutdown()
    # Authorization header must NOT have been forwarded to the second server
    assert "Authorization" not in received_headers
