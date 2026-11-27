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


# ══════════════════════════════════════════════════════════════════════════════
# M2b: --upgrade / --revoke-first CLI parsing tests
# ══════════════════════════════════════════════════════════════════════════════

def test_upgrade_flag_accepted_by_parser() -> None:
    """setup_parser wires --upgrade onto 'google connect'."""
    import argparse
    from olo.hermes_plugin.cli import setup_parser

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    olo_p = sub.add_parser("olo")
    setup_parser(olo_p)

    args = p.parse_args(
        ["olo", "google", "connect",
         "--account", "test",
         "--client-secret-file", "/tmp/cs.json",
         "--upgrade"]
    )
    assert args.upgrade is True
    assert args.revoke_first is False


def test_revoke_first_flag_accepted_by_parser() -> None:
    """--revoke-first is accepted alongside --upgrade."""
    import argparse
    from olo.hermes_plugin.cli import setup_parser

    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    olo_p = sub.add_parser("olo")
    setup_parser(olo_p)

    args = p.parse_args(
        ["olo", "google", "connect",
         "--account", "test",
         "--client-secret-file", "/tmp/cs.json",
         "--upgrade", "--revoke-first"]
    )
    assert args.upgrade is True
    assert args.revoke_first is True


def test_revoke_first_without_upgrade_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--revoke-first without --upgrade must print an error and exit 1."""
    import argparse, types, sys
    from olo.hermes_plugin.cli import _cmd_google_connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    ns = types.SimpleNamespace(
        account="test",
        client_secret_file=str(tmp_path / "cs.json"),
        manual=False,
        production=False,
        upgrade=False,
        revoke_first=True,
    )
    # Create a minimal client-secret file so the file-load check passes
    (tmp_path / "cs.json").write_text(
        '{"installed":{"client_id":"cid","client_secret":"cs",'
        '"redirect_uris":["http://127.0.0.1"]}}'
    )
    with pytest.raises(SystemExit) as exc_info:
        _cmd_google_connect(ns)
    assert exc_info.value.code == 1


def test_upgrade_calls_upgrade_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--upgrade path calls upgrade_credentials() not the basic connect flow."""
    import types
    from olo.hermes_plugin.cli import _cmd_google_connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Create dummy client secret
    cs_path = tmp_path / "cs.json"
    cs_path.write_text(
        '{"installed":{"client_id":"cid","client_secret":"cs",'
        '"redirect_uris":["http://127.0.0.1"]}}'
    )

    called_with: list[tuple] = []

    class _FakeResult:
        success = True
        scope_union = False
        needs_revoke_first = False
        reason = ""

    def _fake_upgrade(alias, *, revoke_first=False, connect_fn=None,
                      oauth_publishing_status=None, **kw):
        called_with.append((alias, revoke_first, oauth_publishing_status))
        return _FakeResult()

    # Patch where it is imported from inside _cmd_google_connect
    monkeypatch.setattr("olo.google.oauth.upgrade_credentials", _fake_upgrade)

    ns = types.SimpleNamespace(
        account="test",
        client_secret_file=str(cs_path),
        manual=False,
        production=False,
        upgrade=True,
        revoke_first=False,
    )
    _cmd_google_connect(ns)

    assert called_with, "upgrade_credentials was not called"
    alias_called, revoke_called, status_called = called_with[0]
    assert alias_called == "test"
    assert revoke_called is False
    assert status_called == "testing"


def test_upgrade_revoke_first_passed_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--upgrade --revoke-first passes revoke_first=True to upgrade_credentials."""
    import types
    from olo.hermes_plugin.cli import _cmd_google_connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    cs_path = tmp_path / "cs.json"
    cs_path.write_text(
        '{"installed":{"client_id":"cid","client_secret":"cs",'
        '"redirect_uris":["http://127.0.0.1"]}}'
    )

    called: list[bool] = []

    class _FakeResult:
        success = True
        scope_union = False
        needs_revoke_first = False
        reason = ""

    def _fake_upgrade(alias, *, revoke_first=False, connect_fn=None, **kw):
        called.append(revoke_first)
        return _FakeResult()

    monkeypatch.setattr("olo.google.oauth.upgrade_credentials", _fake_upgrade)

    ns = types.SimpleNamespace(
        account="test",
        client_secret_file=str(cs_path),
        manual=False,
        production=False,
        upgrade=True,
        revoke_first=True,
    )
    _cmd_google_connect(ns)

    assert called == [True], f"expected revoke_first=True, got {called}"


def test_upgrade_scope_union_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scope_union result from upgrade_credentials → exit 1 with helpful message."""
    import types
    from olo.hermes_plugin.cli import _cmd_google_connect

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    cs_path = tmp_path / "cs.json"
    cs_path.write_text(
        '{"installed":{"client_id":"cid","client_secret":"cs",'
        '"redirect_uris":["http://127.0.0.1"]}}'
    )

    class _ScopeUnionResult:
        success = False
        scope_union = True
        needs_revoke_first = True
        reason = "scope_union"

    monkeypatch.setattr(
        "olo.google.oauth.upgrade_credentials",
        lambda *a, **kw: _ScopeUnionResult(),
    )

    ns = types.SimpleNamespace(
        account="test",
        client_secret_file=str(cs_path),
        manual=False,
        production=False,
        upgrade=True,
        revoke_first=False,
    )
    with pytest.raises(SystemExit) as exc_info:
        _cmd_google_connect(ns)

    assert exc_info.value.code == 1


# ── Reported scopes are the granted ones (live acceptance, 2026-11-27) ───────

def test_status_reports_exactly_the_granted_read_only_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The connect message and status printed ``['gmail.metadata', 'calendar']``
    for a token holding the three read-only v2 scopes."""
    from datetime import UTC, datetime

    from olo.google.credentials import (
        CALENDAR_LIST_SCOPE, CALENDAR_READONLY_SCOPE, GMAIL_READONLY_SCOPE, SCOPE_PROFILE_FULL_V1,
        GoogleAccountCredentials, credential_lock, save_credentials,
    )
    from olo.hermes_plugin.cli import _cmd_google_status

    _home(tmp_path, monkeypatch)
    creds = GoogleAccountCredentials(
        alias="livetest", client_id="cid", client_secret="cs", refresh_token="rt",
        granted_scopes=frozenset({GMAIL_READONLY_SCOPE, CALENDAR_READONLY_SCOPE, CALENDAR_LIST_SCOPE}),
        email_address="biz@example.com", refresh_token_issued_at=datetime.now(UTC),
        oauth_publishing_status="testing", api_base_override=None, scope_profile=SCOPE_PROFILE_FULL_V1)
    with credential_lock("livetest"):
        save_credentials(creds)
    want = "['calendar.calendarlist.readonly', 'calendar.events.readonly', 'gmail.readonly']"
    assert str(creds.scope_labels()) == want
    _cmd_google_status(_ns(check=False))
    _cmd_status(_ns())
    out = capsys.readouterr().out
    assert out.count(want) == 2 and "gmail.metadata" not in out and "'calendar'" not in out
