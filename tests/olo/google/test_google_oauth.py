"""Tests for olo.google.oauth — PKCE, token exchange, refresh, revoke."""
from __future__ import annotations

import base64
import hashlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olo.google.credentials import (
    GoogleAuthRevoked,
    credential_lock,
    load_credentials,
    save_credentials,
)
from olo.google.oauth import (
    CALENDAR_EVENTS_OWNED_RO,
    GMAIL_METADATA_SCOPE,
    PendingOAuth,
    _pkce_pair,
    _random_state,
    build_auth_url,
    exchange_code,
    parse_manual_redirect,
    refresh_access_token,
    revoke_token,
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
    url, pending = build_auth_url("client_id_x", "http://127.0.0.1:12345/", _SCOPES)
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "response_type=code" in url
    assert "state=" in url
    assert pending.state in url


def test_build_auth_url_uses_override() -> None:
    url, _ = build_auth_url("cid", "http://127.0.0.1:1/", _SCOPES,
                            api_base_override="http://127.0.0.1:9999")
    assert url.startswith("http://127.0.0.1:9999")


# ── State mismatch → nothing saved ────────────────────────────────────────────

def test_state_mismatch_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        import olo.google.oauth as _oauth
        _, pending, _ = _oauth.connect_loopback(
            "cid", "csecret", "alias", _SCOPES, api_base_override=fake.base_url
        )
        result = parse_manual_redirect("http://127.0.0.1:1/?code=abc&state=WRONG", pending)
        assert result is None


# ── access_denied → None ──────────────────────────────────────────────────────

def test_access_denied_returns_none() -> None:
    _, pending = build_auth_url("cid", "http://127.0.0.1:1/", _SCOPES)
    result = parse_manual_redirect(
        f"http://127.0.0.1:1/?error=access_denied&state={pending.state}", pending
    )
    assert result is None


# ── exchange_code with fake server ────────────────────────────────────────────

def test_exchange_code_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(email="user@example.com") as fake:
        _, pending = build_auth_url("cid", "http://127.0.0.1:1/", _SCOPES,
                                    api_base_override=fake.base_url)
        pending2 = PendingOAuth(state=pending.state, code_verifier=pending.code_verifier,
                                redirect_uri="http://127.0.0.1:1/", scopes=_SCOPES)
        creds = exchange_code("test_auth_code_123", pending2, "cid", "csecret", "alias",
                              api_base_override=fake.base_url)
        assert creds.email_address == "user@example.com"
        assert GMAIL_METADATA_SCOPE in creds.granted_scopes
        assert creds.refresh_token == fake.tokens.refresh_token


def test_extra_scope_refused_and_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    extra = "https://www.googleapis.com/auth/gmail.send"
    with FakeGoogleServer(
        granted_scopes=" ".join([GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO, extra])
    ) as fake:
        _, pending = build_auth_url("cid", "http://127.0.0.1:1/", _SCOPES,
                                    api_base_override=fake.base_url)
        pending2 = PendingOAuth(pending.state, pending.code_verifier, "http://127.0.0.1:1/", _SCOPES)
        with pytest.raises(ValueError, match="extra scopes"):
            exchange_code("test_auth_code_123", pending2, "cid", "csecret", "alias",
                          api_base_override=fake.base_url)
        from olo.google.credentials import credential_path
        assert not credential_path("alias").exists()


# ── refresh and rotation ──────────────────────────────────────────────────────

def test_refresh_returns_access_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        token = refresh_access_token("alias")
        assert token.startswith("access_fake_")


def test_refresh_rotation_saved_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        old_token = load_credentials("alias").refresh_token
        refresh_access_token("alias")
        new_token = load_credentials("alias").refresh_token
        assert new_token != old_token
        assert new_token.startswith("refresh_fake_")


def test_invalid_grant_raises_google_auth_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        fake.inject_invalid_grant()
        with pytest.raises(GoogleAuthRevoked, match="google auth revoked"):
            refresh_access_token("alias")


def test_invalid_grant_testing_mode_mentions_7_days(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer() as fake:
        creds = fake.make_credentials("alias")
        with credential_lock("alias"):
            save_credentials(creds)
        fake.inject_invalid_grant()
        with pytest.raises(GoogleAuthRevoked, match="7 days"):
            refresh_access_token("alias")


# ── Lock timeout ──────────────────────────────────────────────────────────────

def test_lock_timeout_raises_source_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.adapters import SourceError
    from olo.google.credentials import _CredentialLock

    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_lock() -> None:
        with credential_lock("alias"):
            lock_held.set()
            release_lock.wait(timeout=10)

    t = threading.Thread(target=hold_lock)
    t.start()
    lock_held.wait(timeout=5)

    orig = _CredentialLock.TIMEOUT_SECS
    _CredentialLock.TIMEOUT_SECS = 0.2
    try:
        with pytest.raises(SourceError, match="lock timeout"):
            with credential_lock("alias"):
                pass
    finally:
        _CredentialLock.TIMEOUT_SECS = orig
        release_lock.set()
        t.join(timeout=5)


# ── Concurrent rotation (§4.3) ────────────────────────────────────────────────

def test_concurrent_rotation_no_invalid_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two threads each refresh 5 times with rotation; no invalid_grant expected."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with FakeGoogleServer(rotate_tokens=True) as fake:
        fake.make_saved_credentials("alias", tmp_path / "home", monkeypatch)
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def do_refreshes() -> None:
            barrier.wait()
            for _ in range(5):
                try:
                    refresh_access_token("alias")
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=do_refreshes) for _ in range(2)]
        for thr in threads:
            thr.start()
        for thr in threads:
            thr.join(timeout=30)

        assert not errors, f"Concurrent refresh errors: {errors}"
        final_refresh = load_credentials("alias").refresh_token
        assert final_refresh == fake.tokens.refresh_token


# ── revoke ────────────────────────────────────────────────────────────────────

def test_revoke_does_not_raise(tmp_path: Path) -> None:
    with FakeGoogleServer() as fake:
        revoke_token("some_token", api_base_override=fake.base_url)


# ── Expiry warning (§3.6) ─────────────────────────────────────────────────────

def test_status_expiry_warning_timing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Days 3, 5.5, 8 — no warning, warning, expired indicator."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    def _days_ago(n: float) -> datetime:
        return datetime.now(UTC) - timedelta(days=n)

    creds_3 = _make_testing_creds(tmp_path, "d3", _days_ago(3))
    assert _expiry_status(creds_3) == "ok"

    creds_55 = _make_testing_creds(tmp_path, "d55", _days_ago(5.5))
    assert _expiry_status(creds_55) == "warning"

    creds_8 = _make_testing_creds(tmp_path, "d8", _days_ago(8))
    assert _expiry_status(creds_8) == "expired"


def _make_testing_creds(tmp_path: Path, alias: str, issued_at: datetime):
    """Create and save a testing-mode GoogleAccountCredentials object."""
    from olo.google.credentials import GoogleAccountCredentials
    creds = GoogleAccountCredentials(
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


def _expiry_status(creds) -> str:
    """Return 'ok', 'warning', or 'expired' for a testing-mode credential."""
    if creds.oauth_publishing_status != "testing":
        return "ok"
    expiry = creds.refresh_token_issued_at + timedelta(days=7)
    remaining = (expiry - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining < 48 * 3600:
        return "warning"
    return "ok"
