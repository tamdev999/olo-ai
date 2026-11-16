"""Tests for olo CLI — setup (M1 + M2), google connect/status/disconnect,
checkpoint reset, isolation, printed-schedule parse. §8.6, §9.5."""
from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest

from olo.hermes_plugin.cli import (
    _cmd_checkpoint_reset,
    _cmd_setup,
    _cmd_status,
    _GCAL_SCHEDULE,
    _GMAIL_SCHEDULE,
)
from olo.google.oauth import GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO
from tests.olo.fake_google import FakeGoogleServer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ns(**kwargs):
    defaults = {"option": [], "force": False}
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


# ── §7.1: printed schedule parses correctly ───────────────────────────────────

def test_printed_schedule_parses_gmail() -> None:
    """The setup-printed Gmail schedule string must parse as interval minutes=5."""
    from cron.jobs import parse_schedule
    result = parse_schedule(_GMAIL_SCHEDULE)
    assert result["kind"] == "interval"
    assert result["minutes"] == 5


def test_printed_schedule_parses_gcal() -> None:
    """The setup-printed Calendar schedule string must parse as interval minutes=15."""
    from cron.jobs import parse_schedule
    result = parse_schedule(_GCAL_SCHEDULE)
    assert result["kind"] == "interval"
    assert result["minutes"] == 15


# ── Setup: M1 behaviour preserved ────────────────────────────────────────────

def test_setup_writes_shim_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    _cmd_setup(_ns(source="synthetic"))
    shim = home / "scripts" / "olo_monitor_synthetic.py"
    assert shim.exists()


def test_setup_force_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    _cmd_setup(_ns(source="synthetic"))
    _cmd_setup(_ns(source="synthetic", force=True))  # no exception


def test_setup_refuses_without_force(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    _cmd_setup(_ns(source="synthetic"))
    with pytest.raises(SystemExit) as exc:
        _cmd_setup(_ns(source="synthetic"))
    assert exc.value.code == 1


# ── Setup: gmail/gcal requires account ───────────────────────────────────────

def test_setup_gmail_requires_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _cmd_setup(_ns(source="gmail"))
    assert exc.value.code == 1


def test_setup_gmail_requires_connected_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _cmd_setup(_ns(source="gmail", option=["account=notconnected"]))
    assert exc.value.code == 1


def test_setup_gmail_with_connected_account_uses_account_in_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("acme", tmp_path / "h", monkeypatch)
        _cmd_setup(_ns(source="gmail", option=["account=acme"]))
    shim = home / "scripts" / "olo_monitor_gmail_acme.py"
    assert shim.exists()
    content = shim.read_text(encoding="utf-8")
    assert "gmail" in content
    assert "acme" in content


def test_setup_gcal_with_connected_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("acme", tmp_path / "h", monkeypatch)
        _cmd_setup(_ns(source="gcal", option=["account=acme"]))
    shim = home / "scripts" / "olo_monitor_gcal_acme.py"
    assert shim.exists()


# ── Setup: shim does not contain credentials ─────────────────────────────────

def test_setup_shim_contains_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    with FakeGoogleServer() as fake:
        fake.make_saved_credentials("acme", tmp_path / "h", monkeypatch)
        _cmd_setup(_ns(source="gmail", option=["account=acme"]))
    shim = (home / "scripts" / "olo_monitor_gmail_acme.py").read_text(encoding="utf-8")
    assert "fake_client_secret" not in shim
    assert "refresh_fake" not in shim
    assert "access_fake" not in shim


# ── Checkpoint reset ───────────────────────────────────────────────────────────

def test_checkpoint_reset_requires_confirm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        _cmd_checkpoint_reset(_ns(source="synthetic"))
    assert exc.value.code == 1


def test_checkpoint_reset_clears_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    from olo.paths import db_path
    from olo.store import connect, migrate, acquire_run, finish_run, ingest_batch
    from olo.signals import NormalizedSignal
    from datetime import datetime, UTC

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)

    # Create a checkpoint
    from olo.store import get_checkpoint
    run_id = acquire_run("synthetic", "test:1:0", conn)
    sig = NormalizedSignal(
        source="synthetic", external_id="x1", kind="payment.failed",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC), subject=None, payload={"n": 1},
    )
    ingest_batch("synthetic", run_id, (sig,), "1", None, conn)
    finish_run("synthetic", run_id, "succeeded", conn)
    conn.close()

    assert get_checkpoint("synthetic", connect(db)) == "1"

    _cmd_checkpoint_reset(_ns(source="synthetic", confirm=True))

    conn2 = connect(db)
    assert get_checkpoint("synthetic", conn2) is None
    conn2.close()


def test_checkpoint_reset_refuses_live_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    from olo.paths import db_path
    from olo.store import connect, migrate, acquire_run

    db = db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    acquire_run("synthetic", "test:1:0", conn, ttl=900)
    conn.close()

    with pytest.raises(SystemExit) as exc:
        _cmd_checkpoint_reset(_ns(source="synthetic", confirm=True))
    assert exc.value.code == 1


# ── Status works on empty home ────────────────────────────────────────────────

def test_status_works_on_empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    _cmd_status(_ns())   # must not raise


# ── Hermes skill token untouched ──────────────────────────────────────────────

def test_olo_does_not_touch_hermes_skill_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    skill_token = home / "google_token.json"
    skill_token.write_text('{"token": "hermes_skill_secret"}', encoding="utf-8")

    _cmd_setup(_ns(source="synthetic"))

    assert skill_token.read_text(encoding="utf-8") == '{"token": "hermes_skill_secret"}'


# ── Two-account isolation ──────────────────────────────────────────────────────

def test_two_account_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two different aliases have separate files, sources, and no cross-contamination."""
    _home(tmp_path, monkeypatch)
    from olo.google.credentials import (
        GoogleAccountCredentials, save_credentials, load_credentials, credential_lock,
    )
    from datetime import datetime, UTC

    for alias in ("alpha", "beta"):
        creds = GoogleAccountCredentials(
            alias=alias,
            client_id=f"cid_{alias}",
            client_secret=f"cs_{alias}",
            refresh_token=f"rt_{alias}",
            granted_scopes=frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO}),
            email_address=f"{alias}@example.com",
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=None,
        )
        with credential_lock(alias):
            save_credentials(creds)

    alpha = load_credentials("alpha")
    beta = load_credentials("beta")

    assert alpha.client_id == "cid_alpha"
    assert beta.client_id == "cid_beta"
    assert alpha.email_address != beta.email_address
    assert alpha.refresh_token != beta.refresh_token
