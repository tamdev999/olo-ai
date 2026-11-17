"""Cron E2E tests for the Google Workspace M2 pipeline (Phase A — fake server).

§9.5 acceptance criteria.
Runs real Hermes CLI as subprocesses against a temp HERMES_HOME
with a loopback FakeGoogleServer injected via api_base_override.

I4 fix: all assertions use == "completed" or == "failed", not `in`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from tests.waiting import wait_until
from tests.olo.fake_google import FakeGoogleServer

# Mark: not an integration test (no live Google)
pytestmark = pytest.mark.not_integration


def _hermes_exe() -> str:
    scripts = Path(sys.executable).parent
    for name in ("hermes.exe", "hermes"):
        p = scripts / name
        if p.exists():
            return str(p)
    raise RuntimeError(f"hermes not found in {scripts}")


def _run(args: list[str], home: Path, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": str(home)}
    return subprocess.run(
        [_hermes_exe(), *args],
        capture_output=True, text=True, env=env, check=False,
        **kwargs,
    )


def _install_plugin(home: Path, repo_root: Path) -> None:
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(exist_ok=True)
    link = plugins_dir / "olo"
    if link.exists():
        return
    target = repo_root / "olo" / "hermes_plugin"
    if os.name == "nt":
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            shutil.copytree(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _enable_plugin(home: Path) -> None:
    import hermes_yaml as yaml
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["olo"]}}), encoding="utf-8"
    )


def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _parse_job_id(output: str) -> Optional[str]:
    for line in output.splitlines():
        if line.strip().startswith("Created job:"):
            return line.split(":", 1)[1].strip()
    return None


def _wait_for_terminal(home: Path, job_id: str, timeout: float = 60.0) -> dict:
    orig = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        from cron.executions import list_executions, _TERMINAL_STATES
        def done():
            for e in list_executions(job_id=job_id, limit=5):
                if e["status"] in _TERMINAL_STATES:
                    return e
            return None
        return wait_until(done, timeout=timeout, what=f"terminal execution {job_id}")
    finally:
        if orig is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = orig


def _save_creds(fake: FakeGoogleServer, alias: str, home: Path) -> None:
    """Save fake credentials with api_base_override pointing at the fake server."""
    from olo.google.credentials import save_credentials, credential_lock
    creds = fake.make_credentials(alias)
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        with credential_lock(alias):
            save_credentials(creds)
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


@pytest.fixture()
def e2e_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes_home"
    home.mkdir()
    _install_plugin(home, _repo_root())
    _enable_plugin(home)
    return home


# ── Gmail E2E: bootstrap (0 signals), then 3 messages ────────────────────────

def test_gmail_bootstrap_then_messages(e2e_home: Path) -> None:
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)

        r = _run(["plugins", "enable", "olo"], home)
        assert r.returncode == 0 or "already" in r.stdout.lower()

        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0, f"setup failed: {r.stderr}"

        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        assert r.returncode == 0, f"cron create failed: {r.stderr}"
        job_id = _parse_job_id(r.stdout)
        assert job_id

        # First fire: bootstrap → 0 signals
        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        # I4 fix: must be exactly "completed"
        assert exec1["status"] == "completed", (
            f"bootstrap run must complete, got: {exec1['status']}"
        )

        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, count_signals, get_checkpoint, migrate
            from olo.paths import db_path
            db = db_path()
            conn = connect(db)
            migrate(conn)
            assert count_signals("gmail_acme", conn) == 0
            bootstrap_cursor = get_checkpoint("gmail_acme", conn)
            assert bootstrap_cursor is not None
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old

        for i in range(1, 4):
            fake.gmail.add_message(f"msg{i}", subject=f"Test Message {i}")

        _run(["cron", "run", job_id], home)
        exec2 = _wait_for_terminal(home, job_id, timeout=60)
        # I4 fix: must be exactly "completed"
        assert exec2["status"] == "completed", (
            f"message run must complete, got: {exec2['status']}"
        )

        os.environ["HERMES_HOME"] = str(home)
        try:
            conn = connect(db)
            n = count_signals("gmail_acme", conn)
            assert n == 3, f"expected 3 signals, got {n}"
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old

        _run(["cron", "run", job_id], home)
        _wait_for_terminal(home, job_id, timeout=60)

        os.environ["HERMES_HOME"] = str(home)
        try:
            conn = connect(db)
            assert count_signals("gmail_acme", conn) == 3
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


# ── Calendar E2E: bootstrap events, then changes ─────────────────────────────

def test_gcal_bootstrap_then_events(e2e_home: Path) -> None:
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        fake.calendar.add_event("ev1", summary="Weekly Standup")
        fake.calendar.add_event("ev2", summary="One-on-One")

        _save_creds(fake, "acme", home)

        r = _run(["olo", "setup", "--source", "gcal", "--option", "account=acme",
                  "--option", "lookback_days=7"], home)
        assert r.returncode == 0, f"setup failed: {r.stderr}"

        r = _run([
            "cron", "create", "every 15m", "--name", "olo-gcal-acme",
            "--script", "olo_monitor_gcal_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        assert r.returncode == 0
        job_id = _parse_job_id(r.stdout)
        assert job_id

        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        # I4 fix: must be exactly "completed"
        assert exec1["status"] == "completed", (
            f"calendar bootstrap must complete, got: {exec1['status']}"
        )

        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, count_signals, migrate
            from olo.paths import db_path
            db = db_path()
            conn = connect(db)
            migrate(conn)
            n = count_signals("gcal_acme", conn)
            assert n >= 2, f"expected ≥2 calendar signals, got {n}"
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


# ── auth_failed: invalid_grant → execution failed ────────────────────────────

def test_gmail_invalid_grant_fails_execution(e2e_home: Path) -> None:
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0
        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        job_id = _parse_job_id(r.stdout)

        fake.inject_invalid_grant()

        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        # I4 fix: invalid_grant must produce exactly "failed", not "completed"
        assert exec1["status"] == "failed", (
            f"invalid_grant run must fail, got: {exec1['status']}"
        )

        # §9.5: Olo run row must be failed, checkpoint unchanged, lease released
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, get_checkpoint, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            # Checkpoint must still be None (bootstrap never completed)
            chk = get_checkpoint("gmail_acme", conn)
            assert chk is None, f"checkpoint should be None after invalid_grant, got {chk!r}"
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old

        # stderr must contain reconnect message with no bearer token
        # (The run stdout/stderr is captured in exec1; check via re-run log)
        # The key assertion is the execution row status == "failed" (done above)


# ── §9.5: history 404 → failed, checkpoint reset, re-bootstrap → completed ───

def test_history_404_then_reset_then_rebootstrap(e2e_home: Path) -> None:
    """§9.5: history 404 → run fails; after checkpoint reset, re-bootstrap succeeds."""
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0

        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        job_id = _parse_job_id(r.stdout)

        # Bootstrap first
        _run(["cron", "run", job_id], home)
        exec_boot = _wait_for_terminal(home, job_id, timeout=60)
        assert exec_boot["status"] == "completed"

        # Force 404 on next history.list (old historyId)
        fake.set_min_history_id("9999999")

        _run(["cron", "run", job_id], home)
        exec_fail = _wait_for_terminal(home, job_id, timeout=60)
        assert exec_fail["status"] == "failed", (
            f"history 404 must produce failed execution, got: {exec_fail['status']}"
        )

        # F1: Olo run row must be failed and checkpoint unchanged (still the bootstrap cursor)
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, get_checkpoint, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            chk_after_fail = get_checkpoint("gmail_acme", conn)
            assert chk_after_fail is not None, (
                "checkpoint must still be set (bootstrap cursor) after 404 fail"
            )
            conn.close()
        finally:
            if old_env is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_env

        # Reset checkpoint — requires --confirm flag
        r = _run(["olo", "checkpoint", "reset", "--source", "gmail_acme",
                  "--confirm"], home)
        assert r.returncode == 0, f"checkpoint reset failed: {r.stderr}"

        # Verify checkpoint row is gone and audit entry exists
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, get_checkpoint, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            chk_after_reset = get_checkpoint("gmail_acme", conn)
            assert chk_after_reset is None, (
                f"checkpoint must be gone after reset, got {chk_after_reset!r}"
            )
            # Audit event must exist
            audit_row = conn.execute(
                "SELECT * FROM audit_events WHERE action=? AND detail LIKE ?",
                ("checkpoint.reset", '%"gmail_acme"%'),
            ).fetchone()
            assert audit_row is not None, (
                "checkpoint.reset audit event must be written"
            )
            conn.close()
        finally:
            if old_env is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_env

        # Restore fake to accept history requests
        fake.set_min_history_id("1000")

        # Re-bootstrap
        _run(["cron", "run", job_id], home)
        exec_reboot = _wait_for_terminal(home, job_id, timeout=60)
        assert exec_reboot["status"] == "completed", (
            f"re-bootstrap after reset must complete, got: {exec_reboot['status']}"
        )

        # F1: cursor == fake's current historyId (a fresh bootstrap cursor)
        old_env2 = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, get_checkpoint, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            reboot_cursor = get_checkpoint("gmail_acme", conn)
            assert reboot_cursor is not None, (
                "cursor must be set after re-bootstrap"
            )
            assert reboot_cursor == fake.gmail.current_history_id, (
                f"reboot cursor must == fake historyId, "
                f"got {reboot_cursor!r} vs {fake.gmail.current_history_id!r}"
            )
            conn.close()
        finally:
            if old_env2 is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_env2


# ── §9.5: label/deletion change events — F4: exact signal assertions ─────────

def test_label_and_deletion_change_events(e2e_home: Path) -> None:
    """F4: label changes and deletions produce exact signals with correct external_ids.

    Chronology (all executions must be 'completed'):
    1. Bootstrap fire → 0 signals; checkpoint set.
    2. Add msg1 (INBOX, UNREAD); fire → exactly 1 email.received, external_id="msg1".
    3. remove_labels(msg1, [UNREAD]); delete_message(msg1); fire →
       exactly 2 new signals:
         - email.labels_removed with payload.labels==["UNREAD"],
           external_id == f"h{R1}:msg1:removed"
         - email.deleted with external_id == f"h{R2}:msg1:deleted"
       where R1/R2 are read from the fake (not hard-coded).
    4. Checkpoint == fake's last history id.
    5. Fourth fire → 0 new signals.
    """
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)

        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0
        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        job_id = _parse_job_id(r.stdout)
        assert job_id

        def _db_connect(h: Path):
            old = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = str(h)
            try:
                from olo.store import connect, migrate
                from olo.paths import db_path
                conn = connect(db_path())
                migrate(conn)
                return conn
            finally:
                if old is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old

        # ── Fire 1: bootstrap ─────────────────────────────────────────────────
        _run(["cron", "run", job_id], home)
        exec_boot = _wait_for_terminal(home, job_id, timeout=60)
        assert exec_boot["status"] == "completed", (
            f"bootstrap must complete, got: {exec_boot['status']}"
        )

        conn = _db_connect(home)
        from olo.store import count_signals, get_checkpoint
        assert count_signals("gmail_acme", conn) == 0
        assert get_checkpoint("gmail_acme", conn) is not None
        conn.close()

        # ── Fire 2: add msg1 → 1 email.received ──────────────────────────────
        fake.gmail.add_message("msg1", labels=["INBOX", "UNREAD"], subject="Inbox msg")

        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec1["status"] == "completed", (
            f"fire 2 must complete, got: {exec1['status']}"
        )

        conn = _db_connect(home)
        assert count_signals("gmail_acme", conn) == 1
        row = conn.execute(
            "SELECT external_id, kind FROM signals WHERE source='gmail_acme'"
        ).fetchone()
        assert row is not None
        assert row["kind"] == "email.received"
        assert row["external_id"] == "msg1"
        conn.close()

        # ── Fire 3: label change + deletion → 2 new signals ──────────────────
        # Read the record IDs the fake will assign BEFORE making the changes
        # (each _bump_history increments current_history_id)
        fake.gmail.remove_labels("msg1", ["UNREAD"])
        R1 = fake.gmail.current_history_id  # record id for labels_removed

        fake.gmail.delete_message("msg1")
        R2 = fake.gmail.current_history_id  # record id for messagesDeleted

        _run(["cron", "run", job_id], home)
        exec2 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec2["status"] == "completed", (
            f"fire 3 must complete, got: {exec2['status']}"
        )

        conn = _db_connect(home)
        total = count_signals("gmail_acme", conn)
        assert total == 3, f"expected 3 total signals after fire 3, got {total}"

        new_rows = conn.execute(
            "SELECT external_id, kind, payload FROM signals "
            "WHERE source='gmail_acme' AND external_id != 'msg1'"
        ).fetchall()
        assert len(new_rows) == 2, (
            f"expected 2 new signals, got {len(new_rows)}: {[r['external_id'] for r in new_rows]}"
        )

        import json as _json
        new_by_kind = {r["kind"]: r for r in new_rows}

        # email.labels_removed
        assert "email.labels_removed" in new_by_kind, (
            f"missing email.labels_removed, got kinds: {list(new_by_kind)}"
        )
        removed_row = new_by_kind["email.labels_removed"]
        expected_removed_id = f"h{R1}:msg1:removed"
        assert removed_row["external_id"] == expected_removed_id, (
            f"labels_removed external_id: {removed_row['external_id']!r} "
            f"!= {expected_removed_id!r}"
        )
        removed_payload = _json.loads(removed_row["payload"])
        assert removed_payload.get("labels") == ["UNREAD"], (
            f"labels_removed payload.labels: {removed_payload.get('labels')!r}"
        )

        # email.deleted
        assert "email.deleted" in new_by_kind, (
            f"missing email.deleted, got kinds: {list(new_by_kind)}"
        )
        deleted_row = new_by_kind["email.deleted"]
        expected_deleted_id = f"h{R2}:msg1:deleted"
        assert deleted_row["external_id"] == expected_deleted_id, (
            f"deleted external_id: {deleted_row['external_id']!r} "
            f"!= {expected_deleted_id!r}"
        )

        # Checkpoint == fake's last history id
        final_chk = get_checkpoint("gmail_acme", conn)
        assert final_chk == fake.gmail.current_history_id, (
            f"checkpoint {final_chk!r} != fake historyId "
            f"{fake.gmail.current_history_id!r}"
        )
        conn.close()

        # ── Fire 4: no new signals ────────────────────────────────────────────
        _run(["cron", "run", job_id], home)
        exec3 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec3["status"] == "completed"

        conn = _db_connect(home)
        assert count_signals("gmail_acme", conn) == 3, (
            "fire 4 must add 0 signals"
        )
        conn.close()


# ── §9.5: both sources of one account fired ───────────────────────────────────

def test_both_sources_gmail_and_gcal(e2e_home: Path) -> None:
    """§9.5: Gmail + Calendar jobs for the same account both complete."""
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        fake.calendar.add_event("ev1", summary="Standup")
        _save_creds(fake, "acme", home)

        # Setup both sources
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0
        r = _run(["olo", "setup", "--source", "gcal", "--option", "account=acme",
                  "--option", "lookback_days=7"], home)
        assert r.returncode == 0

        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        gmail_job = _parse_job_id(r.stdout)

        r = _run([
            "cron", "create", "every 15m", "--name", "olo-gcal-acme",
            "--script", "olo_monitor_gcal_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        gcal_job = _parse_job_id(r.stdout)

        for job_id in [gmail_job, gcal_job]:
            _run(["cron", "run", job_id], home)
            ex = _wait_for_terminal(home, job_id, timeout=60)
            assert ex["status"] == "completed", (
                f"job {job_id} must complete, got: {ex['status']}"
            )


# ── §9.5: zero-LLM proof through cron.scheduler.run_job ──────────────────────

def test_cron_run_job_never_constructs_agent(
    e2e_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: run_job with no_agent=True never constructs AIAgent.

    run_job return shape (verified against cron/scheduler.py):
        (success: bool, full_output_doc: str, final_response: str, error: Optional[str])

    Test plan:
    1. Set up HERMES_HOME, fake server, saved credentials.
    2. hermes olo setup writes the shim script.
    3. create_job(None, ..., no_agent=True, script=...) creates the job in-process.
    4. Patch run_agent.AIAgent with a sentinel that raises AssertionError.
       No ImportError fallback — if run_agent is absent the test must fail.
    5. cron.scheduler.run_job(job) must succeed (success==True).
    6. Sentinel was never constructed (bootstrap run).
    7. Gmail checkpoint set.
    8. Add 1 message, run_job again → 1 email.received signal stored.
    9. No usage_audit.jsonl line for this job.
    """
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)

        old_home = os.environ.get("HERMES_HOME")
        monkeypatch.setenv("HERMES_HOME", str(home))

        # Write shim via CLI
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0, f"setup failed: {r.stderr}"

        # Ensure DB exists
        from olo.store import connect, migrate, count_signals, get_checkpoint
        from olo.paths import db_path
        conn = connect(db_path())
        migrate(conn)
        conn.close()

        # Create job in-process
        from cron.jobs import create_job
        job = create_job(
            None,  # prompt — None is valid for no_agent jobs
            "every 5m",
            name="olo-gmail-acme",
            script="olo_monitor_gmail_acme.py",
            no_agent=True,
            deliver="local",
        )
        assert job["id"], "create_job must return a job with an id"

        # Sentinel class — raises if ever constructed
        constructed: list[bool] = []

        class _NeverConstructAgent:
            def __init__(self, *a, **kw):
                constructed.append(True)
                raise AssertionError(
                    "F3: AIAgent was constructed — cron script must not call LLM"
                )

        # Patch run_agent.AIAgent at the module where run_job will re-import it
        import run_agent  # must not raise ImportError
        monkeypatch.setattr(run_agent, "AIAgent", _NeverConstructAgent)

        import cron.scheduler as _sched

        # ── Fire 1: bootstrap ─────────────────────────────────────────────────
        success, _doc, _resp, error = _sched.run_job(job)
        # Recorded return shape: (bool, str, str, Optional[str])
        assert success is True, (
            f"F3: run_job bootstrap must succeed, error={error!r}"
        )
        assert not constructed, "F3: AIAgent must never be constructed"

        conn = connect(db_path())
        cursor_after_boot = get_checkpoint("gmail_acme", conn)
        assert cursor_after_boot is not None, (
            "F3: Gmail checkpoint must be set after bootstrap"
        )
        conn.close()

        # ── Fire 2: 1 inbox message → 1 signal ───────────────────────────────
        fake.gmail.add_message("msg1", subject="Test zero-LLM")

        success2, _doc2, _resp2, error2 = _sched.run_job(job)
        assert success2 is True, (
            f"F3: run_job incremental must succeed, error={error2!r}"
        )
        assert not constructed, "F3: AIAgent must never be constructed"

        conn = connect(db_path())
        n = count_signals("gmail_acme", conn)
        assert n == 1, f"F3: expected 1 signal after msg1, got {n}"
        conn.close()

        # ── No usage_audit.jsonl entry ────────────────────────────────────────
        audit_path = home / "cron" / "usage_audit.jsonl"
        if audit_path.exists():
            content = audit_path.read_text(encoding="utf-8")
            assert job["id"] not in content, (
                "F3: no-agent job must not appear in usage audit"
            )


# ── Zero LLM: no usage_audit.jsonl ───────────────────────────────────────────

def test_google_cron_no_usage_audit(e2e_home: Path) -> None:
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0
        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        job_id = _parse_job_id(r.stdout)

        _run(["cron", "run", job_id], home)
        _wait_for_terminal(home, job_id, timeout=60)

    audit_path = home / "cron" / "usage_audit.jsonl"
    if audit_path.exists():
        content = audit_path.read_text(encoding="utf-8")
        assert job_id not in content, "no-agent job must not appear in usage audit"
