"""Cron E2E tests for the Google Workspace M2 pipeline (Phase A — fake server).

§9.5 acceptance criteria.
Runs real Hermes CLI as subprocesses against a temp HERMES_HOME
with a loopback FakeGoogleServer injected via api_base_override.

I4 fix: all assertions use == "completed" or == "failed", not `in`.
H2 fix: _cron_run_and_wait snapshots execution IDs before the trigger so the
returned row is always from the run we just fired, not a stale previous row.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

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


def _list_executions_in_home(home: Path, job_id: str, limit: int = 500) -> list[dict]:
    """Return executions for job_id with HERMES_HOME set to home."""
    orig = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        from cron.executions import list_executions
        return list_executions(job_id=job_id, limit=limit)
    finally:
        if orig is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = orig


def _cron_run_and_wait(
    home: Path,
    job_id: str,
    *,
    expect_status: str,
    trigger: Optional[Callable[[], None]] = None,
    timeout: float = 60.0,
) -> dict:
    """H2: fire a cron job and return exactly the new execution row.

    1. Snapshot the set of existing execution IDs (all rows, not just limit=5).
    2. Run the trigger (default: hermes cron run <job_id>).
    3. Assert the trigger exit code: 0 if expect_status=="completed", else 1.
       Assert stdout contains "Ran now:" (proves synchronous execution).
    4. wait_until: find exactly one new execution row (id not in snapshot)
       that is in a terminal state with status == expect_status.
    5. Return that row.

    Raises AssertionError if the wrong execution is returned or the timeout
    is reached before a new terminal row appears.
    """
    from cron.executions import _TERMINAL_STATES

    # Step 1: snapshot before
    before_ids = {e["id"] for e in _list_executions_in_home(home, job_id)}

    # Step 2: trigger
    if trigger is not None:
        trigger()
        # For custom triggers we can't assert exit code
        run_result = None
    else:
        run_result = _run(["cron", "run", job_id], home)

    # Step 3: assert trigger outcome
    if run_result is not None:
        expected_rc = 0 if expect_status == "completed" else 1
        assert run_result.returncode == expected_rc, (
            f"H2: hermes cron run exit code {run_result.returncode} "
            f"!= expected {expected_rc} for status={expect_status!r}; "
            f"stdout={run_result.stdout!r}; stderr={run_result.stderr!r}"
        )
        assert "Ran now:" in run_result.stdout, (
            f"H2: 'Ran now:' not found in stdout — was execution synchronous? "
            f"stdout={run_result.stdout!r}"
        )

    # Step 4–5: wait for the new row
    def _new_terminal() -> Optional[dict]:
        rows = _list_executions_in_home(home, job_id)
        for row in rows:
            if row["id"] not in before_ids and row["status"] in _TERMINAL_STATES:
                return row
        return None

    new_row = wait_until(
        _new_terminal,
        timeout=timeout,
        what=f"new terminal execution for job {job_id}",
    )

    assert new_row["status"] == expect_status, (
        f"H2: new execution status={new_row['status']!r} "
        f"!= expected {expect_status!r}"
    )
    return new_row


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
        exec1 = _cron_run_and_wait(home, job_id, expect_status="completed")

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

        exec2 = _cron_run_and_wait(home, job_id, expect_status="completed")

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

        # Third fire: idempotence — still 3 signals
        _cron_run_and_wait(home, job_id, expect_status="completed")

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

        exec1 = _cron_run_and_wait(home, job_id, expect_status="completed")

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

        exec1 = _cron_run_and_wait(home, job_id, expect_status="failed")

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
        _cron_run_and_wait(home, job_id, expect_status="completed")

        # Force 404 on next history.list
        fake.set_min_history_id("9999999")

        exec_fail = _cron_run_and_wait(home, job_id, expect_status="failed")

        # F1: checkpoint unchanged after the 404 fail
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

        # Restore fake and re-bootstrap
        fake.set_min_history_id("1000")

        exec_reboot = _cron_run_and_wait(home, job_id, expect_status="completed")

        # F1: cursor == fake's current historyId
        old_env2 = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, get_checkpoint, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            reboot_cursor = get_checkpoint("gmail_acme", conn)
            assert reboot_cursor is not None, "cursor must be set after re-bootstrap"
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
    """F4: label changes and deletions produce exact signals with correct external_ids."""
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

        # Fire 1: bootstrap
        _cron_run_and_wait(home, job_id, expect_status="completed")

        conn = _db_connect(home)
        from olo.store import count_signals, get_checkpoint
        assert count_signals("gmail_acme", conn) == 0
        assert get_checkpoint("gmail_acme", conn) is not None
        conn.close()

        # Fire 2: add msg1 → 1 email.received
        fake.gmail.add_message("msg1", labels=["INBOX", "UNREAD"], subject="Inbox msg")
        _cron_run_and_wait(home, job_id, expect_status="completed")

        conn = _db_connect(home)
        assert count_signals("gmail_acme", conn) == 1
        row = conn.execute(
            "SELECT external_id, kind FROM signals WHERE source='gmail_acme'"
        ).fetchone()
        assert row is not None
        assert row["kind"] == "email.received"
        assert row["external_id"] == "msg1"
        conn.close()

        # Fire 3: label change + deletion → 2 new signals
        fake.gmail.remove_labels("msg1", ["UNREAD"])
        R1 = fake.gmail.current_history_id

        fake.gmail.delete_message("msg1")
        R2 = fake.gmail.current_history_id

        _cron_run_and_wait(home, job_id, expect_status="completed")

        conn = _db_connect(home)
        total = count_signals("gmail_acme", conn)
        assert total == 3, f"expected 3 total signals after fire 3, got {total}"

        new_rows = conn.execute(
            "SELECT external_id, kind, payload FROM signals "
            "WHERE source='gmail_acme' AND external_id != 'msg1'"
        ).fetchall()
        assert len(new_rows) == 2, (
            f"expected 2 new signals, got {len(new_rows)}: "
            f"{[r['external_id'] for r in new_rows]}"
        )

        import json as _json
        new_by_kind = {r["kind"]: r for r in new_rows}

        removed_row = new_by_kind["email.labels_removed"]
        assert removed_row["external_id"] == f"h{R1}:msg1:removed"
        removed_payload = _json.loads(removed_row["payload"])
        assert removed_payload.get("labels") == ["UNREAD"]

        deleted_row = new_by_kind["email.deleted"]
        assert deleted_row["external_id"] == f"h{R2}:msg1:deleted"

        final_chk = get_checkpoint("gmail_acme", conn)
        assert final_chk == fake.gmail.current_history_id
        conn.close()

        # Fire 4: no new signals
        _cron_run_and_wait(home, job_id, expect_status="completed")
        conn = _db_connect(home)
        assert count_signals("gmail_acme", conn) == 3
        conn.close()


# ── §9.5: both sources of one account fired ───────────────────────────────────

def test_both_sources_gmail_and_gcal(e2e_home: Path) -> None:
    """§9.5: Gmail + Calendar jobs for the same account both complete."""
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        fake.calendar.add_event("ev1", summary="Standup")
        _save_creds(fake, "acme", home)

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
            _cron_run_and_wait(home, job_id, expect_status="completed")


# ── §9.5: zero-LLM proof through cron.scheduler.run_job ──────────────────────

def test_cron_run_job_never_constructs_agent(
    e2e_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F3: run_job with no_agent=True never constructs AIAgent."""
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)

        monkeypatch.setenv("HERMES_HOME", str(home))

        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0, f"setup failed: {r.stderr}"

        from olo.store import connect, migrate, count_signals, get_checkpoint
        from olo.paths import db_path
        conn = connect(db_path())
        migrate(conn)
        conn.close()

        from cron.jobs import create_job
        job = create_job(
            None,
            "every 5m",
            name="olo-gmail-acme",
            script="olo_monitor_gmail_acme.py",
            no_agent=True,
            deliver="local",
        )
        assert job["id"]

        constructed: list[bool] = []

        class _NeverConstructAgent:
            def __init__(self, *a, **kw):
                constructed.append(True)
                raise AssertionError(
                    "F3: AIAgent was constructed — cron script must not call LLM"
                )

        import run_agent
        monkeypatch.setattr(run_agent, "AIAgent", _NeverConstructAgent)

        import cron.scheduler as _sched

        # Fire 1: bootstrap
        success, _doc, _resp, error = _sched.run_job(job)
        assert success is True, f"F3: bootstrap must succeed, error={error!r}"
        assert not constructed

        conn = connect(db_path())
        assert get_checkpoint("gmail_acme", conn) is not None
        conn.close()

        # Fire 2: 1 inbox message → 1 signal
        fake.gmail.add_message("msg1", subject="Test zero-LLM")
        success2, _doc2, _resp2, error2 = _sched.run_job(job)
        assert success2 is True, f"F3: incremental must succeed, error={error2!r}"
        assert not constructed

        conn = connect(db_path())
        assert count_signals("gmail_acme", conn) == 1
        conn.close()

        audit_path = home / "cron" / "usage_audit.jsonl"
        if audit_path.exists():
            content = audit_path.read_text(encoding="utf-8")
            assert job["id"] not in content


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

        _cron_run_and_wait(home, job_id, expect_status="completed")

    audit_path = home / "cron" / "usage_audit.jsonl"
    if audit_path.exists():
        content = audit_path.read_text(encoding="utf-8")
        assert job_id not in content, "no-agent job must not appear in usage audit"


# ── H2 negative test: stale execution must not be returned ───────────────────

def test_wait_helper_rejects_stale_execution(e2e_home: Path) -> None:
    """H2: _cron_run_and_wait must not return a row from a previous run.

    After one completed run, call _cron_run_and_wait with trigger=lambda: None
    (no actual run fired) and a short timeout. It must raise (timeout) instead
    of returning the previous row.

    This proves the helper cannot be fooled by stale state — a defect that
    affected the old _wait_for_terminal helper which could return the previous
    execution when the new run was skipped or slow to appear.
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

        # Complete one run so a terminal row exists
        _cron_run_and_wait(home, job_id, expect_status="completed")

        # Now try with a no-op trigger — no new execution will be created
        with pytest.raises(AssertionError, match="timed out"):
            _cron_run_and_wait(
                home, job_id,
                expect_status="completed",
                trigger=lambda: None,
                timeout=2.0,   # short timeout — no run fired
            )
