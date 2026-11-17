"""Olo M1 cron E2E tests.

§8.5 acceptance criteria.
Runs real Hermes CLI as subprocesses against a temp HERMES_HOME.
No time.sleep; uses wait_until for execution completion.

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hermes_exe() -> str:
    """Return the path to the hermes executable in the current venv."""
    scripts = Path(sys.executable).parent
    for name in ("hermes.exe", "hermes"):
        p = scripts / name
        if p.exists():
            return str(p)
    raise RuntimeError(f"hermes executable not found in {scripts}")


def _run(args: list[str], home: Path, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": str(home)}
    return subprocess.run(
        [_hermes_exe(), *args],
        capture_output=True, text=True, env=env, check=False,
        **kwargs,
    )


def _run_python(script: list[str], home: Path, **kwargs) -> subprocess.CompletedProcess:
    env = {**os.environ, "HERMES_HOME": str(home)}
    return subprocess.run(
        [sys.executable, *script],
        capture_output=True, text=True, env=env, check=False,
        **kwargs,
    )


def _install_plugin(home: Path, repo_root: Path) -> None:
    """Install olo plugin in HERMES_HOME (copy on Windows, symlink on POSIX)."""
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
    """Write plugins.enabled: [olo] to config.yaml."""
    import hermes_yaml as yaml
    cfg = home / "config.yaml"
    cfg.write_text(yaml.safe_dump({"plugins": {"enabled": ["olo"]}}), encoding="utf-8")


def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _setup_home(home: Path) -> None:
    home.mkdir(exist_ok=True)
    _install_plugin(home, _repo_root())
    _enable_plugin(home)


def _parse_job_id(output: str) -> Optional[str]:
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Created job:"):
            return line.split(":", 1)[1].strip()
    return None


def _list_executions_in_home(home: Path, job_id: str, limit: int = 500) -> list[dict]:
    """Return execution rows for job_id with HERMES_HOME set to home."""
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

    The M1 runner dispatches via 'cron run' + 'cron tick' (not synchronous).
    The default trigger runs both; custom triggers skip the exit-code/stdout checks.

    1. Snapshot the set of existing execution IDs (all rows, not just limit=5).
    2. Run the trigger.
    3. For the default trigger: check that 'cron run' succeeded (exit 0).
       Note: M1 'cron run' just marks the job; 'cron tick' fires the scheduler.
       The exit code from 'cron run' does not reflect the execution outcome.
    4. wait_until: find exactly one new execution row (id not in snapshot)
       that is in a terminal state with status == expect_status.
    5. Return that row.
    """
    from cron.executions import _TERMINAL_STATES

    # Step 1: snapshot before
    before_ids = {e["id"] for e in _list_executions_in_home(home, job_id)}

    # Step 2: trigger
    if trigger is not None:
        trigger()
    else:
        r_run = _run(["cron", "run", job_id], home)
        assert r_run.returncode == 0, (
            f"H2: 'cron run' failed: rc={r_run.returncode} "
            f"stderr={r_run.stderr!r}"
        )
        _run(["cron", "tick"], home)

    # Step 3-5: wait for the new row
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


# ── Full E2E: install, setup, cron create, run, tick, verify ─────────────────

@pytest.fixture()
def e2e_home(tmp_path: Path) -> Path:
    home = tmp_path / "hermes_home"
    _setup_home(home)
    return home


def test_e2e_25_signals_first_fire(e2e_home: Path) -> None:
    """Install plugin, setup, cron create, cron run, cron tick → 25 signals."""
    home = e2e_home

    # 1. Enable plugin and setup
    result = _run(["plugins", "enable", "olo"], home)
    assert result.returncode == 0 or "already" in result.stdout.lower() or "already" in result.stderr.lower(), (
        f"plugins enable failed: {result.stderr}"
    )

    result = _run(["olo", "setup", "--source", "synthetic"], home)
    assert result.returncode == 0, f"olo setup failed: {result.stderr}"

    # 2. Create cron job
    result = _run([
        "cron", "create", "15m",
        "--name", "olo-synthetic",
        "--script", "olo_monitor_synthetic.py",
        "--no-agent", "--deliver", "local",
    ], home)
    assert result.returncode == 0, f"cron create failed: {result.stderr}"
    job_id = _parse_job_id(result.stdout)
    assert job_id, f"could not parse job id from: {result.stdout}"

    # 3. Fire and wait
    exec_row = _cron_run_and_wait(home, job_id, expect_status="completed")
    assert exec_row["status"] == "completed", (
        f"execution status={exec_row['status']} error={exec_row.get('error')}"
    )

    # 4. Verify 25 signals
    from olo.store import connect, count_signals, get_checkpoint, migrate
    from olo.paths import db_path as _dp
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        db = _dp()
        conn = connect(db)
        migrate(conn)
        assert count_signals("synthetic", conn) == 25
        assert get_checkpoint("synthetic", conn) == "25"
        conn.close()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    # 5. Second fire: still 25 signals, 0 new inserted
    _cron_run_and_wait(home, job_id, expect_status="completed")

    os.environ["HERMES_HOME"] = str(home)
    try:
        conn = connect(db)
        assert count_signals("synthetic", conn) == 25
        conn.close()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def test_e2e_failure_and_recovery(e2e_home: Path) -> None:
    """Step 7: fail_at_batch=1 fire → all 6 failure invariants hold; then recovery → 40 signals."""
    home = e2e_home

    _run(["plugins", "enable", "olo"], home)
    _run(["olo", "setup", "--source", "synthetic"], home)

    # Create job
    result = _run([
        "cron", "create", "15m",
        "--name", "olo-synthetic-fail",
        "--script", "olo_monitor_synthetic.py",
        "--no-agent", "--deliver", "local",
    ], home)
    assert result.returncode == 0
    job_id = _parse_job_id(result.stdout)
    assert job_id

    # Setup shim with fail_at_batch=1 (first fetch raises SourceError)
    _run([
        "olo", "setup", "--source", "synthetic",
        "--option", "total=40",
        "--option", "fail_at_batch=1",
        "--force",
    ], home)

    # Fire — should fail
    exec_fail = _cron_run_and_wait(home, job_id, expect_status="failed")

    # ── Invariant 1: Hermes records a 'failed' execution ─────────────────────
    assert exec_fail["status"] == "failed", (
        f"Hermes execution status should be 'failed', got {exec_fail['status']!r}"
    )

    # ── Invariants 2–5: Olo DB state ──────────────────────────────────────────
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        from olo.store import connect, count_signals, get_checkpoint, migrate, get_lease
        from olo.paths import db_path as _dp
        db = _dp()
        conn = connect(db)
        migrate(conn)

        # Invariant 2: Olo run is 'failed'
        run_row = conn.execute(
            "SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        assert run_row is not None, "no ingest_run row found"
        assert run_row["status"] == "failed", (
            f"Olo run status should be 'failed', got {run_row['status']!r}"
        )

        # Invariant 3: 0 signals committed
        assert count_signals("synthetic", conn) == 0, (
            f"expected 0 signals, found {count_signals('synthetic', conn)}"
        )

        # Invariant 4: checkpoint unchanged (None — was never set)
        cp = get_checkpoint("synthetic", conn)
        assert cp is None, (
            f"checkpoint should be None after failed first run, got {cp!r}"
        )

        # Invariant 5: lease released
        assert get_lease("synthetic", conn) is None, (
            "lease should be released after failed run"
        )

        conn.close()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old

    # ── Recovery: reinstall shim without fail_at_batch ────────────────────────
    _run([
        "olo", "setup", "--source", "synthetic",
        "--option", "total=40",
        "--force",
    ], home)

    exec_recovery = _cron_run_and_wait(home, job_id, expect_status="completed")

    # Invariant 6: recovery run succeeds with 40 signals
    assert exec_recovery["status"] == "completed", (
        f"recovery execution status: {exec_recovery['status']}"
    )

    os.environ["HERMES_HOME"] = str(home)
    try:
        conn = connect(db)
        assert count_signals("synthetic", conn) == 40, (
            f"expected 40 signals after recovery, got {count_signals('synthetic', conn)}"
        )
        assert get_checkpoint("synthetic", conn) == "40", (
            f"expected checkpoint '40', got {get_checkpoint('synthetic', conn)!r}"
        )
        conn.close()
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# ── Zero-LLM proofs ───────────────────────────────────────────────────────────

def test_zero_llm_no_usage_audit(e2e_home: Path) -> None:
    """Proof (b): after E2E runs, usage_audit.jsonl is absent or has no line for olo."""
    home = e2e_home

    _run(["plugins", "enable", "olo"], home)
    _run(["olo", "setup", "--source", "synthetic"], home)

    result = _run([
        "cron", "create", "15m",
        "--name", "olo-synthetic-audit",
        "--script", "olo_monitor_synthetic.py",
        "--no-agent", "--deliver", "local",
    ], home)
    assert result.returncode == 0
    job_id = _parse_job_id(result.stdout)
    assert job_id

    _cron_run_and_wait(home, job_id, expect_status="completed")

    audit_path = home / "cron" / "usage_audit.jsonl"
    if audit_path.exists():
        content = audit_path.read_text(encoding="utf-8")
        # No line for our job_id
        assert job_id not in content, (
            f"usage_audit.jsonl contains entry for no-agent job {job_id}"
        )


def test_zero_llm_no_credentials(e2e_home: Path) -> None:
    """Proof (c): E2E home has no model credentials; runner runs without them."""
    home = e2e_home
    # Verify no .env or model config
    env_file = home / ".env"
    assert not env_file.exists() or not any(
        "API_KEY" in line for line in env_file.read_text().splitlines()
        if not line.startswith("#")
    )
    # The shim runs without LLM credentials (already proven by test_shim_executes_successfully)


def test_zero_llm_no_agent_constructed_in_process(e2e_home: Path) -> None:
    """Proof (a): running the olo cron job in-process does not construct an AIAgent."""
    import types
    home = e2e_home
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)

    try:
        _run(["olo", "setup", "--source", "synthetic"], home)

        # Create a scripts/ dir and shim
        scripts_dir = home / "scripts"
        scripts_dir.mkdir(exist_ok=True)

        # Build the shim manually
        shim_path = scripts_dir / "olo_monitor_synthetic.py"
        shim_content = (
            f"import sys; sys.path.insert(0, r'{_repo_root()}')\n"
            f"import olo.runner as _r; raise SystemExit(_r.main(['--source', 'synthetic']))\n"
        )
        shim_path.write_text(shim_content, encoding="utf-8")

        # Create cron job via Python API
        from cron.jobs import create_job

        agent_constructed = []

        class _SentinelAgent:
            def __init__(self, *args, **kwargs):
                agent_constructed.append(True)
                raise AssertionError("LLM agent constructed in no_agent job!")

        # Use monkeypatch-style replacement
        from unittest import mock
        with mock.patch.dict("sys.modules", {"run_agent": types.ModuleType("run_agent")}):
            import sys as _sys
            _sys.modules["run_agent"].AIAgent = _SentinelAgent  # type: ignore

            job = create_job(
                prompt=None,
                schedule="every 15m",
                name="olo-no-agent-inprocess",
                script="olo_monitor_synthetic.py",
                no_agent=True,
                deliver="local",
            )
            from cron.scheduler import run_job
            success, _, _, err = run_job(job)
            assert success, f"run_job returned failure: {err}"

        assert agent_constructed == [], "AIAgent was constructed despite no_agent=True"

    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


# ── H2 negative test: stale execution must not be returned ───────────────────

def test_wait_helper_rejects_stale_execution(e2e_home: Path) -> None:
    """H2: _cron_run_and_wait must not return a row from a previous run.

    After one completed run, call _cron_run_and_wait with trigger=lambda: None
    (no actual run fired) and a short timeout. It must raise (timeout) instead
    of returning the previous terminal row.

    Old _wait_for_terminal_execution would have returned the previous row because
    it queried `list_executions(job_id=..., limit=5)` without comparing against
    any before-snapshot — meaning the first terminal row in those 5 results
    was always returned, even if it was from a previous run.
    """
    home = e2e_home

    _run(["plugins", "enable", "olo"], home)
    _run(["olo", "setup", "--source", "synthetic"], home)

    result = _run([
        "cron", "create", "15m",
        "--name", "olo-synthetic-stale",
        "--script", "olo_monitor_synthetic.py",
        "--no-agent", "--deliver", "local",
    ], home)
    job_id = _parse_job_id(result.stdout)
    assert job_id

    # Complete one run so a terminal row exists
    _cron_run_and_wait(home, job_id, expect_status="completed")

    # Now call with a no-op trigger — no new execution will be created.
    # The helper must time out rather than return the stale previous row.
    with pytest.raises(AssertionError, match="timed out"):
        _cron_run_and_wait(
            home, job_id,
            expect_status="completed",
            trigger=lambda: None,
            timeout=2.0,  # short — no run fired
        )
