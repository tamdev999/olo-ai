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
    # The `hermes` executable's editable install may point at another checkout
    # (it did in the WSL test venv: a stale pre-S5 copy).  Putting this repo
    # first on PYTHONPATH makes the CLI, the plugin and every cron-run shim
    # import the code under test, not whatever the installer recorded.
    repo = str(_repo_root())
    pythonpath = os.pathsep.join(p for p in (repo, os.environ.get("PYTHONPATH", "")) if p)
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": pythonpath}
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

    # Step 4–5: wait for new rows and assert exactly one
    def _new_terminal() -> Optional[list]:
        rows = _list_executions_in_home(home, job_id)
        new = [r for r in rows
               if r["id"] not in before_ids and r["status"] in _TERMINAL_STATES]
        return new if new else None

    new_rows = wait_until(
        _new_terminal,
        timeout=timeout,
        what=f"new terminal execution for job {job_id}",
    )

    assert len(new_rows) == 1, (
        f"H2: expected exactly 1 new terminal execution, got {len(new_rows)}: "
        f"{[r['id'] for r in new_rows]}"
    )
    new_row = new_rows[0]
    assert new_row["status"] == expect_status, (
        f"H2: new execution status={new_row['status']!r} "
        f"!= expected {expect_status!r}"
    )
    return new_row


def _save_creds(fake: FakeGoogleServer, alias: str, home: Path) -> None:
    """Save fake credentials with api_base_override pointing at the fake server.

    Fix 8: defaults to V2 scopes (gmail.readonly + calendar.events.readonly +
    calendar.calendarlist.readonly) since legacy credentials are now rejected
    by the adapters (Fix 4).
    """
    from olo.google.credentials import save_credentials, credential_lock
    creds = fake.make_credentials(alias, gmail_readonly=True)  # Fix 8: V2 default
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
            # V2 credentials emit both email.received and email.content per
            # message; assert the exact received count rather than total.
            n_received = conn.execute(
                "SELECT COUNT(*) FROM signals "
                "WHERE source='gmail_acme' AND kind='email.received'"
            ).fetchone()[0]
            assert n_received == 3, (
                f"expected 3 email.received signals, got {n_received}"
            )
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old

        # Third fire: idempotence — still 3 email.received signals
        _cron_run_and_wait(home, job_id, expect_status="completed")

        os.environ["HERMES_HOME"] = str(home)
        try:
            conn = connect(db)
            n_received_idem = conn.execute(
                "SELECT COUNT(*) FROM signals "
                "WHERE source='gmail_acme' AND kind='email.received'"
            ).fetchone()[0]
            assert n_received_idem == 3, (
                f"idempotence: expected 3 email.received signals, "
                f"got {n_received_idem}"
            )
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

        # Force 404 on next history.list — gap auto-detection kicks in
        fake.set_min_history_id("9999999")

        # With gap auto-detection: run succeeds (gap cursor written), NOT fails
        exec_gap = _cron_run_and_wait(home, job_id, expect_status="completed")

        # F1: checkpoint must now be a gap-mode backfill cursor (not an error state)
        old_env = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            import json as _json
            from olo.store import connect, get_checkpoint, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            chk_after_gap = get_checkpoint("gmail_acme", conn)
            assert chk_after_gap is not None, (
                "checkpoint must be set (gap backfill cursor) after 404 gap detection"
            )
            try:
                cd = _json.loads(chk_after_gap)
                assert cd.get("phase") == "backfill", (
                    f"checkpoint must be backfill cursor, got {chk_after_gap!r}"
                )
                assert cd.get("mode") == "gap", (
                    f"checkpoint mode must be 'gap', got {cd.get('mode')!r}"
                )
            except (_json.JSONDecodeError, ValueError):
                pytest.fail(
                    f"checkpoint is not a gap JSON cursor: {chk_after_gap!r}"
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
        # V2 creds emit email.received + email.content per message; check
        # the received count and confirm the received row exists.
        n_received_f2 = conn.execute(
            "SELECT COUNT(*) FROM signals "
            "WHERE source='gmail_acme' AND kind='email.received'"
        ).fetchone()[0]
        assert n_received_f2 == 1, (
            f"expected 1 email.received after fire 2, got {n_received_f2}"
        )
        row = conn.execute(
            "SELECT external_id, kind FROM signals "
            "WHERE source='gmail_acme' AND kind='email.received'"
        ).fetchone()
        assert row is not None
        assert row["kind"] == "email.received"
        assert row["external_id"] == "msg1"
        conn.close()

        # Fire 3: label change + deletion → 2 new non-received signals
        fake.gmail.remove_labels("msg1", ["UNREAD"])
        R1 = fake.gmail.current_history_id

        fake.gmail.delete_message("msg1")
        R2 = fake.gmail.current_history_id

        _cron_run_and_wait(home, job_id, expect_status="completed")

        conn = _db_connect(home)
        # Exact counts per kind: 1 received + 1 content (from fire 2) +
        # 1 labels_removed + 1 deleted (from fire 3).
        n_received_f3 = conn.execute(
            "SELECT COUNT(*) FROM signals "
            "WHERE source='gmail_acme' AND kind='email.received'"
        ).fetchone()[0]
        assert n_received_f3 == 1, (
            f"expected 1 email.received after fire 3, got {n_received_f3}"
        )

        new_rows = conn.execute(
            "SELECT external_id, kind, payload FROM signals "
            "WHERE source='gmail_acme' "
            "  AND kind IN ('email.labels_removed', 'email.deleted')"
        ).fetchall()
        assert len(new_rows) == 2, (
            f"expected 2 label/delete signals, got {len(new_rows)}: "
            f"{[(r['kind'], r['external_id']) for r in new_rows]}"
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

        # Fire 4: no new label/delete signals (idempotent)
        _cron_run_and_wait(home, job_id, expect_status="completed")
        conn = _db_connect(home)
        n_label_del_f4 = conn.execute(
            "SELECT COUNT(*) FROM signals "
            "WHERE source='gmail_acme' "
            "  AND kind IN ('email.labels_removed', 'email.deleted')"
        ).fetchone()[0]
        assert n_label_del_f4 == 2, (
            f"idempotence: expected 2 label/delete signals, got {n_label_del_f4}"
        )
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
        # V2 creds produce email.received + email.content per message; check
        # that exactly 1 email.received signal was stored (not total count).
        n_received = conn.execute(
            "SELECT COUNT(*) FROM signals "
            "WHERE source='gmail_acme' AND kind='email.received'"
        ).fetchone()[0]
        assert n_received == 1, (
            f"F3: expected 1 email.received signal, got {n_received}"
        )
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


# ── Fix 8: Gmail full-content (email.content) E2E test ───────────────────────

def test_gmail_full_content_ingestion_e2e(e2e_home: Path) -> None:
    """Fix 8: Gmail full-content (email.content + blob) ingestion end-to-end.

    Uses V2 credentials (gmail.readonly) so the adapter produces email.content
    signals with ContentBlob entries alongside email.received signals.

    Before Fix 8: all E2E tests used legacy (gmail.metadata) credentials.
    After Fix 8: _save_creds defaults to gmail_readonly=True (V2 scopes).
    """
    from olo.store import connect, migrate
    from olo.content import load_json_doc
    import json as _j

    home = e2e_home

    with FakeGoogleServer(email="fullcontent@example.com") as fake:
        # Seed messages BEFORE bootstrap so they appear in backfill via messages.list
        fake.gmail.add_message(
            "fc1", thread_id="tc1", labels=["INBOX"],
            subject="Full content test A",
        )
        fake.gmail.add_message(
            "fc2", thread_id="tc2", labels=["INBOX"],
            subject="Full content test B",
        )

        _save_creds(fake, "acme", home)  # V2 scopes by default after Fix 8

        r = _run(["plugins", "enable", "olo"], home)
        assert r.returncode == 0, f"plugin enable: {r.stderr}"

        # Setup Gmail source
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0, f"setup: {r.stderr}"

        job_id = None
        for line in r.stdout.splitlines():
            if "hermes cron create" in line:
                break
        r2 = _run([
            "cron", "create", "every 5m",
            "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        assert r2.returncode == 0, f"cron create: {r2.stderr}"
        job_id = _parse_job_id(r2.stdout)
        assert job_id, f"could not parse job id: {r2.stdout}"

        # Bootstrap tick (cursor=None → historyId set, 0 signals initially)
        exec1 = _cron_run_and_wait(home, job_id, expect_status="completed")

        db_path = home / "olo" / "olo.db"
        assert db_path.exists(), "DB not created"
        conn = connect(db_path)
        migrate(conn)

        from olo.store import get_checkpoint, count_signals
        cursor = get_checkpoint("gmail_acme", conn)
        n_before = count_signals("gmail_acme", conn)
        conn.close()

        assert cursor is not None, "Bootstrap must set a cursor"
        assert n_before == 0, f"Bootstrap must ingest 0 signals, got {n_before}"

        # Inject backfill cursor with the historyId captured at bootstrap
        # (uses cli backfill command)
        # Instead, manually inject since we need to test the full pipeline
        import json as _j
        import sqlite3
        conn2 = connect(db_path)
        migrate(conn2)
        backfill_cursor = _j.dumps({
            "phase": "backfill",
            "mode": "bootstrap",
            "after": 0,
            "page_token": None,
            "resume_history_id": cursor,
        })
        conn2.execute("BEGIN IMMEDIATE")
        conn2.execute(
            "UPDATE sync_checkpoints SET cursor=?,updated_at=datetime('now') WHERE source=?",
            (backfill_cursor, "gmail_acme"),
        )
        conn2.execute("COMMIT")
        conn2.close()

        # Run cron — backfill should produce email.content signals
        exec2 = _cron_run_and_wait(home, job_id, expect_status="completed")

        conn3 = connect(db_path)
        migrate(conn3)
        total = count_signals("gmail_acme", conn3)
        kinds_rows = conn3.execute(
            "SELECT kind, COUNT(*) n FROM signals WHERE source='gmail_acme' GROUP BY kind"
        ).fetchall()
        blob_n = conn3.execute(
            "SELECT COUNT(*) FROM content_blobs WHERE source='gmail_acme'"
        ).fetchone()[0]
        kinds = {r["kind"]: r["n"] for r in kinds_rows}
        conn3.close()

        # Verify email.content signals and blobs were produced
        assert kinds.get("email.content", 0) >= 1, (
            f"Fix 8 FAIL: no email.content signals. kinds={kinds}, blobs={blob_n}"
        )
        assert blob_n >= 1, (
            f"Fix 8 FAIL: no content_blobs stored. kinds={kinds}"
        )


# ── Fix 8: Three-calendar synchronization E2E test ───────────────────────────

def test_three_calendar_sync_e2e(e2e_home: Path) -> None:
    """Fix 8: Three independent calendars are all synced by a single cron job.

    Uses V2 credentials (calendar.events.readonly + calendarlist.readonly).
    After bootstrap and steady-state ticks, events from all 3 calendars must
    appear — demonstrating that calendarList discovery works and round-robin
    prevents starvation (Fix 7 / P0-A).
    """
    home = e2e_home

    CAL_A = "cal-a@example.com"
    CAL_B = "cal-b@example.com"
    CAL_C = "cal-c@example.com"

    with FakeGoogleServer(email="threecal@example.com") as fake:
        # Register three secondary calendars with independent events
        for cal_id in [CAL_A, CAL_B, CAL_C]:
            sec = fake.add_secondary_calendar_with_events(
                cal_id, f"Calendar {cal_id}",
                event_ids=[f"ev-{cal_id.split('@')[0]}"],
            )
            # Keep events in incr buffer so full sync sees them
            # (don't clear so bootstrap picks them up)

        _save_creds(fake, "acme", home)

        r = _run(["plugins", "enable", "olo"], home)
        assert r.returncode == 0

        r = _run([
            "olo", "setup", "--source", "gcal",
            "--option", "account=acme",
            "--option", "lookback_days=7",
        ], home)
        assert r.returncode == 0, f"setup: {r.stderr}"

        r2 = _run([
            "cron", "create", "every 15m",
            "--name", "olo-gcal-acme",
            "--script", "olo_monitor_gcal_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        assert r2.returncode == 0, f"cron create: {r2.stderr}"
        job_id = _parse_job_id(r2.stdout)
        assert job_id

        # Run multiple ticks — enough for calendarList discovery + all 3 calendars
        for _ in range(10):
            exec_row = _cron_run_and_wait(home, job_id, expect_status="completed")

        from olo.store import connect, migrate
        db_path = home / "olo" / "olo.db"
        conn = connect(db_path)
        migrate(conn)

        rows = conn.execute(
            "SELECT payload FROM signals WHERE source='gcal_acme' AND kind='calendar.event.changed'"
        ).fetchall()
        conn.close()

        import json as _j
        seen_cals = {_j.loads(r["payload"]).get("calendar_id") for r in rows}

        # All three secondary calendars must have been synced
        for cal_id in [CAL_A, CAL_B, CAL_C]:
            assert cal_id in seen_cals, (
                f"Fix 8 / P0-A FAIL: calendar {cal_id!r} was never synced. "
                f"seen_cals={seen_cals}"
            )
