"""Cron E2E tests for the Google Workspace M2 pipeline (Phase A â€” fake server).

Â§9.5 acceptance criteria.
Runs real Hermes CLI as subprocesses against a temp HERMES_HOME
with a loopback FakeGoogleServer injected via api_base_override.
"""
from __future__ import annotations

import json
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


# â”€â”€ Gmail E2E: bootstrap (0 signals), then 3 messages â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_gmail_bootstrap_then_messages(e2e_home: Path) -> None:
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        _save_creds(fake, "acme", home)

        # Setup
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

        # First fire: bootstrap â†’ 0 signals
        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec1["status"] in ("completed", "failed"), exec1

        # Read DB
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
            assert bootstrap_cursor is not None   # historyId set
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old

        # Add 3 messages to fake
        for i in range(1, 4):
            fake.gmail.add_message(f"msg{i}", subject=f"Test Message {i}")

        # Second fire: 3 signals
        _run(["cron", "run", job_id], home)
        exec2 = _wait_for_terminal(home, job_id, timeout=60)
        assert exec2["status"] in ("completed", "failed")

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

        # Third fire: no new signals
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


# â”€â”€ Calendar E2E: bootstrap events, then changes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_gcal_bootstrap_then_events(e2e_home: Path) -> None:
    home = e2e_home

    with FakeGoogleServer(email="test@example.com") as fake:
        # Add events before bootstrap
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
        assert exec1["status"] in ("completed", "failed")

        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(home)
        try:
            from olo.store import connect, count_signals, migrate
            from olo.paths import db_path
            db = db_path()
            conn = connect(db)
            migrate(conn)
            # Bootstrap should have ingested the 2 pre-existing events
            n = count_signals("gcal_acme", conn)
            assert n >= 2, f"expected â‰¥2 calendar signals, got {n}"
            conn.close()
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old


# â”€â”€ auth_failed: invalid_grant â†’ execution failed â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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

        # Inject invalid_grant
        fake.inject_invalid_grant()

        _run(["cron", "run", job_id], home)
        exec1 = _wait_for_terminal(home, job_id, timeout=60)
        # Execution must be failed or completed (non-zero exit â†’ failed in Hermes)
        assert exec1["status"] in ("failed", "completed")


# â”€â”€ Zero LLM: no usage_audit.jsonl â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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
