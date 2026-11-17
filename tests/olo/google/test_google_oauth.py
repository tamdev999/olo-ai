"""Tests for olo.google.oauth — PKCE, token exchange, refresh, revoke.

Covers: C2, I9, Minor regression tests.
"""
from __future__ import annotations

import base64
import hashlib
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olo.google.oauth import (
    GMAIL_METADATA_SCOPE,
    CALENDAR_EVENTS_OWNED_RO,
    build_auth_url,
    exchange_code,
    parse_manual_redirect,
    PendingOAuth,
    refresh_access_token,
    revoke_token,
    _pkce_pair,
    _random_state,
)
from olo.google.credentials import (
    GoogleAccountCredentials,
    GoogleAuthRevoked,
    credential_lock,
    load_credentials,
    save_credentials,
)
from tests.olo.fake_google import FakeGoogleServer

_SCOPES = frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO})


# ── PKCE correctness ──────────────────────────────────────────────────────────

def test_pkce_s256_challenge_matches_verifier() -> None:
    verifier, challenge = _pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    assert challenge == expected


def test_pkce_verifier_length_in_range() -> None:
    for _ in range(10):
        verifier, _ = _pkce_pair()
        assert 43 <= len(verifier) <= 128


def test_state_has_sufficient_entropy() -> None:
    states = {_random_state() for _ in range(50)}
    assert len(states) == 50


# ── build_auth_url ────────────────────────────────────────────────────────────

def test_build_auth_url_contains_required_params() -> None:
    url, pending = build_auth_url(
        "client_id_x",
        "http://127.0.0.1:12345/",
        _SCOPES,
    )
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "response_type=code" in url
    assert "state=" in url
    assert pending.state in url


def test_build_auth_url_uses_override() -> None:
    url, _ = build_auth_url(
        "cid", "http://127.0.0.1:1/", _SCOPES,
        api_base_override="http://127.0.0.1:9999",
    )
    assert url.startswith("http://127.0.0.1:9999")


# ── State mismatch → nothing saved ────────────────────────────────────────────

def test_state_mismatch_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        from olo.google.oauth import connect_loopback
        _, pending, _ = connect_loopback(
            "cid", "csecret", "alias", _SCOPES, api_base_override=fake.base_url
        )
        result = parse_manual_redirect(
            f"http://127.0.0.1:1/?code=abc&state=WRONG",
            pending,
        )
        assert result is None


# ── access_denied → None ──────────────────────────────────────────────────────

def test_access_denied_returns_none() -> None:
    _, pending = build_auth_url("cid", "http://127.0.0.1:1/", _SCOPES)
    result = parse_manual_redirect(
        f"http://127.0.0.1:1/?error=access_denied&state={pending.state}",
        pending,
    )
    assert result is None


# ── exchange_code with fake server ────────────────────────────────────────────

def test_exchange_code_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(email="user@example.com") as fake:
        _, pending = build_auth_url(
            "cid", "http://127.0.0.1:1/", _SCOPES,
            api_base_override=fake.base_url,
        )
        pending2 = PendingOAuth(
            state=pending.state,
            code_verifier=pending.code_verifier,
            redirect_uri="http://127.0.0.1:1/",
            scopes=_SCOPES,
        )
        creds = exchange_code(
            "test_auth_code_123", pending2,
            "cid", "csecret", "alias",
            api_base_override=fake.base_url,
        )
        assert creds.email_address == "user@example.com"
        assert GMAIL_METADATA_SCOPE in creds.granted_scopes
        assert creds.refresh_token == fake.tokens.refresh_token


def test_extra_scope_refused_and_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    extra_scope = "https://www.googleapis.com/auth/gmail.send"
    with FakeGoogleServer(
        granted_scopes=" ".join([GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO, extra_scope])
    ) as fake:
        _, pending = build_auth_url("cid", "http://127.0.0.1:1/", _SCOPES,
                                    api_base_override=fake.base_url)
        pending2 = PendingOAuth(pending.state, pending.code_verifier,
                                "http://127.0.0.1:1/", _SCOPES)
        with pytest.raises(ValueError, match="extra scopes"):
            exchange_code("test_auth_code_123", pending2, "cid", "csecret", "alias",
                          api_base_override=fake.base_url)
        try:
            from olo.google.credentials import credential_path
            assert not credential_path("alias").exists()
        except FileNotFoundError:
            pass


# ── refresh and rotation ───────────────────────────────────────────────────────

def test_refresh_returns_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        creds = fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        token = refresh_access_token("alias")
        assert token.startswith("access_fake_")


def test_refresh_rotation_saved_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        old_token = load_credentials("alias").refresh_token
        refresh_access_token("alias")
        new_token = load_credentials("alias").refresh_token
        assert new_token != old_token
        assert new_token.startswith("refresh_fake_")


def test_invalid_grant_raises_google_auth_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        fake.inject_invalid_grant()
        with pytest.raises(GoogleAuthRevoked, match="google auth revoked"):
            refresh_access_token("alias")


def test_invalid_grant_testing_mode_mentions_7_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        creds = fake.make_credentials("alias")
        with credential_lock("alias"):
            save_credentials(creds)
        fake.inject_invalid_grant()
        with pytest.raises(GoogleAuthRevoked, match="7 days"):
            refresh_access_token("alias")


# ── Lock timeout ──────────────────────────────────────────────────────────────

def test_lock_timeout_raises_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.google.credentials import _CredentialLock, credential_lock
    from olo.adapters import SourceError

    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        with credential_lock("alias"):
            lock_held.set()
            release_lock.wait(timeout=10)

    t = threading.Thread(target=hold_lock)
    t.start()
    lock_held.wait(timeout=5)

    orig_timeout = _CredentialLock.TIMEOUT_SECS
    _CredentialLock.TIMEOUT_SECS = 0.2
    try:
        with pytest.raises(SourceError, match="lock timeout"):
            with credential_lock("alias"):
                pass
    finally:
        _CredentialLock.TIMEOUT_SECS = orig_timeout
        release_lock.set()
        t.join(timeout=5)


# ── Concurrent rotation (§4.3 regression) ────────────────────────────────────

def test_concurrent_rotation_no_invalid_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads each refresh 5 times with token rotation; no invalid_grant expected."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def do_refreshes():
            barrier.wait()
            for _ in range(5):
                try:
                    refresh_access_token("alias")
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=do_refreshes) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent refresh errors: {errors}"
        final_refresh = load_credentials("alias").refresh_token
        assert final_refresh == fake.tokens.refresh_token


# ── I9: subprocess rotation test ─────────────────────────────────────────────

def test_concurrent_rotation_subprocess_no_invalid_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I9: two separate processes refresh concurrently; no invalid_grant errors.

    Uses a rotating FakeGoogleServer running in the parent process.
    Each subprocess calls refresh_access_token() five times.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        base_url = fake.base_url

        # Write a small helper script each subprocess will execute
        script = tmp_path / "do_refresh.py"
        script.write_text(
            f"""\
import os, sys
os.environ["HERMES_HOME"] = {str(tmp_path / "home")!r}
sys.path.insert(0, {str(Path(__file__).parent.parent.parent.parent)!r})
from olo.google.oauth import refresh_access_token
errors = []
for _ in range(5):
    try:
        refresh_access_token("alias")
    except Exception as exc:
        errors.append(str(exc))
if errors:
    sys.exit("\\n".join(errors))
""",
            encoding="utf-8",
        )

        procs = [
            subprocess.Popen(
                [sys.executable, str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [p.communicate(timeout=30) for p in procs]
        for i, (stdout, stderr) in enumerate(results):
            assert procs[i].returncode == 0, (
                f"subprocess {i} failed: {stderr.decode()}"
            )


def test_concurrent_rotation_negative_control_lock_bypassed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I9 negative control: without the lock, concurrent rotation produces invalid_grant.

    credential_lock is monkeypatched to a no-op so two threads race.
    At least one refresh must fail with invalid_grant / GoogleAuthRevoked.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    import contextlib

    @contextlib.contextmanager
    def _noop_lock(_alias: str):
        yield

    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)

        # Patch credential_lock to a no-op in the oauth module
        monkeypatch.setattr("olo.google.oauth.credential_lock", _noop_lock)

        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def do_refreshes():
            barrier.wait()
            for _ in range(10):
                try:
                    refresh_access_token("alias")
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=do_refreshes) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # Without the lock, we expect at least one error (invalid_grant race)
        assert errors, (
            "negative control failed: no errors with lock bypassed — "
            "test cannot prove the lock is necessary"
        )


# ── revoke ─────────────────────────────────────────────────────────────────────

def test_revoke_does_not_raise(tmp_path: Path) -> None:
    with FakeGoogleServer() as fake:
        revoke_token("some_token", api_base_override=fake.base_url)


# ── C2: redirect does not forward Authorization ───────────────────────────────

def test_profile_redirect_does_not_forward_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2 regression: Authorization header must NOT reach the redirect target.

    Server A (fake) 302-redirects the profile request to Server B.
    Server B records all received headers.
    Assert: A got the Authorization header; B did not.
    """
    import http.server

    received_by_b: dict = {}

    class _RecordHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_by_b["auth"] = self.headers.get("Authorization", "")
            received_by_b["path"] = self.path
            body = b'{"emailAddress":"a@b.com"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_) -> None:
            pass

    server_b = http.server.HTTPServer(("127.0.0.1", 0), _RecordHandler)
    port_b = server_b.server_address[1]
    t_b = threading.Thread(target=server_b.serve_forever, daemon=True)
    t_b.start()

    received_by_a: dict = {}

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_by_a["auth"] = self.headers.get("Authorization", "")
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port_b}/profile")
            self.end_headers()

        def log_message(self, *_) -> None:
            pass

    server_a = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port_a = server_a.server_address[1]
    t_a = threading.Thread(target=server_a.serve_forever, daemon=True)
    t_a.start()

    # Wait until both servers are accepting connections (safe with serve_forever)
    import socket as _sock
    import time as _time
    for _port in (port_a, port_b):
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            try:
                s = _sock.create_connection(("127.0.0.1", _port), timeout=0.1)
                s.close()
                break
            except OSError:
                _time.sleep(0.005)

    from olo.google.oauth import _get_profile
    try:
        _get_profile("test_bearer_token", api_base_override=f"http://127.0.0.1:{port_a}")
    except Exception:
        pass  # We only care about headers, not the response

    t_a.join(timeout=5)
    t_b.join(timeout=5)
    server_a.shutdown()
    server_b.shutdown()
    server_a.server_close()
    server_b.server_close()

    assert "test_bearer_token" in received_by_a.get("auth", ""), (
        "Server A must have received the Authorization header"
    )
    assert received_by_b.get("auth", "") == "", (
        f"C2 FAIL: Authorization was forwarded to redirect target: "
        f"{received_by_b.get('auth')}"
    )


def test_no_urlopen_in_olo_source() -> None:
    """C2: grep -rn urlopen olo/ must return nothing."""
    import ast

    olo_root = Path(__file__).parent.parent.parent.parent / "olo"
    violations = []
    for py_file in olo_root.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8", errors="replace")
        # Look for direct urlopen calls (not inside strings/comments)
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # urllib.request.urlopen(...)
                if isinstance(func, ast.Attribute) and func.attr == "urlopen":
                    violations.append(f"{py_file}:{node.lineno}")
                # urlopen(...) direct
                if isinstance(func, ast.Name) and func.id == "urlopen":
                    violations.append(f"{py_file}:{node.lineno}")
    assert not violations, (
        "C2: direct urlopen calls found in olo/ source — "
        "all HTTP must go through open_credentialed_url:\n"
        + "\n".join(violations)
    )


# ── Minor: assert replaced by explicit check ──────────────────────────────────

def test_refresh_explicit_check_not_assert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Minor: the under-lock token check raises SourceError, not AssertionError.

    Simulate a concurrent write by patching load_credentials to return a stale
    token on the second call (the re-read inside the lock).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.adapters import SourceError
    import olo.google.oauth as _oauth_mod
    import olo.google.credentials as _creds_mod

    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)

        original_load = _creds_mod.load_credentials
        call_count = [0]

        def _patched_load(alias: str):
            call_count[0] += 1
            creds = original_load(alias)
            if call_count[0] >= 2:
                # Return a creds object whose refresh_token doesn't match
                # the one that was loaded at the start of refresh_access_token
                from olo.google.credentials import GoogleAccountCredentials
                return GoogleAccountCredentials(
                    alias=creds.alias,
                    client_id=creds.client_id,
                    client_secret=creds.client_secret,
                    refresh_token="DIFFERENT_TOKEN_SIMULATING_CONCURRENT_WRITE",
                    granted_scopes=creds.granted_scopes,
                    email_address=creds.email_address,
                    refresh_token_issued_at=creds.refresh_token_issued_at,
                    oauth_publishing_status=creds.oauth_publishing_status,
                    api_base_override=creds.api_base_override,
                )
            return creds

        monkeypatch.setattr(_creds_mod, "load_credentials", _patched_load)
        monkeypatch.setattr(_oauth_mod, "load_credentials", _patched_load)

        with pytest.raises(SourceError, match="concurrent write"):
            refresh_access_token("alias")


# ── I9: expiry_status() from production module ────────────────────────────────

def _make_creds_with_issued(alias: str, issued_at: datetime, monkeypatch) -> GoogleAccountCredentials:
    """Create and save credentials with the given refresh_token_issued_at."""
    from olo.google.credentials import GoogleAccountCredentials as _GAC
    creds = _GAC(
        alias=alias,
        client_id="cid",
        client_secret="cs",
        refresh_token="rt",
        granted_scopes=frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO}),
        email_address="u@example.com",
        refresh_token_issued_at=issued_at,
        oauth_publishing_status="testing",
        api_base_override=None,
    )
    with credential_lock(alias):
        save_credentials(creds)
    return creds


def test_status_expiry_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Day 3 (< 5 days elapsed) → 'ok'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.google.credentials import expiry_status
    now = datetime.now(UTC)
    issued_3_days_ago = now - timedelta(days=3)
    creds = _make_creds_with_issued("d3", issued_3_days_ago, monkeypatch)
    assert expiry_status(creds, now=now) == "ok"


def test_status_expiry_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Day 5.5 (< 48 h remaining) → 'warning'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.google.credentials import expiry_status
    now = datetime.now(UTC)
    issued_55_days_ago = now - timedelta(days=5.5)
    creds = _make_creds_with_issued("d55", issued_55_days_ago, monkeypatch)
    assert expiry_status(creds, now=now) == "warning"


def test_status_expiry_expired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Day 8 (past 7-day limit) → 'expired'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.google.credentials import expiry_status
    now = datetime.now(UTC)
    issued_8_days_ago = now - timedelta(days=8)
    creds = _make_creds_with_issued("d8", issued_8_days_ago, monkeypatch)
    assert expiry_status(creds, now=now) == "expired"


def test_status_expiry_non_testing_always_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production (non-testing) publishing status → always 'ok'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.google.credentials import expiry_status, GoogleAccountCredentials
    now = datetime.now(UTC)
    creds = GoogleAccountCredentials(
        alias="prod",
        client_id="cid",
        client_secret="cs",
        refresh_token="rt",
        granted_scopes=frozenset({GMAIL_METADATA_SCOPE}),
        email_address="u@example.com",
        refresh_token_issued_at=now - timedelta(days=30),
        oauth_publishing_status="production",
        api_base_override=None,
    )
    assert expiry_status(creds, now=now) == "ok"


def test_status_expiry_warning_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """I9: calls the PRODUCTION expiry_status() with a fixed *now* (not the test helper)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.google.credentials import expiry_status  # production function

    def _days_ago(n: float) -> datetime:
        return datetime.now(UTC) - timedelta(days=n)

    now = datetime.now(UTC)

    creds_3 = _make_creds_with_issued("d3b", _days_ago(3), monkeypatch)
    assert expiry_status(creds_3, now=now) == "ok"

    creds_55 = _make_creds_with_issued("d55b", _days_ago(5.5), monkeypatch)
    assert expiry_status(creds_55, now=now) == "warning"

    creds_8 = _make_creds_with_issued("d8b", _days_ago(8), monkeypatch)
    assert expiry_status(creds_8, now=now) == "expired"


# ── C2: POST redirect tests ───────────────────────────────────────────────────

def _make_recording_server() -> tuple:
    """Return (server, port, received_dict) for a server that records one request."""
    import http.server
    received: dict = {}

    class _RecordHandler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            received["method"] = self.command
            received["auth"] = self.headers.get("Authorization", "")
            received["content_type"] = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = self.rfile.read(length) if length else b""
            body = b'{"access_token":"at","token_type":"Bearer","expires_in":3600}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802
            self._handle()

        def do_GET(self) -> None:  # noqa: N802
            self._handle()

        def log_message(self, *_) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _RecordHandler)
    port = server.server_address[1]
    return server, port, received


def test_token_exchange_redirect_does_not_forward_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: token exchange POST that gets a 302 must not forward the body or
    Authorization header to the redirect target.

    Server A redirects to server B. Assert B receives neither the
    Authorization header nor the client_secret in the body.
    """
    import http.server
    import threading

    received_by_b: dict = {}

    class _RedirectTarget(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received_by_b["auth"] = self.headers.get("Authorization", "")
            received_by_b["content_type"] = self.headers.get("Content-Type", "")
            length = int(self.headers.get("Content-Length", 0))
            received_by_b["body"] = self.rfile.read(length) if length else b""
            # Return a valid token response
            body = (
                b'{"access_token":"at2","refresh_token":"rt2",'
                b'"token_type":"Bearer","expires_in":3600,'
                b'"scope":"https://www.googleapis.com/auth/gmail.metadata"}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            self.do_POST()

        def log_message(self, *_) -> None:
            pass

    server_b = http.server.HTTPServer(("127.0.0.1", 0), _RedirectTarget)
    port_b = server_b.server_address[1]
    t_b = threading.Thread(target=server_b.serve_forever, daemon=True)
    t_b.start()

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            # Drain body before redirecting
            int(self.headers.get("Content-Length", 0))
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length) if length else b""
            self.send_response(302)
            self.send_header("Location",
                             f"http://127.0.0.1:{port_b}/token")
            self.end_headers()

        def log_message(self, *_) -> None:
            pass

    server_a = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port_a = server_a.server_address[1]
    t_a = threading.Thread(target=server_a.serve_forever, daemon=True)
    t_a.start()

    from olo.google.oauth import _post_token
    # Attempt a token exchange that will hit server A (which 302s to B)
    # The call may raise or return an error — we only care that B doesn't
    # receive credentials
    try:
        _post_token(
            {
                "grant_type": "authorization_code",
                "code": "testcode",
                "client_secret": "SUPER_SECRET_VALUE",
                "client_id": "cid",
                "redirect_uri": "http://127.0.0.1:1/",
                "code_verifier": "v",
            },
            api_base_override=f"http://127.0.0.1:{port_a}",
        )
    except Exception:
        pass

    server_a.shutdown()
    server_b.shutdown()
    t_a.join(timeout=5)
    t_b.join(timeout=5)
    server_a.server_close()
    server_b.server_close()

    # B must not have received the client_secret or an Authorization header
    body_b = received_by_b.get("body", b"")
    assert b"SUPER_SECRET_VALUE" not in body_b, (
        "C2: client_secret must not reach redirect target"
    )
    assert received_by_b.get("auth", "") == "", (
        "C2: Authorization must not reach redirect target"
    )


def test_revoke_redirect_does_not_forward_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2: revoke POST that gets a 302 must not forward the token to the redirect target."""
    import http.server
    import threading

    received_by_b: dict = {}
    TOKEN_VALUE = "SECRET_TOKEN_12345"

    class _RecordTarget(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received_by_b["auth"] = self.headers.get("Authorization", "")
            length = int(self.headers.get("Content-Length", 0))
            received_by_b["body"] = self.rfile.read(length) if length else b""
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self.do_POST()

        def log_message(self, *_) -> None:
            pass

    server_b = http.server.HTTPServer(("127.0.0.1", 0), _RecordTarget)
    port_b = server_b.server_address[1]
    t_b = threading.Thread(target=server_b.serve_forever, daemon=True)
    t_b.start()

    class _RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length) if length else b""
            self.send_response(302)
            self.send_header("Location",
                             f"http://127.0.0.1:{port_b}/revoke")
            self.end_headers()

        def log_message(self, *_) -> None:
            pass

    server_a = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)
    port_a = server_a.server_address[1]
    t_a = threading.Thread(target=server_a.serve_forever, daemon=True)
    t_a.start()

    from olo.google.oauth import revoke_token
    revoke_token(TOKEN_VALUE, api_base_override=f"http://127.0.0.1:{port_a}")

    server_a.shutdown()
    server_b.shutdown()
    t_a.join(timeout=5)
    t_b.join(timeout=5)
    server_a.server_close()
    server_b.server_close()

    body_b = received_by_b.get("body", b"")
    assert TOKEN_VALUE.encode() not in body_b, (
        "C2: revoke token value must not reach redirect target"
    )
    assert received_by_b.get("auth", "") == "", (
        "C2: Authorization must not reach redirect target on revoke redirect"
    )
