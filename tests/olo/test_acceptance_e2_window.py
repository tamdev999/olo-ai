"""E2 measures two unattended hours of the installed scheduler (scenarios.e2).

Regressions:

* live targeted re-acceptance, 2026-11-27: a bounded 35-minute scheduled
  regression passed E2 version 1, which looked only at the health conditions
  at check time.  Version 2 measured the window.
* E2 version 2 counted every ingest run, so a worker invoked by hand (or a
  loop of ``run.py sync``) satisfied it, a scheduled fire that never polled
  its source went unnoticed, and an exhausted model budget looked like
  Mode A's intentionally disabled model.  Version 3 judges the scheduler by
  cron's own execution ledger, ties every ingest run to a scheduled fire and
  requires a mid-window canary ingested by a scheduled run.

The jobs are installed with ``olo.hermes_plugin.schedule.ensure_job`` (what
``hermes olo setup --schedule`` calls), the ingest runs are the real worker's,
and the scheduler's attempts are written as cron's ``executions`` ledger rows
(source ``builtin`` for a scheduled fire, ``direct`` for ``hermes cron run``).
"""
from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olo.adapters.gmail_full import build_email_content_signal
from olo.runner import run_source
from olo.signals import format_utc
from olo.store import connect
from tests.olo import gmail_fixtures as gf
from tests.olo.test_calendar_email_linking import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "olo" / "m3"))
from google_acceptance import scenarios

START = datetime(2026, 11, 27, 22, 30, tzinfo=UTC)
EVERY = {"gmail": timedelta(minutes=5), "gcal": timedelta(minutes=15), "hindsight": timedelta(minutes=5)}


class Installed:
    """A HERMES_HOME with the three Olo jobs installed and an empty olo.db."""

    def __init__(self, home: Path) -> None:
        from cron.executions import list_executions
        from olo.hermes_plugin.schedule import ensure_job
        (home / "scripts").mkdir(parents=True)
        self.db = home / "olo" / "olo.db"
        self.db.parent.mkdir(parents=True)
        self.jobs: dict[str, str] = {}
        for kind, schedule in (("gmail", "every 5m"), ("gcal", "every 15m"), ("hindsight", "every 1m")):
            script = f"olo_{kind}.py"
            (home / "scripts" / script).write_text("raise SystemExit(0)\n", encoding="utf-8")
            self.jobs[kind], _ = ensure_job(f"olo-{kind}-biz", schedule, script)
        list_executions()                                    # creates cron's ledger
        self.ledger = home / "cron" / "executions.db"
        self.queues = {k: Queue(f"{k}_biz") for k in ("gmail", "gcal")}

    def fire(self, kind: str, at: datetime, *, source: str = "builtin", status: str = "completed") -> None:
        local = at.astimezone(datetime.now().astimezone().tzinfo)
        conn = sqlite3.connect(self.ledger)
        conn.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at,"
            " started_at, finished_at, scheduled_instant, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, self.jobs[kind], source, "p", 1, status, local.isoformat(),
             (local + timedelta(seconds=1)).isoformat(),
             None if status == "missed" else (local + timedelta(seconds=9)).isoformat(),
             at.isoformat(), "degraded: commitment: {'budget_exhausted': 1}" if status == "failed" else None))
        conn.commit()
        conn.close()

    def poll(self, kind: str, at: datetime) -> None:
        assert run_source(self.queues[kind], db_path=self.db,
                          clock=lambda: at + timedelta(seconds=2)).status == "succeeded"

    def canary(self) -> None:
        payload = gf.with_headers(gf.text_part("A new message during the unattended window."),
                                  gf.message_headers(frm="Customer <customer@acceptance.test>",
                                                     to="biz@acceptance.test",
                                                     subject="[OLO-E2] canary", msg_id="canary"))
        raw = gf.raw_message("canary", payload, labels=["INBOX"], thread="t-canary")
        self.queues["gmail"].queue.append(build_email_content_signal("gmail_biz", raw, "1"))

    def run(self, until: timedelta, *, silent: dict | None = None, canary_at: timedelta | None
            = timedelta(minutes=61), fire_only: frozenset = frozenset()) -> None:
        """Each job fired on its interval from START; a worker fire polls its source."""
        for kind, every in EVERY.items():
            at = START + timedelta(minutes=1)
            while at <= START + until:
                lo, hi = (silent or {}).get(kind, (timedelta(0), timedelta(0)))
                if not (START + lo <= at < START + hi):
                    if kind == "gmail" and canary_at is not None and at - every < START + canary_at <= at:
                        self.canary()
                    self.fire(kind, at)
                    if kind in self.queues and (kind, at) not in fire_only:
                        self.poll(kind, at)
                at += every


@pytest.fixture()
def installed(tmp_path, monkeypatch) -> Installed:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return Installed(tmp_path / "home")


def _check(inst: Installed, now: datetime, **kw):
    conn = connect(inst.db)
    try:
        ctx = scenarios.Ctx(conn, "biz@acceptance.test", "customer@acceptance.test",
                            {"E2": {"at": format_utc(START), "by": "operator"}}, now=now)
        if kw:
            [out] = scenarios.evaluate(ctx, {"E2"}, now=format_utc(now), **kw)
            return out
        return scenarios.e2(ctx)
    finally:
        conn.close()


AFTER_2H = START + timedelta(hours=2, minutes=1)


def test_two_unattended_hours_of_the_installed_scheduler_pass(installed) -> None:
    installed.run(timedelta(hours=2))
    ok, details = _check(installed, AFTER_2H)
    assert ok, details
    gmail, gcal = details["jobs"]["gmail"], details["jobs"]["gcal"]
    assert gmail["scheduled_completed"] == gmail["ingest_runs"] == 24 and gmail["unscheduled_runs"] == 0
    assert gcal["scheduled_completed"] == gcal["ingest_runs"] == 8
    assert details["canary"] == {**details["canary"], "seen": True, "by_scheduled_run": True}


def test_a_bounded_35_minute_window_does_not_pass(installed) -> None:
    installed.run(timedelta(minutes=35), canary_at=timedelta(minutes=20))
    ok, details = _check(installed, START + timedelta(minutes=36))
    assert not ok
    assert details["window_min"] == 36.0 and details["required_min"] == 120.0
    assert details["conditions"] == []                 # healthy — and still not two hours


def test_a_job_silent_for_45_minutes_fails(installed) -> None:
    installed.run(timedelta(hours=2), silent={"gcal": (timedelta(minutes=40), timedelta(minutes=85))})
    ok, details = _check(installed, AFTER_2H)
    assert not ok and details["jobs"]["gcal"]["max_gap_min"] > 30


def test_a_manually_invoked_worker_is_not_the_scheduler(installed) -> None:
    """Two hours of a worker run by hand (no scheduled fires) is not E2."""
    for kind, every in (("gmail", EVERY["gmail"]), ("gcal", EVERY["gcal"])):
        at = START + timedelta(minutes=1)
        while at <= START + timedelta(hours=2):
            installed.poll(kind, at)
            at += every
    ok, details = _check(installed, AFTER_2H)
    assert not ok
    assert details["jobs"]["gmail"]["scheduled_completed"] == 0
    assert details["jobs"]["gmail"]["unscheduled_runs"] == 24


def test_a_manual_run_during_the_window_fails(installed) -> None:
    installed.run(timedelta(hours=2))
    installed.poll("gmail", START + timedelta(minutes=63, seconds=30))        # run.py sync by hand
    installed.fire("gcal", START + timedelta(minutes=70), source="direct")   # hermes cron run
    ok, details = _check(installed, AFTER_2H)
    assert not ok
    assert details["jobs"]["gmail"]["unscheduled_runs"] == 1 and details["jobs"]["gcal"]["manual_fires"] == 1


def test_a_scheduled_fire_that_never_polled_its_source_fails(installed) -> None:
    installed.run(timedelta(hours=2), fire_only=frozenset({("gcal", START + timedelta(minutes=46))}))
    ok, details = _check(installed, AFTER_2H)
    assert not ok and details["jobs"]["gcal"]["fires_without_a_run"] == 1


def test_an_exhausted_model_budget_is_not_a_disabled_model(installed) -> None:
    installed.run(timedelta(hours=2))
    installed.fire("gmail", START + timedelta(minutes=90), status="failed")
    ok, details = _check(installed, AFTER_2H)
    assert not ok and details["jobs"]["gmail"]["scheduled_failed"] == 1
    assert "budget_exhausted" in details["jobs"]["gmail"]["errors"][0]


def test_a_missed_slot_fails(installed) -> None:
    installed.run(timedelta(hours=2))
    installed.fire("hindsight", START + timedelta(minutes=33), source="scheduler", status="missed")
    ok, details = _check(installed, AFTER_2H)
    assert not ok and details["jobs"]["hindsight"]["missed"] == 1


def test_surviving_processes_without_the_canary_fail(installed) -> None:
    installed.run(timedelta(hours=2), canary_at=None)
    ok, details = _check(installed, AFTER_2H)
    assert not ok and details["canary"] == {"seen": False}


def test_an_uninstalled_job_fails(installed) -> None:
    from cron.jobs import remove_job
    installed.run(timedelta(hours=2))
    remove_job(installed.jobs["hindsight"])
    ok, details = _check(installed, AFTER_2H)
    assert not ok and details["missing_jobs"] == ["hindsight"]


def test_a_verdict_under_an_older_criterion_is_not_reused(installed) -> None:
    installed.run(timedelta(minutes=35))
    now = START + timedelta(minutes=36)
    action = format_utc(START)
    for version in (1, 2):
        old = scenarios.Outcome("E2", "passed", {"conditions": []}, format_utc(now), action, version=version)
        out = _check(installed, now, frozen={"E2": old})
        assert out.fresh and out.version == 3 and out.status == "failed"
    current = scenarios.Outcome("E2", "failed", {}, format_utc(now), action, version=3)
    assert _check(installed, now, frozen={"E2": current}) is current        # same criterion: reused
