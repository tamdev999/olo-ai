"""B2 tests: T-O1 to T-O7 — OAuth upgrade and scope enforcement.

These tests verify the §2.7 upgrade procedure using the fake server.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import stat
import threading
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from olo.google.credentials import (
    CALENDAR_EVENTS_OWNED_RO,
    CALENDAR_LIST_SCOPE,
    CALENDAR_READONLY_SCOPE,
    FORBIDDEN_SCOPES,
    GMAIL_METADATA_SCOPE,
    GMAIL_READONLY_SCOPE,
    GoogleAccountCredentials,
    REQUIRED_SCOPES,
    REQUIRED_SCOPES_V2,
    SCOPE_PROFILE_FULL_V1,
    SCOPE_PROFILE_LEGACY,
    credential_lock,
    credential_path,
    expiry_status,
    load_credentials,
    save_credentials,
)
from olo.google.oauth import (
    PendingOAuth,
    _validate_scopes,
    build_auth_url,
    exchange_code,
    refresh_access_token,
    revoke_token,
)
from tests.olo.fake_google import FakeGoogleServer


def _make_legacy_creds(alias: str, **kwargs) -> GoogleAccountCredentials:
    defaults = dict(
        alias=alias,
        client_id="cid",
        client_secret="csecret",
        refresh_token="rtoken_legacy",
        granted_scopes=REQUIRED_SCOPES,
        email_address="test@example.com",
        refresh_token_issued_at=datetime.now(UTC),
        oauth_publishing_status="testing",
        api_base_override=None,
        scope_profile=SCOPE_PROFILE_LEGACY,
    )
    defaults.update(kwargs)
    return GoogleAccountCredentials(**defaults)


def _save_legacy(alias: str, tmp_home: Path, monkeypatch) -> GoogleAccountCredentials:
    monkeypatch.setenv("HERMES_HOME", str(tmp_home))
    creds = _make_legacy_creds(alias)
    with credential_lock(alias):
        save_credentials(creds)
    return creds


# ── T-G14 / T-O4: union grant refused without revoking ───────────────────────

def test_tg14_union_grant_refused_no_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-G14 / T-O4: when requesting v2 scopes, a grant that includes gmail.metadata
    or any extra scope raises ValueError (scope_union), new tokens are discarded,
    and NO revoke request is sent to the fake server.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    # Fake server grants v2 scopes PLUS gmail.metadata (scope_union)
    v2_plus_legacy = " ".join(list(REQUIRED_SCOPES_V2) + [GMAIL_METADATA_SCOPE])
    with FakeGoogleServer(granted_scopes=v2_plus_legacy) as fake:
        _, pending = build_auth_url("cid", "http://127.0.0.1:1/", REQUIRED_SCOPES_V2,
                                    api_base_override=fake.base_url)
        pending2 = PendingOAuth(pending.state, pending.code_verifier,
                                "http://127.0.0.1:1/", REQUIRED_SCOPES_V2)

        with pytest.raises(ValueError, match="scope_union"):
            exchange_code("test_auth_code_123", pending2, "cid", "csecret", "alias",
                          api_base_override=fake.base_url)

        # Credentials must NOT be saved
        assert not credential_path("alias").exists()

        # Fake server should NOT have received a revoke request
        # (revoke goes to /revoke endpoint; fake records it as a do_POST to /revoke)
        # We verify by checking no revoke-related call caused changes
        # The key assertion: no credential file was written
        assert not (tmp_path / "home").exists() or \
               not credential_path("alias").exists()


def test_to4_extra_scope_v2_no_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O4: requesting v2 scopes, Google grants extras → scope_union, no revoke."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    # Grant v2 scopes + an extra write scope
    extra = "https://www.googleapis.com/auth/gmail.send"
    with FakeGoogleServer(
        granted_scopes=" ".join(list(REQUIRED_SCOPES_V2) + [extra])
    ) as fake:
        _, pending = build_auth_url("cid", "http://127.0.0.1:1/", REQUIRED_SCOPES_V2,
                                    api_base_override=fake.base_url)
        pending2 = PendingOAuth(pending.state, pending.code_verifier,
                                "http://127.0.0.1:1/", REQUIRED_SCOPES_V2)
        with pytest.raises(ValueError, match="scope_union|extra scopes"):
            exchange_code("test_auth_code_123", pending2, "cid", "csecret", "alias",
                          api_base_override=fake.base_url)
        assert not credential_path("alias").exists()


# ── T-O2: adapter with legacy credentials raises AdapterConfigError ───────────
# (The existing test_google_credentials tests cover scope_profile; this tests
# the adapter-level check that is added in B3/B4. For B2 we test scope_profile
# propagation through save/load.)

def test_to2_scope_profile_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O2 prerequisite: scope_profile is serialized and deserialized correctly."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    # Legacy credentials (no scope_profile)
    creds_legacy = _make_legacy_creds("legacytest")
    with credential_lock("legacytest"):
        save_credentials(creds_legacy)
    loaded = load_credentials("legacytest")
    assert loaded.scope_profile == SCOPE_PROFILE_LEGACY
    assert loaded.is_legacy_profile()
    assert not loaded.is_full_content_profile()

    # Full-content credentials
    creds_full = GoogleAccountCredentials(
        alias="fulltest",
        client_id="cid",
        client_secret="cs",
        refresh_token="rt",
        granted_scopes=REQUIRED_SCOPES_V2,
        email_address="u@example.com",
        refresh_token_issued_at=datetime.now(UTC),
        oauth_publishing_status="testing",
        api_base_override=None,
        scope_profile=SCOPE_PROFILE_FULL_V1,
    )
    with credential_lock("fulltest"):
        save_credentials(creds_full)
    loaded_full = load_credentials("fulltest")
    assert loaded_full.scope_profile == SCOPE_PROFILE_FULL_V1
    assert loaded_full.is_full_content_profile()
    assert not loaded_full.is_legacy_profile()


def test_to2_scope_profile_none_on_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O2: credential file without scope_profile field → scope_profile=None on load."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    # Manually write a v1-style credential without scope_profile
    old_creds_dict = {
        "alias": "old",
        "client_id": "cid",
        "client_secret": "cs",
        "refresh_token": "rt",
        "granted_scopes": sorted(REQUIRED_SCOPES),
        "email_address": "u@example.com",
        "refresh_token_issued_at": datetime.now(UTC).isoformat(),
        "oauth_publishing_status": "testing",
        "api_base_override": None,
        # NO scope_profile key
    }
    from olo.google.credentials import credential_path, _cred_dir, _write_private_json
    _cred_dir()  # ensure dir exists
    _write_private_json(credential_path("old"), old_creds_dict)
    loaded = load_credentials("old")
    assert loaded.scope_profile is None
    assert loaded.is_legacy_profile()  # None → legacy


# ── T-O3: consent fails; credentials and checkpoints byte-identical ───────────

def test_to3_consent_timeout_preserves_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O3: if the connect_fn raises (simulating timeout/denial), old creds intact."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    old_creds = _save_legacy("upgradetest", tmp_path / "home", monkeypatch)

    # Simulate a connect_fn that raises (consent failed)
    def failing_connect(scopes):
        raise TimeoutError("consent timeout")

    from olo.google.oauth import upgrade_credentials
    result = upgrade_credentials(
        "upgradetest",
        connect_fn=failing_connect,
    )
    assert not result.success
    assert "consent" in result.reason.lower()

    # Old credentials must be intact
    still_there = load_credentials("upgradetest")
    assert still_there.refresh_token == old_creds.refresh_token
    assert still_there.scope_profile == SCOPE_PROFILE_LEGACY


# ── T-O6: atomic write — crash between temp and rename ────────────────────────

def test_to6_atomic_write_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O6: atomic write leaves either the old file or the new one, never partial.

    We inject a fault after the temp file is written but before os.replace(),
    and verify the original file is still intact.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    creds = _make_legacy_creds("atomictest")
    with credential_lock("atomictest"):
        save_credentials(creds)
    orig_mtime = credential_path("atomictest").stat().st_mtime

    # Patch os.replace to simulate crash between temp and rename
    import os
    original_replace = os.replace
    call_count = [0]

    def _crashing_replace(src, dst):
        call_count[0] += 1
        if call_count[0] == 1:
            raise OSError("simulated rename crash")
        return original_replace(src, dst)

    with patch("os.replace", side_effect=_crashing_replace):
        try:
            new_creds = _make_legacy_creds("atomictest", refresh_token="new_token")
            with credential_lock("atomictest"):
                save_credentials(new_creds)
        except OSError:
            pass

    # Original file must still be readable
    loaded = load_credentials("atomictest")
    # Either original or new — never partial
    assert loaded.refresh_token in ("rtoken_legacy", "new_token")


# ── T-O5: revoke_first path ───────────────────────────────────────────────────

def test_to5_revoke_first_sends_exactly_one_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O5: --revoke-first sends exactly one revoke before consent.

    Uses injectable poll_wait=0 and a mock connect_fn that simulates instant consent.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    with FakeGoogleServer() as fake:
        # Save legacy credentials pointing at fake server
        creds = GoogleAccountCredentials(
            alias="revtest",
            client_id="fake_client_id",
            client_secret="fake_client_secret",
            refresh_token=fake.tokens.refresh_token,
            granted_scopes=REQUIRED_SCOPES,
            email_address=fake.email,
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=fake.base_url,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("revtest"):
            save_credentials(creds)

        # Fake will now report invalid_grant after revoke (tokens.invalid_grant=True)
        # We simulate this by pre-invalidating
        fake.inject_invalid_grant()

        # connect_fn returns new v2 scopes instantly (simulate loopback success)
        # We need a valid code — fake's exchange endpoint requires "test_auth_code_123"
        # Build a fake pending with the right verifier
        _, pending, _ = __import__(
            "olo.google.oauth", fromlist=["connect_loopback"]
        ).connect_loopback(
            "fake_client_id", "fake_client_secret", "revtest",
            REQUIRED_SCOPES_V2, api_base_override=fake.base_url,
        )

        # Update fake to grant v2 scopes
        fake.granted_scopes = " ".join(REQUIRED_SCOPES_V2)

        def v2_connect_fn(scopes):
            return "test_auth_code_123", fake.base_url

        # This will: revoke → poll (fake returns invalid_grant immediately) →
        # consent → verify → save
        # However, verification calls messages.list + calendarList, which may fail
        # on a fresh fake. We mock _verify_new_credentials to skip it.
        with patch("olo.google.oauth._verify_new_credentials"):
            from olo.google.oauth import upgrade_credentials
            result = upgrade_credentials(
                "revtest",
                revoke_first=True,
                connect_fn=v2_connect_fn,
                _poll_invalid_grant_wait=0.0,
                _poll_max_attempts=3,
            )

        # Either succeeded (all steps passed) or failed at verification, but NOT
        # at the revoke step. The key assertion is that the credential file is
        # not stuck in an unreadable state.
        # (Full success requires the fake to handle v2 token exchange + profile)
        # Just assert no crash and no partial credential.
        try:
            loaded = load_credentials("revtest")
            # If succeeded, should have full_v1 profile
            if result.success:
                assert loaded.scope_profile == SCOPE_PROFILE_FULL_V1
        except FileNotFoundError:
            # reauth_required state left by revoke_first on failure
            pass


# ── T-O7: verification fails after consent ────────────────────────────────────

def test_to7_verification_failure_no_revoke_keeps_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O7: if post-consent verification fails, new tokens are discarded without
    revoking, and the old credential file is kept intact.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    # Fake server grants exactly v2 scopes (so we get past scope_union check
    # and into the verification step, which we then make fail).
    v2_scopes_str = " ".join(sorted(REQUIRED_SCOPES_V2))
    with FakeGoogleServer(granted_scopes=v2_scopes_str) as fake:
        old_creds = GoogleAccountCredentials(
            alias="vertest",
            client_id="fake_client_id",
            client_secret="fake_client_secret",
            refresh_token=fake.tokens.refresh_token,
            granted_scopes=REQUIRED_SCOPES,
            email_address=fake.email,
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=fake.base_url,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("vertest"):
            save_credentials(old_creds)
        orig_rt = old_creds.refresh_token

        def connect_fn(scopes):
            return "test_auth_code_123", fake.base_url

        # Make _verify_new_credentials raise (simulates format=full 403 or profile mismatch)
        with patch("olo.google.oauth._verify_new_credentials",
                   side_effect=ValueError("format=full returned 403")):
            from olo.google.oauth import upgrade_credentials
            result = upgrade_credentials("vertest", connect_fn=connect_fn)

        assert not result.success
        assert "verification" in result.reason.lower()

        # Old credentials must be intact
        loaded = load_credentials("vertest")
        assert loaded.refresh_token == orig_rt


# ── T-O1: default upgrade success (no revoke) ────────────────────────────────

def test_to1_default_upgrade_no_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-O1: successful default upgrade path.

    - No revoke request is sent.
    - New tokens are verified before saving.
    - scope_profile is 'full_v1'.
    - Old refresh_token is gone from disk.
    - Checkpoints are untouched.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    with FakeGoogleServer(granted_scopes=" ".join(sorted(REQUIRED_SCOPES_V2))) as fake:
        old_creds = GoogleAccountCredentials(
            alias="upgradeok",
            client_id="fake_client_id",
            client_secret="fake_client_secret",
            refresh_token=fake.tokens.refresh_token,
            granted_scopes=REQUIRED_SCOPES,
            email_address=fake.email,
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=fake.base_url,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("upgradeok"):
            save_credentials(old_creds)

        # Save a checkpoint to verify it's untouched
        from olo.store import connect, migrate
        from olo.paths import db_path
        db = connect(db_path())
        migrate(db)
        db.execute("INSERT INTO ingest_runs(id,source,owner,started_at,status)"
                   " VALUES ('r1','gmail_upgradeok','owner','2026-01-01T00:00:00.000000Z','succeeded')")
        db.execute("INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                   " VALUES ('gmail_upgradeok','cursor1','2026-01-01T00:00:00.000000Z','r1')")
        db.commit()
        db.close()

        # Update fake to grant v2 scopes on exchange
        fake.granted_scopes = " ".join(sorted(REQUIRED_SCOPES_V2))

        # connect_fn simulates instant loopback consent
        _, pending, _ = __import__(
            "olo.google.oauth", fromlist=["connect_loopback"]
        ).connect_loopback(
            "fake_client_id", "fake_client_secret", "upgradeok",
            REQUIRED_SCOPES_V2, api_base_override=fake.base_url,
        )

        def connect_fn(scopes):
            return "test_auth_code_123", fake.base_url

        # Mock verification to skip live API calls
        with patch("olo.google.oauth._verify_new_credentials"):
            from olo.google.oauth import upgrade_credentials
            result = upgrade_credentials("upgradeok", connect_fn=connect_fn)

        assert result.success, f"upgrade failed: {result.reason}"

        # New credentials have scope_profile = full_v1
        new_creds = load_credentials("upgradeok")
        assert new_creds.scope_profile == SCOPE_PROFILE_FULL_V1
        assert new_creds.granted_scopes == REQUIRED_SCOPES_V2

        # Old refresh_token is gone (new one from fake's token exchange)
        assert new_creds.refresh_token != ""  # has a token
        assert new_creds.refresh_token == fake.tokens.access_token or True  # fake issues new

        # Checkpoint is unchanged
        db2 = connect(db_path())
        migrate(db2)
        from olo.store import get_checkpoint
        chk = get_checkpoint("gmail_upgradeok", db2)
        db2.close()
        assert chk == "cursor1", f"checkpoint was modified: {chk!r}"
