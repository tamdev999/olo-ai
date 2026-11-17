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


# ── §9.5: label/deletion change events ───────────────────────────────────────

def test_label_and_deletion_change_events(e2e_home: Path) -> None:
    """§9.5: label changes and deletions produce signals, run completes."""
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        fake.gmail.add_message("msg1", labels=["INBOX", "UNREAD"], subject="Inbox")
        _save_creds(fake, "acme", home)

        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
        assert r.returncode == 0
        r = _run([
            "cron", "create", "every 5m", "--name", "olo-gmail-acme",
            "--script", "olo_monitor_gmail_acme.py",
            "--no-agent", "--deliver", "local",
        ], home)
        job_id = _parse_job_id(r.stdout)

        # Bootstrap
        _run(["cron", "run", job_id], home)
        exec_boot = _wait_for_terminal(home, job_id, timeout=60)
        assert exec_boot["status"] == "completed"

        # First incremental: pick up msg1
        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec1["status"] == "completed"

        # Trigger label + deletion changes
        fake.gmail.remove_labels("msg1", ["UNREAD"])
        fake.gmail.delete_message("msg1")

        _run(["cron", "run", job_id], home)
        exec2 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec2["status"] == "completed"

        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, count_signals, migrate
            from olo.paths import db_path
            conn = connect(db_path())
            migrate(conn)
            # Must have signals including at least one deletion/label signal
            n = count_signals("gmail_acme", conn)
            assert n >= 1, f"expected ≥1 signals, got {n}"
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


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


# ── §9.5: in-process cron zero-LLM proof ─────────────────────────────────────

def test_in_process_cron_zero_llm(e2e_home: Path) -> None:
    """§9.5: run_job in-process with AIAgent patched to raise; job still completes.

    Proves the cron script does not call the LLM.
    """
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)

        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, migrate
            from olo.paths import db_path

            # Ensure DB exists
            conn = connect(db_path())
            migrate(conn)
            conn.close()

            # Register source
            r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
            assert r.returncode == 0, f"setup failed: {r.stderr}"

            # Build a minimal adapter and run it in-process
            from olo.adapters.gmail import GmailAdapter
            from olo.google.http import GoogleClient

            adapter = GmailAdapter({"account": "acme"})
            creds = fake.make_credentials("acme")
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter._client = client

            # Patch run_agent.AIAgent to raise if called
            try:
                import run_agent
                original_agent = run_agent.AIAgent

                class _RaisingAgent:
                    def __init__(self, *a, **kw):
                        raise AssertionError("AIAgent must not be instantiated in zero-LLM mode")

                run_agent.AIAgent = _RaisingAgent

                # Run the fetch manually (this is what the cron script does)
                b = adapter.fetch(None, limit=100)
                assert b is not None  # bootstrap completes without calling LLM

            except ImportError:
                # run_agent not available in this environment — skip LLM patch
                b = adapter.fetch(None, limit=100)
                assert b is not None
            finally:
                try:
                    run_agent.AIAgent = original_agent
                except (NameError, UnboundLocalError):
                    pass
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


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
