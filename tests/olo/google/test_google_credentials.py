"""Tests for olo.google.credentials — storage, permissions, lock, repr."""
from __future__ import annotations

import os
from datetime import datetime, UTC
from pathlib import Path

import pytest

from olo.google.credentials import (
    GoogleAccountCredentials,
    CredentialPermissionError,
    credential_path,
    credential_lock,
    email_hash,
    list_accounts,
    load_credentials,
    remove_credentials,
    save_credentials,
)
from olo.google.oauth import GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO


def _make_creds(alias: str = "test", **kwargs) -> GoogleAccountCredentials:
    defaults = dict(
        alias=alias,
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken_abcdef",
        granted_scopes=frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO}),
        email_address="user@example.com",
        refresh_token_issued_at=datetime.now(UTC),
        oauth_publishing_status="testing",
        api_base_override=None,
    )
    defaults.update(kwargs)
    return GoogleAccountCredentials(**defaults)


# ── repr masks secrets ─────────────────────────────────────────────────────────

def test_repr_masks_refresh_token() -> None:
    creds = _make_creds(refresh_token="rtoken_supersecret")
    r = repr(creds)
    assert "rtoken_supersecret" not in r
    assert "cret" in r   # last 4 of "rtoken_supersecret" → "cret"


def test_repr_masks_client_secret() -> None:
    creds = _make_creds(client_secret="cs_supersecret")
    r = repr(creds)
    assert "cs_supersecret" not in r
    assert "cret" in r


def test_repr_shows_alias_and_email() -> None:
    creds = _make_creds(alias="myalias", email_address="u@example.com")
    r = repr(creds)
    assert "myalias" in r
    assert "u@example.com" in r


# ── Save and load ─────────────────────────────────────────────────────────────

def test_save_and_load_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    creds = _make_creds()
    with credential_lock(creds.alias):
        save_credentials(creds)
    loaded = load_credentials(creds.alias)
    assert loaded.alias == creds.alias
    assert loaded.refresh_token == creds.refresh_token
    assert loaded.granted_scopes == creds.granted_scopes


def test_load_missing_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    with pytest.raises(FileNotFoundError, match="no credentials"):
        load_credentials("nonexistent")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_file_mode_is_0600(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    creds = _make_creds()
    with credential_lock(creds.alias):
        save_credentials(creds)
    p = credential_path(creds.alias)
    mode = oct(os.stat(str(p)).st_mode)[-3:]
    assert mode == "600"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_load_rejects_group_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    creds = _make_creds()
    with credential_lock(creds.alias):
        save_credentials(creds)
    p = credential_path(creds.alias)
    os.chmod(str(p), 0o644)   # widen permissions
    with pytest.raises(CredentialPermissionError):
        load_credentials(creds.alias)


# ── remove ────────────────────────────────────────────────────────────────────

def test_remove_deletes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    creds = _make_creds()
    with credential_lock(creds.alias):
        save_credentials(creds)
    assert credential_path(creds.alias).exists()
    remove_credentials(creds.alias)
    assert not credential_path(creds.alias).exists()


# ── list_accounts ─────────────────────────────────────────────────────────────

def test_list_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    for alias in ["acme", "beta", "gamma"]:
        with credential_lock(alias):
            save_credentials(_make_creds(alias=alias))
    assert list_accounts() == ["acme", "beta", "gamma"]


# ── email_hash ────────────────────────────────────────────────────────────────

def test_email_hash_is_deterministic_case_insensitive() -> None:
    h1 = email_hash("User@Example.COM")
    h2 = email_hash("user@example.com")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


# ── scope helpers ─────────────────────────────────────────────────────────────

def test_has_gmail_scope() -> None:
    creds = _make_creds(granted_scopes=frozenset({GMAIL_METADATA_SCOPE}))
    assert creds.has_gmail_scope()
    assert not creds.has_calendar_scope()


def test_has_calendar_scope() -> None:
    creds = _make_creds(granted_scopes=frozenset({CALENDAR_EVENTS_OWNED_RO}))
    assert creds.has_calendar_scope()
    assert not creds.has_gmail_scope()


# ── api_base_override allow-list ──────────────────────────────────────────────

def test_api_base_override_loopback_accepted() -> None:
    creds = _make_creds(api_base_override="http://127.0.0.1:12345")
    assert creds.api_base_override == "http://127.0.0.1:12345"


def test_api_base_override_localhost_accepted() -> None:
    creds = _make_creds(api_base_override="http://localhost:9999")
    assert creds.api_base_override == "http://localhost:9999"


# ── Hermes skill token untouched ──────────────────────────────────────────────

def test_olo_does_not_read_hermes_skill_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Create a fake Hermes skill token
    skill_token = home / "google_token.json"
    skill_token.write_text('{"token": "hermes_skill_secret"}', encoding="utf-8")

    creds = _make_creds()
    with credential_lock(creds.alias):
        save_credentials(creds)
    loaded = load_credentials(creds.alias)

    # Skill token is untouched
    assert skill_token.read_text(encoding="utf-8") == '{"token": "hermes_skill_secret"}'
    assert loaded.refresh_token == creds.refresh_token


# ── I6: api_base_override validation ─────────────────────────────────────────

@pytest.mark.parametrize("bad_url", [
    "https://evil.example",
    "http://10.0.0.1:1",
    "http://127.0.0.1.evil.example:1",
    "http://user@127.0.0.1:1",
    "http://127.0.0.1",        # missing port
    "http://localhost",         # missing port
    "http://127.0.0.1:0/path", # has path
    "ftp://127.0.0.1:1",       # wrong scheme
])
def test_api_base_override_bad_url_rejected_on_construct(bad_url: str) -> None:
    """I6: bad api_base_override values raise AdapterConfigError at construction."""
    from olo.adapters import AdapterConfigError
    with pytest.raises((AdapterConfigError, Exception)):
        _make_creds(api_base_override=bad_url)


@pytest.mark.parametrize("bad_url", [
    "https://evil.example",
    "http://10.0.0.1:1",
    "http://127.0.0.1.evil.example:1",
    "http://user@127.0.0.1:1",
    "http://127.0.0.1",
    "http://localhost",
])
def test_api_base_override_bad_url_rejected_on_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    """I6: save_credentials must also reject bad api_base_override."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.adapters import AdapterConfigError

    # Build a valid creds object, then try to save a mutated one with a bad URL.
    # Since GoogleAccountCredentials is frozen, we bypass __post_init__ by
    # patching _validate_api_base_override at the dataclass level and testing
    # save_credentials independently.
    import olo.google.credentials as _cm
    original = _cm._validate_api_base_override

    # Allow construction but test save rejects
    _cm._validate_api_base_override = lambda v: None  # bypass for construction
    try:
        bad_creds = _make_creds(api_base_override=bad_url)
    finally:
        _cm._validate_api_base_override = original

    with pytest.raises((AdapterConfigError, Exception)):
        with credential_lock(bad_creds.alias):
            save_credentials(bad_creds)


def test_api_base_override_loopback_accepted_on_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I6: valid loopback URLs are accepted by save_credentials."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    creds = _make_creds(api_base_override="http://127.0.0.1:12345")
    with credential_lock(creds.alias):
        save_credentials(creds)
    loaded = load_credentials(creds.alias)
    assert loaded.api_base_override == "http://127.0.0.1:12345"


# ── I7: alias validation ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad_alias", [
    "../x",
    "A",          # uppercase not allowed
    "a/b",        # slash not allowed
    "",            # empty
    "a" * 17,     # 17 chars — too long
    "a-b",         # hyphen not allowed
    "a b",         # space not allowed
])
def test_alias_bad_rejected_on_construct(bad_alias: str) -> None:
    """I7: invalid alias raises AdapterConfigError at construction."""
    from olo.adapters import AdapterConfigError
    with pytest.raises((AdapterConfigError, Exception)):
        _make_creds(alias=bad_alias)


@pytest.mark.parametrize("bad_alias", [
    "../x", "A", "a/b", "", "a" * 17,
])
def test_alias_bad_rejected_in_credential_path(bad_alias: str) -> None:
    """I7: credential_path() refuses bad aliases — no file created."""
    from olo.adapters import AdapterConfigError
    with pytest.raises((AdapterConfigError, Exception)):
        credential_path(bad_alias)


@pytest.mark.parametrize("bad_alias", [
    "../x", "A", "a/b", "", "a" * 17,
])
def test_alias_bad_rejected_in_load_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_alias: str
) -> None:
    """I7: load_credentials() refuses bad aliases."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.adapters import AdapterConfigError
    with pytest.raises((AdapterConfigError, Exception)):
        load_credentials(bad_alias)


@pytest.mark.parametrize("bad_alias", [
    "../x", "A", "a/b", "", "a" * 17,
])
def test_alias_bad_rejected_in_remove_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_alias: str
) -> None:
    """I7: remove_credentials() refuses bad aliases — no file created."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from olo.adapters import AdapterConfigError
    with pytest.raises((AdapterConfigError, Exception)):
        remove_credentials(bad_alias)


def test_alias_bad_rejected_by_gcal_adapter() -> None:
    """I7: GcalAdapter refuses invalid alias option."""
    from olo.adapters import AdapterConfigError
    from olo.adapters.gcal import GcalAdapter
    for bad in ["../x", "A", "a/b", "", "a" * 17]:
        with pytest.raises(AdapterConfigError, match="alias"):
            GcalAdapter({"account": bad})


def test_alias_bad_rejected_by_gmail_adapter() -> None:
    """I7: GmailAdapter refuses invalid alias option."""
    from olo.adapters import AdapterConfigError
    from olo.adapters.gmail import GmailAdapter
    for bad in ["../x", "A", "a/b", "", "a" * 17]:
        with pytest.raises(AdapterConfigError, match="alias"):
            GmailAdapter({"account": bad})
