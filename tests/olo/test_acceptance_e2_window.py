"""E2 measures its two-hour unattended window (acceptance harness, scenarios.e2).

Regression for the live targeted re-acceptance of 2026-11-27: a bounded
35-minute scheduled regression passed E2, whose check looked only at the
health conditions at check time.  E2 (criterion version 2) now requires the
window itself — two hours since the action, every scheduled source running
with no gap over 30 minutes, nothing degraded or stalled — and a verdict
recorded under the old criterion is never reused.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from olo.runner import run_source
from olo.signals import format_utc
from olo.store import connect
from tests.olo.test_calendar_email_linking import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "olo" / "m3"))
from google_acceptance import scenarios

START = datetime(2026, 11, 27, 22, 30, tzinfo=UTC)


def _schedule(db: Path, until: timedelta, *,
              silent: dict[str, tuple[timedelta, timedelta]] | None = None) -> None:
    """Run each source on its installed interval from START, through the real worker."""
    for source, every in (("gmail_biz", timedelta(minutes=5)), ("gcal_biz", timedelta(minutes=15))):
        queue = Queue(source)
        at = START + timedelta(minutes=1)
        while at <= START + until:
            lo, hi = (silent or {}).get(source, (timedelta(0), timedelta(0)))
            if not (START + lo <= at < START + hi):
                assert run_source(queue, db_path=db, clock=lambda t=at: t).status == "succeeded"
            at += every


def _check(db: Path, now: datetime, actions=None, **kw):
    conn = connect(db)
    try:
        ctx = scenarios.Ctx(conn, "biz@acceptance.test", "customer@acceptance.test",
                            actions or {"E2": {"at": format_utc(START), "by": "operator"}}, now=now)
        if kw:
            [out] = scenarios.evaluate(ctx, {"E2"}, now=format_utc(now), **kw)
            return out
        return scenarios.e2(ctx)
    finally:
        conn.close()


def test_a_bounded_35_minute_window_does_not_pass(tmp_path) -> None:
    db = tmp_path / "olo.db"
    _schedule(db, timedelta(minutes=35))
    ok, details = _check(db, START + timedelta(minutes=36))
    assert not ok
    assert details["window_min"] == 36.0 and details["required_min"] == 120.0
    assert details["conditions"] == []                 # healthy — and still not two hours


def test_two_unattended_hours_pass(tmp_path) -> None:
    db = tmp_path / "olo.db"
    _schedule(db, timedelta(hours=2))
    ok, details = _check(db, START + timedelta(hours=2, minutes=1))
    assert ok, details
    assert details["coverage"]["gmail_biz"]["runs"] == 24 and details["coverage"]["gcal_biz"]["runs"] == 8


def test_a_source_silent_for_45_minutes_fails(tmp_path) -> None:
    db = tmp_path / "olo.db"
    _schedule(db, timedelta(hours=2), silent={"gcal_biz": (timedelta(minutes=40), timedelta(minutes=85))})
    ok, details = _check(db, START + timedelta(hours=2, minutes=1))
    assert not ok and details["coverage"]["gcal_biz"]["max_gap_min"] > 30


def test_a_verdict_under_the_old_criterion_is_not_reused(tmp_path) -> None:
    db = tmp_path / "olo.db"
    _schedule(db, timedelta(minutes=35))
    now = START + timedelta(minutes=36)
    action = format_utc(START)
    old = scenarios.Outcome("E2", "passed", {"conditions": []}, format_utc(now), action, version=1)
    out = _check(db, now, frozen={"E2": old})
    assert out.fresh and out.version == 2 and out.status == "failed"
    current = scenarios.Outcome("E2", "failed", {}, format_utc(now), action, version=2)
    assert _check(db, now, frozen={"E2": current}) is current           # same criterion: reused

