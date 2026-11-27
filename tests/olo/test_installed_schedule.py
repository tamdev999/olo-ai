"""The installed schedule (post-audit batch 5): `hermes olo setup --schedule`.

Real Hermes CLI subprocesses against a temporary HERMES_HOME and the real
Hermes cron store and runner.  With ``--enable-projection`` setup installs
the Hindsight supervisor shim and its every-minute job beside the source's
job; jobs are idempotent by name and pin the interpreter that ran setup;
shims run the checkout that installed them even when another ``olo`` comes
first on ``sys.path``.  On POSIX the supervisor job drives a stand-in
``hindsight-api`` through start, forced termination, restart and a crash
loop, all through ``hermes cron run``.  No network, no paid calls.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests.olo.fake_google import FakeGoogleServer
from tests.olo.test_google_cron_e2e import (
    _cron_run_and_wait,
    _enable_plugin,
    _install_plugin,
    _repo_root,
    _run,
    _save_creds,
)
from tests.waiting import wait_until

pytestmark = pytest.mark.not_integration

BIZ, BANK, TENANT_KEY = "biz", "olo-biz", "tenant-key-for-tests-only"


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes_home"
    h.mkdir()
    _install_plugin(h, _repo_root())
    _enable_plugin(h)
    return h


def _jobs(home: Path) -> dict[str, dict]:
    out = subprocess.run(
        [sys.executable, "-c", "import json; from cron.jobs import list_jobs;"
                               " print(json.dumps(list_jobs(include_disabled=True)))"],
        capture_output=True, text=True, check=True, cwd=str(_repo_root()),
        env={**os.environ, "HERMES_HOME": str(home)})
    return {j["name"]: j for j in json.loads(out.stdout)}


def _business(home: Path, port: int) -> Path:
    """What setup.sh leaves for a business: port registry and tenant key."""
    registry = home / "hindsight_port_registry.json"
    registry.write_text(json.dumps({BIZ: {"port": port, "bank_id": BANK}}), encoding="utf-8")
    env = home / "businesses" / BIZ / ".env"
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(f"HINDSIGHT_API_TENANT_API_KEY={TENANT_KEY}\nFAKE_TENANT_AUTH=on\n",
                   encoding="utf-8")
    return registry


def _setup(home: Path, registry: Path, venv: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run(["olo", "setup", "--source", "gmail", "--option", "account=acme",
                 "--enable-projection", "--business-id", BIZ, "--bank-id", BANK,
                 "--projection-max-docs", "20", "--projection-max-prose-docs", "8",
                 "--projection-budget-usd", "1.0", "--hindsight-registry", str(registry),
                 "--hindsight-venv", str(venv), *extra], home)


def test_plain_setup_schedules_nothing(home: Path) -> None:
    with FakeGoogleServer() as google:
        _save_creds(google, "acme", home)
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme"], home)
    assert r.returncode == 0, r.stderr
    assert "hermes cron create" in r.stdout and "olo-hindsight" not in r.stdout
    assert not list((home / "scripts").glob("olo_hindsight_*.py"))
    assert _jobs(home) == {}


def test_setup_installs_both_jobs_idempotently(home: Path, tmp_path: Path) -> None:
    with FakeGoogleServer() as google:
        _save_creds(google, "acme", home)
        registry = _business(home, 18999)
        r = _setup(home, registry, tmp_path / "venv", "--schedule")
        assert r.returncode == 0, r.stderr
        jobs = _jobs(home)
        assert set(jobs) == {"olo-gmail-acme", f"olo-hindsight-{BIZ}"}
        sup = jobs[f"olo-hindsight-{BIZ}"]
        assert (sup["script"], sup["schedule_display"], sup["no_agent"]) == (
            f"olo_hindsight_{BIZ}.py", "every 1m", True)
        interpreters = {j["interpreter"] for j in jobs.values()}
        assert len(interpreters) == 1 and Path(next(iter(interpreters))).is_file()
        again = _setup(home, registry, tmp_path / "venv", "--schedule", "--force")
        assert again.returncode == 0, again.stderr
        assert again.stdout.count(": unchanged [") == 2
        assert {n: j["id"] for n, j in _jobs(home).items()} == {n: j["id"] for n, j in jobs.items()}


def test_a_shim_runs_its_own_checkout_before_any_other_olo(home: Path, tmp_path: Path) -> None:
    """An ``olo`` earlier on sys.path (an editable install of another copy,
    a stale PYTHONPATH) is never what a scheduled shim runs."""
    r = _run(["olo", "setup", "--source", "synthetic"], home)
    assert r.returncode == 0, r.stderr
    decoy = tmp_path / "decoy"
    (decoy / "olo").mkdir(parents=True)
    marker = tmp_path / "decoy-ran"
    (decoy / "olo" / "__init__.py").write_text(
        f"open({str(marker)!r}, 'w').write('stale copy')\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(home / "scripts" / "olo_monitor_synthetic.py")],
        capture_output=True, text=True, check=False,
        env={**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(decoy)})
    assert proc.returncode == 0, proc.stderr
    assert not marker.exists()


# ── The supervisor job through hermes cron (POSIX: a stand-in executable) ────

def _install_server(tmp_path: Path, *, crash: bool = False) -> Path:
    venv = tmp_path / ("crashing-venv" if crash else "venv")
    (venv / "bin").mkdir(parents=True)
    exe = venv / "bin" / "hindsight-api"
    if crash:
        exe.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    else:
        fake = Path(__file__).with_name("fake_hindsight_api.py")
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    return venv


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthy(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return resp.status == 200
    except OSError:
        return False


def _server_pid(home: Path) -> int | None:
    out = subprocess.run(
        [sys.executable, "-c", "from olo.paths import db_path; from olo.store import connect;"
                               "from olo.hindsight_service import service_status;"
                               "print(service_status(connect(db_path()))['pid'])"],
        capture_output=True, text=True, check=True, cwd=str(_repo_root()),
        env={**os.environ, "HERMES_HOME": str(home)})
    return None if out.stdout.strip() == "None" else int(out.stdout)


def _service(home: Path) -> dict:
    """The supervisor's state and recent events, for failure messages."""
    out = subprocess.run(
        [sys.executable, "-c", "import json; from olo.paths import db_path; from olo.store import connect;"
                               "from olo.hindsight_service import service_status;"
                               "print(json.dumps(service_status(connect(db_path()), limit=20), default=str))"],
        capture_output=True, text=True, check=True, cwd=str(_repo_root()),
        env={**os.environ, "HERMES_HOME": str(home)})
    return json.loads(out.stdout)


def _exited(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass   # kills the server cron started (detached, by design)
def test_the_supervisor_job_starts_restarts_and_reports_a_crash_loop(home: Path, tmp_path: Path) -> None:
    port = _free_port()
    with FakeGoogleServer() as google:
        _save_creds(google, "acme", home)
        registry = _business(home, port)
        r = _setup(home, registry, _install_server(tmp_path), "--schedule")
        assert r.returncode == 0, r.stderr
    job = _jobs(home)[f"olo-hindsight-{BIZ}"]["id"]
    try:
        _cron_run_and_wait(home, job, expect_status="completed")         # starts it
        wait_until(lambda: _healthy(port), timeout=30, what="server up")
        _cron_run_and_wait(home, job, expect_status="completed")         # healthy: silent
        first = _server_pid(home)
        psutil.Process(first).kill()
        psutil.Process(first).wait(10)
        _cron_run_and_wait(home, job, expect_status="completed")         # restarts it
        wait_until(lambda: _healthy(port), timeout=30, what="server back")
        assert _server_pid(home) not in (None, first)
        status = _run(["olo", "status"], home)
        assert "Hindsight: " in status.stdout and "last_event=restarted" in status.stdout
    finally:
        _run(["olo", "hindsight", "stop"], home)

    # The same job against a server that dies at once: a crash loop, reported.
    crashing = _install_server(tmp_path, crash=True)
    r = _setup(home, registry, crashing, "--schedule", "--force")
    assert r.returncode == 0, r.stderr
    _run(["olo", "hindsight", "start"], home)
    # Each run restarts the dead server until the hourly restart budget (which
    # already holds the restart above) is spent; then the job fails.
    outcomes = []
    for _ in range(8):
        # The server "dies at once": wait until it has, so each run sees a dead
        # process (a still-running one is treated as hung, not crashed).
        pid = _server_pid(home)
        if pid:
            wait_until(lambda: _exited(pid), timeout=30, what="crashed server gone")
        ran = _run(["cron", "run", job], home)
        outcomes.append("failed" if "Ran now: failed" in ran.stdout else "completed")
        if outcomes[-1] == "failed":
            break
    assert outcomes[-1] == "failed" and outcomes.count("completed") >= 1, json.dumps(
        {"outcomes": outcomes, "service": _service(home)}, default=str)
    status = _run(["olo", "status"], home)
    assert "[DEGRADED] hindsight: Hindsight crashloop" in status.stdout
    assert _run(["cron", "run", job], home).stdout.count("Ran now: failed") == 1   # stays failed


def test_status_reports_an_unrun_supervisor_only_once_it_is_scheduled(home: Path, tmp_path: Path) -> None:
    """A business whose Hindsight Olo does not supervise is not 'stalled';
    a scheduled supervisor that never ran is."""
    with FakeGoogleServer() as google:
        _save_creds(google, "acme", home)
        registry = _business(home, 18998)
        assert _setup(home, registry, tmp_path / "venv").returncode == 0      # no --schedule
        quiet = _run(["olo", "status"], home).stdout
        assert "supervisor configured, never run" in quiet and "[STALLED] hindsight" not in quiet
        assert _setup(home, registry, tmp_path / "venv", "--schedule", "--force").returncode == 0
    stalled = _run(["olo", "status"], home).stdout
    assert "[STALLED] hindsight: Hindsight supervisor job is scheduled but has never run" in stalled


def test_a_no_model_installation_needs_no_key_and_runs(home: Path) -> None:
    """--extract-max-calls 0 (calendar commitments only) installs with no
    DeepSeek key anywhere, with bounded pages, and its cron job runs."""
    with FakeGoogleServer() as google:
        _save_creds(google, "acme", home)
        google.gmail.add_message("m1", labels=["SENT"], subject="hello", from_=google.email,
                                 to="Alice <alice@customer.test>")
        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme",
                  "--enable-commitments", "--extract-max-calls", "0", "--business-id", BIZ,
                  "--own-email", google.email, "--limit", "5", "--max-batches", "3",
                  "--schedule"], home)
        assert r.returncode == 0, r.stderr
        job = _jobs(home)["olo-gmail-acme"]["id"]
        _cron_run_and_wait(home, job, expect_status="completed")
        _cron_run_and_wait(home, job, expect_status="completed")
