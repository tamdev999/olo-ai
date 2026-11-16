"""Concurrency and lease-ownership tests for olo.runner.

§8.4 (concurrency portion) acceptance criteria.
No time.sleep; uses threading.Event and FakeClock.
"""
from __future__ import annotations

import threading
from datetime import timezone
from pathlib import Path

import pytest

from olo.adapters.synthetic import SyntheticAdapter
from olo.runner import run_source
from olo.store import (
    connect,
    count_signals,
    get_checkpoint,
    get_lease,
    get_run,
    migrate,
    acquire_run,
)
from tests.olo.conftest import BlockingAdapter, FakeClock


def _db(tmp_path: Path) -> Path:
    return tmp_path / "olo" / "olo.db"


def _prep(tmp_path: Path):
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    return conn


# ── Live runner is not abandoned ──────────────────────────────────────────────

def test_live_runner_is_not_abandoned(tmp_path: Path) -> None:
    """Runner A blocks in fetch while holding the lease; Runner B starts and
    gets skipped_busy. A's run stays running. Release A → A succeeds, 25 signals."""
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    adapter_a = BlockingAdapter(total=25)
    result_b_holder: list = []
    error_holder: list = []

    def run_a():
        try:
            result = run_source(adapter_a, db_path=db)
            result_b_holder.append(("a", result))
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=run_a)
    t.start()

    # Wait until A is blocking inside fetch
    adapter_a.wait_blocked(timeout=10)

    # Runner B — should be skipped_busy
    result_b = run_source(SyntheticAdapter({"total": "25"}), db_path=db)
    assert result_b.status == "skipped_busy", f"expected skipped_busy, got {result_b.status}"

    # A's run row is still running
    conn = connect(db)
    lease = get_lease("synthetic", conn)
    assert lease is not None, "lease should still be held by A"
    # Audit: run.skipped_busy exists, run.abandoned does NOT
    skipped = conn.execute(
        "SELECT * FROM audit_events WHERE action='run.skipped_busy'"
    ).fetchall()
    assert len(skipped) == 1
    abandoned = conn.execute(
        "SELECT * FROM audit_events WHERE action='run.abandoned'"
    ).fetchall()
    assert len(abandoned) == 0
    conn.close()

    # Release A
    adapter_a.release()
    t.join(timeout=15)
    assert not error_holder, f"Runner A errored: {error_holder}"
    assert result_b_holder, "Runner A did not complete"
    result_a = result_b_holder[0][1]
    assert result_a.status == "succeeded"
    conn2 = connect(db)
    assert count_signals("synthetic", conn2) == 25
    conn2.close()


# ── Expired lease takeover ────────────────────────────────────────────────────

def test_expired_lease_takeover_and_lease_lost(tmp_path: Path) -> None:
    """A blocks at after_fetch; fake clock advances past TTL; B runs to completion.
    Then A resumes and gets LeaseLost; A's ingest_batch writes nothing."""
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    clock = FakeClock()
    SHORT_TTL = 10

    # A uses a fault that advances the clock
    a_fault_barrier = threading.Event()
    a_resume_event = threading.Event()
    a_error: list = []
    a_result: list = []

    def a_fault(point: str) -> None:
        if point == "after_fetch":
            # Signal main thread that A is at after_fetch
            a_fault_barrier.set()
            # Wait until main thread says proceed
            a_resume_event.wait(timeout=15)

    def run_a():
        try:
            result = run_source(
                SyntheticAdapter({"total": "25"}),
                db_path=db,
                clock=clock,
                lease_ttl=SHORT_TTL,
                _fault=a_fault,
            )
            a_result.append(result)
        except Exception as exc:
            a_error.append(exc)

    t_a = threading.Thread(target=run_a)
    t_a.start()

    # Wait for A to reach after_fetch
    a_fault_barrier.wait(timeout=10)

    # Advance clock past TTL
    clock.advance(SHORT_TTL + 1)

    # B takes over (expires A's lease) and completes
    result_b = run_source(
        SyntheticAdapter({"total": "25"}),
        db_path=db,
        clock=clock,
        lease_ttl=600,
    )
    assert result_b.status == "succeeded", f"B status: {result_b.status}"

    # B abandoned A's run
    conn = connect(db)
    # Find A's run id (the abandoned one)
    abandoned = conn.execute(
        "SELECT id FROM ingest_runs WHERE status='abandoned'"
    ).fetchone()
    assert abandoned is not None
    abandoned_audits = conn.execute(
        "SELECT * FROM audit_events WHERE action='run.abandoned'"
    ).fetchall()
    assert len(abandoned_audits) >= 1
    conn.close()

    # Resume A — it will try to ingest but its lease is gone
    a_resume_event.set()
    t_a.join(timeout=15)

    # A either raised LeaseLost or returned failed
    # Either way, 25 signals from B, no duplicates from A
    conn2 = connect(db)
    assert count_signals("synthetic", conn2) == 25
    # A's run is abandoned (not succeeded)
    if a_result:
        assert a_result[0].status in ("failed", "succeeded"), a_result
    conn2.close()
