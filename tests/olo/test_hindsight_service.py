"""Hindsight supervision (olo.hindsight_service, post-audit batch 4).

The decision table runs against real child processes (a sleeping Python
stands in for the server; the probe is injected), so process identity,
termination and restarts are real on every host.  On POSIX the whole loop
also runs against a stand-in ``hindsight-api`` executable over HTTP.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psutil
import pytest

from olo.hindsight_service import (
    Probe,
    ServiceSpec,
    configure,
    load_spec,
    migrate,
    probe,
    service_status,
    start,
    stop,
    supervise_once,
)
from olo.store import connect
from olo.store import migrate as migrate_store
from tests.waiting import wait_until

T0 = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "olo.db")
    migrate_store(c)
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def children():
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(10)


def spec(tmp_path: Path, executable: Path | None = None, port: int = 1, key: str = "k") -> ServiceSpec:
    home = tmp_path / "businesses" / "biz"
    home.mkdir(parents=True, exist_ok=True)
    exe = executable or Path(sys.executable)
    return ServiceSpec(business_id="biz", bank_id="olo-biz", home=home, executable=exe,
                       port=port, api_key=key)


def spawner(children, code: str = "import time; time.sleep(120)"):
    calls: list[int] = []

    def spawn(_spec):
        p = subprocess.Popen([sys.executable, "-c", code])
        children.append(p)
        calls.append(p.pid)
        return p.pid, psutil.Process(p.pid).create_time()
    return spawn, calls


def answers(*statuses: str):
    seq = iter(statuses)
    return lambda _spec: Probe(next(seq))


def events(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT event FROM hindsight_service_events ORDER BY id")]


class TestSupervision:
    def test_a_healthy_server_is_left_alone(self, conn, tmp_path, children) -> None:
        spawn, calls = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        assert supervise_once(conn, spec(tmp_path), T0 + timedelta(minutes=1),
                              check=answers("healthy"), spawn=spawn) == "healthy"
        assert len(calls) == 1 and events(conn) == ["started"]

    def test_a_dead_server_is_restarted_at_once(self, conn, tmp_path, children) -> None:
        spawn, calls = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        children[0].kill()
        children[0].wait(10)
        status = supervise_once(conn, spec(tmp_path), T0 + timedelta(minutes=5),
                                check=answers("down"), spawn=spawn)
        assert status == "restarting" and len(calls) == 2
        assert events(conn) == ["started", "restarted"]
        assert service_status(conn)["alive"] is True

    def test_a_hung_server_is_restarted_after_repeated_failures(self, conn, tmp_path, children) -> None:
        spawn, _ = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        first = children[0]
        late = T0 + timedelta(minutes=10)
        got = [supervise_once(conn, spec(tmp_path), late + timedelta(minutes=i),
                              check=answers("down"), spawn=spawn) for i in range(3)]
        assert got == ["unhealthy", "unhealthy", "restarting"]
        assert first.wait(15) is not None              # the hung process was stopped
        assert events(conn) == ["started", "stopped", "restarted"]

    def test_a_starting_server_gets_a_grace_period(self, conn, tmp_path, children) -> None:
        spawn, calls = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        assert supervise_once(conn, spec(tmp_path), T0 + timedelta(seconds=40),
                              check=answers("down"), spawn=spawn) == "starting"
        assert len(calls) == 1

    def test_a_crash_loop_stops_restarting(self, conn, tmp_path, children) -> None:
        spawn, calls = spawner(children, code="pass")        # exits at once
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        statuses = []
        for i in range(8):
            wait_until(lambda: children[-1].poll() is not None, timeout=15)
            statuses.append(supervise_once(conn, spec(tmp_path), T0 + timedelta(minutes=5 + i),
                                           check=answers("down"), spawn=spawn))
        assert statuses[:5] == ["restarting"] * 5
        assert statuses[5:] == ["crashloop"] * 3
        assert len(calls) == 6 and events(conn).count("crashloop") == 1

    @pytest.mark.parametrize("bad", ["tenant_auth_off", "wrong_key"])
    def test_a_misconfigured_server_is_never_restarted(self, conn, tmp_path, children, bad) -> None:
        spawn, calls = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        for i in range(2):
            assert supervise_once(conn, spec(tmp_path), T0 + timedelta(minutes=10 + i),
                                  check=answers(bad), spawn=spawn) == "misconfigured"
        assert len(calls) == 1 and events(conn) == ["started", "misconfigured"]

    def test_an_operator_stop_is_respected(self, conn, tmp_path, children) -> None:
        spawn, calls = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        assert stop(conn, T0.isoformat(), reason="maintenance") is True
        assert supervise_once(conn, spec(tmp_path), T0 + timedelta(minutes=5),
                              check=answers("down"), spawn=spawn) == "stopped"
        assert len(calls) == 1

    def test_a_server_olo_did_not_start_is_adopted(self, conn, tmp_path, children) -> None:
        spawn, calls = spawner(children)
        assert supervise_once(conn, spec(tmp_path), T0, check=answers("healthy"),
                              spawn=spawn) == "adopted"
        assert calls == [] and events(conn) == ["adopted"]

    def test_a_restart_makes_the_projection_reconcile(self, conn, tmp_path, children) -> None:
        from olo.projection import _restart_since
        spawn, _ = spawner(children)
        start(conn, spec(tmp_path), T0.isoformat(), spawn=spawn)
        assert _restart_since(conn, "olo-biz", (T0 - timedelta(hours=1)).isoformat())
        assert not _restart_since(conn, "olo-biz", (T0 + timedelta(hours=1)).isoformat())


def test_configuration_uses_setup_sh_port_key_and_venv(conn, tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "businesses" / "biz").mkdir(parents=True)
    (home / "hindsight_port_registry.json").write_text(
        json.dumps({"biz": {"port": 18901, "bank_id": "olo-biz"}}), encoding="utf-8")
    (home / "businesses" / "biz" / ".env").write_text(
        "HINDSIGHT_API_TENANT_API_KEY=tenant-key\n", encoding="utf-8")
    configure(conn, "biz", str(tmp_path / "venv"), T0.isoformat())
    s = load_spec(conn, home)
    assert (s.port, s.bank_id, s.api_key, s.home) == (18901, "olo-biz", "tenant-key",
                                                      home / "businesses" / "biz")
    assert s.executable == tmp_path / "venv" / "bin" / "hindsight-api"
    conn.execute("INSERT INTO olo_bindings (name, value, bound_at) VALUES ('business_id','other','x')")
    conn.commit()
    with pytest.raises(ValueError, match="belongs to business"):
        configure(conn, "biz", str(tmp_path / "venv"), T0.isoformat())


# ── The real loop against a stand-in executable (POSIX: setsid, exec) ────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install(tmp_path: Path, *, auth: str = "on", key: str = "tenant-key") -> ServiceSpec:
    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    exe = venv / "hindsight-api"
    fake = Path(__file__).with_name("fake_hindsight_api.py")
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    s = spec(tmp_path, executable=exe, port=_free_port(), key=key)
    (s.home / ".env").write_text(f"HINDSIGHT_API_TENANT_API_KEY=tenant-key\nFAKE_TENANT_AUTH={auth}\n",
                                 encoding="utf-8")
    return s


@pytest.mark.platforms("posix")
def test_the_supervisor_restarts_a_killed_server(conn, tmp_path) -> None:
    s = _install(tmp_path)
    try:
        start(conn, s, T0.isoformat())
        wait_until(lambda: probe(s).status == "healthy", timeout=30, what="server up")
        assert supervise_once(conn, s, datetime.now(UTC)) == "healthy"
        first = service_status(conn)["pid"]
        psutil.Process(first).kill()
        psutil.Process(first).wait(10)
        assert supervise_once(conn, s, datetime.now(UTC)) == "restarting"
        wait_until(lambda: probe(s).status == "healthy", timeout=30, what="server back")
        assert service_status(conn)["pid"] != first
        assert events(conn) == ["started", "restarted"]
        assert (s.home / "hindsight.log").exists()
    finally:
        stop(conn, datetime.now(UTC).isoformat(), reason="test teardown")


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("auth, key, expected", [("off", "tenant-key", "tenant_auth_off"),
                                                 ("on", "another-business", "wrong_key")])
def test_the_probe_catches_a_misconfigured_server(conn, tmp_path, auth, key, expected) -> None:
    s = _install(tmp_path, auth=auth, key=key)
    try:
        start(conn, s, T0.isoformat())
        wait_until(lambda: probe(s).status != "down", timeout=30, what="server up")
        assert probe(s).status == expected
        assert supervise_once(conn, s, datetime.now(UTC)) == "misconfigured"
    finally:
        stop(conn, datetime.now(UTC).isoformat(), reason="test teardown")
