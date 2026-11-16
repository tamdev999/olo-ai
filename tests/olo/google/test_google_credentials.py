"""Tests for olo.google.credentials — storage, permissions, lock, repr."""
from __future__ import annotations

import json
import os
import stat
from datetime import datetime, UTC, timedelta
from pathlib import Path

import pytest

from olo.google.credentials import (
    GoogleAccountCredentials,
    GoogleAuthRevoked,
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
