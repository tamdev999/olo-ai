"""olo/hindsight_service.py — start, supervise and restart a business's Hindsight.

One Hindsight server per business (setup.sh: its own home, database, port
and tenant key).  This module owns that process:

* ``configure`` records how to start it (the shared venv, the business home
  from setup.sh, an optional owner-only LLM key file, an optional model
  cache) in olo.db; the tenant key and port stay where setup.sh put them;
* ``start`` / ``stop`` run it detached (its own session), cwd = the business
  home (Hindsight reads ``./.env``), the LLM key passed through the
  environment only — never argv, never the business ``.env``;
* ``supervise_once`` is one pass of a supervisor, run every minute by cron
  or in a loop (``hermes olo hindsight supervise --loop``).

A pass probes the server, then acts:

  =========================  ===================================================
  probe                      action
  =========================  ===================================================
  healthy                    none (a server Olo did not start is adopted, once)
  tenant auth off / wrong    ``misconfigured``: never restarted — another
  key                        business's server may hold the port, and a
                             restart cannot fix configuration
  stopped by an operator     none (``start`` again)
  down, within start grace   ``starting``
  down / unhealthy           restart after ``unhealthy_after`` failed passes
                             (a dead process at once); at most ``max_restarts``
                             per ``restart_window``, then ``crashloop`` until an
                             operator starts it again
  =========================  ===================================================

Timestamps are canonical UTC (olo.signals.format_utc), like the projection
stage's, so they order correctly as text.

Process identity is the recorded pid *and* its creation time (psutil), never
a command-line match.  Every start, restart, stop and condition is an event
in ``hindsight_service_events``; the projection stage reconciles the bank
after a restart (olo.hindsight_reconcile).
"""
from __future__ import annotations

import sqlite3
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

__all__ = ["Probe", "ServiceSpec", "configure", "migrate", "probe", "service_status",
           "start", "stop", "supervise_once"]

START_GRACE = timedelta(minutes=3)
UNHEALTHY_AFTER = 3
MAX_RESTARTS = 5
RESTART_WINDOW = timedelta(hours=1)

_DDL = """\
CREATE TABLE IF NOT EXISTS hindsight_service (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    business_id   TEXT NOT NULL,
    venv          TEXT NOT NULL,
    key_file      TEXT,
    hf_home       TEXT,
    configured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hindsight_service_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    pid                  INTEGER,
    create_time          REAL,
    started_at           TEXT,
    status               TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_check_at        TEXT,
    last_healthy_at      TEXT,
    detail               TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hindsight_service_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    event   TEXT NOT NULL,   -- started | restarted | stopped | adopted | unhealthy |
                             -- crashloop | misconfigured | healthy
    detail  TEXT NOT NULL DEFAULT ''
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


@dataclass(frozen=True)
class ServiceSpec:
    business_id: str
    bank_id: str
    home: Path            # the business home setup.sh created (holds .env)
    executable: Path
    port: int
    api_key: str = ""
    key_file: Optional[str] = None
    hf_home: Optional[str] = None

    @property
    def log_path(self) -> Path:
        return self.home / "hindsight.log"


@dataclass(frozen=True)
class Probe:
    status: str           # healthy | down | tenant_auth_off | wrong_key | error
    detail: str = ""


def configure(conn: sqlite3.Connection, business_id: str, venv: str, now: str, *,
              key_file: Optional[str] = None, hf_home: Optional[str] = None) -> None:
    """Record how to run this business's Hindsight (one business per olo.db)."""
    migrate(conn)
    bound = conn.execute("SELECT value FROM olo_bindings WHERE name='business_id'").fetchone()
    if bound is not None and bound[0] != business_id:
        raise ValueError(f"this olo.db belongs to business {bound[0]!r}, not {business_id!r}")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO hindsight_service (id, business_id, venv, key_file, hf_home, configured_at)"
        " VALUES (1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET business_id=excluded.business_id,"
        " venv=excluded.venv, key_file=excluded.key_file, hf_home=excluded.hf_home,"
        " configured_at=excluded.configured_at",
        (business_id, venv, key_file, hf_home, now))
    conn.execute("COMMIT")


def load_spec(conn: sqlite3.Connection, hermes_home: Path) -> ServiceSpec:
    """The configured service, with port, bank and tenant key from setup.sh."""
    from olo.m3.hindsight.client import HindsightConfig
    migrate(conn)
    row = conn.execute("SELECT business_id, venv, key_file, hf_home FROM hindsight_service"
                       " WHERE id=1").fetchone()
    if row is None:
        raise ValueError("Hindsight service not configured: run `hermes olo hindsight configure`")
    business, venv, key_file, hf_home = tuple(row)
    registry = hermes_home / "hindsight_port_registry.json"
    cfg = HindsightConfig.for_business(business, registry_path=str(registry))
    return ServiceSpec(business_id=business, bank_id=cfg.bank_id,
                       home=hermes_home / "businesses" / business,
                       executable=Path(venv) / "bin" / "hindsight-api",
                       port=cfg.port, api_key=cfg.api_key, key_file=key_file, hf_home=hf_home)


# ── Probe ─────────────────────────────────────────────────────────────────────

def _get(url: str, headers: dict, timeout: float) -> int:
    from hermes_cli.urllib_security import open_credentialed_url
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with open_credentialed_url(req, timeout=timeout) as resp:      # loopback only
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def probe(spec: ServiceSpec, timeout: float = 3.0) -> Probe:
    """Healthy, and enforcing this business's tenant key (C4)?"""
    base = f"http://127.0.0.1:{spec.port}"
    config = f"{base}/v1/default/banks/{spec.bank_id}/config"
    try:
        if _get(f"{base}/health", {}, timeout) != 200:
            return Probe("down", "health check did not return 200")
        anonymous = _get(config, {}, timeout)
        if anonymous != 401:
            return Probe("tenant_auth_off", f"unauthenticated request answered HTTP {anonymous}")
        authed = _get(config, {"Authorization": f"Bearer {spec.api_key}"}, timeout)
    except (urllib.error.URLError, OSError) as exc:
        return Probe("down", f"{type(exc).__name__}")
    if authed == 401:
        return Probe("wrong_key", "the server on this port rejects this business's tenant key")
    if authed not in (200, 404):
        return Probe("error", f"authenticated request answered HTTP {authed}")
    return Probe("healthy")


# ── Process ───────────────────────────────────────────────────────────────────

def _alive(pid: Optional[int], create_time: Optional[float]) -> bool:
    import psutil
    if not pid or create_time is None:
        return False
    try:
        proc = psutil.Process(pid)
        return (abs(proc.create_time() - create_time) < 0.01
                and proc.status() != psutil.STATUS_ZOMBIE)
    except psutil.Error:
        return False


def _environment(spec: ServiceSpec) -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(spec.home), "OTEL_SDK_DISABLED": "true"}
    if spec.hf_home:
        env.update({"HF_HOME": spec.hf_home, "HF_HUB_OFFLINE": "1"})
    if spec.key_file:
        from olo.activation import load_key_file
        env["HINDSIGHT_API_LLM_API_KEY"] = load_key_file(spec.key_file)
    return env


def _spawn(spec: ServiceSpec) -> tuple[int, float]:
    import psutil
    with open(spec.log_path, "ab") as log:     # the child keeps its own descriptor
        proc = subprocess.Popen(
            [str(spec.executable), "--host", "127.0.0.1", "--port", str(spec.port)],
            cwd=str(spec.home), env=_environment(spec), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    return proc.pid, psutil.Process(proc.pid).create_time()


def _event(conn: sqlite3.Connection, now: str, event: str, detail: str = "") -> None:
    conn.execute("INSERT INTO hindsight_service_events (at, event, detail) VALUES (?,?,?)",
                 (now, event, detail[:500]))


def _state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT pid, create_time, started_at, status, consecutive_failures,"
                       " last_healthy_at, detail FROM hindsight_service_state WHERE id=1").fetchone()
    keys = ("pid", "create_time", "started_at", "status", "failures", "last_healthy_at", "detail")
    return dict(zip(keys, tuple(row))) if row else {k: None for k in keys} | {"failures": 0}


def _save(conn: sqlite3.Connection, now: str, **fields) -> None:
    current = _state(conn)
    merged = {**current, **fields}
    conn.execute(
        "INSERT INTO hindsight_service_state (id, pid, create_time, started_at, status,"
        " consecutive_failures, last_check_at, last_healthy_at, detail)"
        " VALUES (1,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET pid=excluded.pid,"
        " create_time=excluded.create_time, started_at=excluded.started_at,"
        " status=excluded.status, consecutive_failures=excluded.consecutive_failures,"
        " last_check_at=excluded.last_check_at, last_healthy_at=excluded.last_healthy_at,"
        " detail=excluded.detail",
        (merged["pid"], merged["create_time"], merged["started_at"], merged["status"] or "unknown",
         merged["failures"] or 0, now, merged["last_healthy_at"], merged["detail"] or ""))


def start(conn: sqlite3.Connection, spec: ServiceSpec, now: str, *,
          spawn: Callable[[ServiceSpec], tuple[int, float]] = _spawn,
          event: str = "started") -> int:
    """Start the server (refused while the recorded process is alive)."""
    migrate(conn)
    st = _state(conn)
    if _alive(st["pid"], st["create_time"]):
        raise RuntimeError(f"Hindsight for {spec.business_id} is already running (pid {st['pid']})")
    if not spec.executable.exists():
        raise FileNotFoundError(f"Hindsight executable not found: {spec.executable}")
    pid, created = spawn(spec)
    conn.execute("BEGIN IMMEDIATE")
    _save(conn, now, pid=pid, create_time=created, started_at=now, status="starting",
          failures=0, detail="")
    _event(conn, now, event, f"pid {pid} port {spec.port}")
    conn.execute("COMMIT")
    return pid


def stop(conn: sqlite3.Connection, now: str, *, reason: str = "operator",
         timeout: float = 15.0) -> bool:
    """Stop the recorded process (verified by pid and creation time)."""
    import psutil
    migrate(conn)
    st = _state(conn)
    stopped = False
    if _alive(st["pid"], st["create_time"]):
        proc = psutil.Process(st["pid"])
        proc.terminate()
        try:
            proc.wait(timeout)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout)
        stopped = True
    conn.execute("BEGIN IMMEDIATE")
    _save(conn, now, pid=None, create_time=None, status="stopped", failures=0, detail=reason)
    _event(conn, now, "stopped", reason)
    conn.execute("COMMIT")
    return stopped


def _restarts_within(conn: sqlite3.Connection, now: datetime, window: timedelta) -> int:
    from olo.signals import format_utc
    since = format_utc(now - window)
    return conn.execute("SELECT COUNT(*) FROM hindsight_service_events"
                        " WHERE event='restarted' AND at >= ?", (since,)).fetchone()[0]


def supervise_once(conn: sqlite3.Connection, spec: ServiceSpec, now_dt: datetime, *,
                   check: Callable[[ServiceSpec], Probe] = probe,
                   spawn: Callable[[ServiceSpec], tuple[int, float]] = _spawn,
                   unhealthy_after: int = UNHEALTHY_AFTER, max_restarts: int = MAX_RESTARTS,
                   restart_window: timedelta = RESTART_WINDOW,
                   start_grace: timedelta = START_GRACE) -> str:
    """One supervision pass; returns the resulting status (module table)."""
    from olo.signals import format_utc
    migrate(conn)
    now = format_utc(now_dt)
    st = _state(conn)
    alive = _alive(st["pid"], st["create_time"])
    result = check(spec)

    def save(status: str, **fields) -> str:
        conn.execute("BEGIN IMMEDIATE")
        if status != st["status"] and status in ("misconfigured", "crashloop", "adopted"):
            _event(conn, now, status, result.detail)
        _save(conn, now, status=status, **fields)
        conn.execute("COMMIT")
        return status

    if result.status == "healthy":
        return save("healthy" if alive else "adopted", last_healthy_at=now, failures=0,
                    detail="" if alive else "a server Olo did not start answers on the port")
    if result.status in ("tenant_auth_off", "wrong_key"):
        return save("misconfigured", detail=result.detail)
    if st["status"] in ("crashloop", "stopped"):
        return save(st["status"])           # an operator starts it again
    started = datetime.fromisoformat(st["started_at"]) if st["started_at"] else None
    if alive and started is not None and now_dt - started < start_grace and not st["last_healthy_at"]:
        return save("starting", detail=result.detail)
    failures = (st["failures"] or 0) + 1
    if alive and failures < unhealthy_after:
        return save("unhealthy", failures=failures, detail=result.detail)
    if _restarts_within(conn, now_dt, restart_window) >= max_restarts:
        return save("crashloop", failures=failures,
                    detail=f"{max_restarts} restarts within {restart_window}; start it manually "
                           "after fixing the cause (see hindsight.log)")
    if alive:
        stop(conn, now, reason=f"unhealthy for {failures} checks: {result.detail}")
    start(conn, spec, now, spawn=spawn, event="restarted" if st["pid"] or st["started_at"] else "started")
    return "restarting"


def service_status(conn: sqlite3.Connection, limit: int = 10) -> dict:
    """The supervisor's view, for `hermes olo status` and the health record."""
    migrate(conn)
    st = _state(conn)
    events = [{"at": r[0], "event": r[1], "detail": r[2]} for r in conn.execute(
        "SELECT at, event, detail FROM hindsight_service_events ORDER BY id DESC LIMIT ?", (limit,))]
    return {"status": st["status"] or "not_started", "pid": st["pid"],
            "alive": _alive(st["pid"], st["create_time"]), "started_at": st["started_at"],
            "last_healthy_at": st["last_healthy_at"], "failures": st["failures"] or 0,
            "detail": st["detail"] or "", "events": events}

