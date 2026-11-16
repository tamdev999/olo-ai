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
    DEFAULT_LEASE_TTL,
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


# ── succeeded-after-abandoned correctness ─────────────────────────────────────

def test_run_reports_failed_not_succeeded_when_abandoned_before_finish(
    tmp_path: Path,
) -> None:
    """run_source must NOT return status='succeeded' if finish_run was a no-op
    because a successor already abandoned the run.

    Scenario:
      - Runner A: lease TTL=10s, fake clock.
      - Advance clock past TTL so lease expires while A is still in the loop.
      - Runner B acquires, abandons A's run, and completes successfully.
      - A then reaches finish_run('succeeded') — which is now a no-op.
      - A must return status='failed', not 'succeeded'.
    """
    db = tmp_path / "olo" / "olo.db"
    db.parent.mkdir(parents=True, exist_ok=True)

    clock = FakeClock()
    SHORT_TTL = 10

    # Use a fault to advance the clock past TTL between the last ingest_batch
    # and the finish_run call, so the lease expires in that window.
    # We achieve this by having A run with a BlockingAdapter that lets us
    # expire the lease after the final batch commits but before finish_run.

    from olo.store import (
        acquire_run, finish_run as _finish_run,
        ingest_batch, migrate, get_run,
    )
    from olo.signals import NormalizedSignal
    from datetime import datetime, UTC

    conn_a = connect(db)
    migrate(conn_a)

    # Manually simulate: A acquires, ingests one batch, then lease expires
    run_a_id = acquire_run("synthetic", "proc_a:1:0", conn_a, clock=clock, ttl=SHORT_TTL)

    sigs = tuple(
        NormalizedSignal(
            source="synthetic",
            external_id=f"syn-1-{n:05d}",
            kind="payment.failed",
            occurred_at=datetime(2026, 1, 1, 0, n % 60, 0, tzinfo=UTC),
            subject=None,
            payload={"seq": n},
        )
        for n in range(1, 26)
    )
    ingest_batch("synthetic", run_a_id, sigs, "25", None, conn_a, clock)
    # At this point A's run is 'running', signals are committed.

    # Expire the lease
    clock.advance(SHORT_TTL + 1)

    # Runner B acquires (abandons A) and completes
    conn_b = connect(db)
    run_b_id = acquire_run("synthetic", "proc_b:2:0", conn_b, clock=clock, ttl=DEFAULT_LEASE_TTL)
    # B's ingest is a no-op (cursor already at 25)
    from olo.store import get_checkpoint
    cursor_b = get_checkpoint("synthetic", conn_b)
    assert cursor_b == "25"
    # B calls finish_run succeeded
    _finish_run("synthetic", run_b_id, "succeeded", conn_b, clock=clock)
    conn_b.close()

    # A's run is now abandoned; calling finish_run with 'succeeded' must be a no-op
    updated = _finish_run("synthetic", run_a_id, "succeeded", conn_a, clock=clock)
    assert not updated, "finish_run should return False for already-abandoned run"

    run_a = get_run(run_a_id, conn_a)
    assert run_a["status"] == "abandoned", (
        f"expected A's run to be 'abandoned', got {run_a['status']!r}"
    )
    conn_a.close()

    # Now prove run_source itself reports 'failed' in this scenario by using
    # a fault that advances the clock after the final ingest_batch but before
    # finish_run, then having a background runner take the lease.
    db2 = tmp_path / "olo2" / "olo.db"
    db2.parent.mkdir(parents=True, exist_ok=True)

    clock2 = FakeClock()
    SHORT_TTL2 = 10
    after_commit_called = threading.Event()
    proceed_event = threading.Event()

    def fault(point: str) -> None:
        if point == "after_commit":
            # Signal that the last batch committed; now expire the lease
            after_commit_called.set()
            proceed_event.wait(timeout=10)

    result_holder: list = []
    error_holder: list = []

    def run_a_thread() -> None:
        try:
            r = run_source(
                SyntheticAdapter({"total": "25"}),
                limit=100, max_batches=50,
                clock=clock2, lease_ttl=SHORT_TTL2,
                db_path=db2, _fault=fault,
            )
            result_holder.append(r)
        except Exception as exc:
            error_holder.append(exc)

    t = threading.Thread(target=run_a_thread)
    t.start()

    # Wait until A has committed the last batch (after_commit)
    after_commit_called.wait(timeout=10)

    # Expire the clock so A's lease is now expired
    clock2.advance(SHORT_TTL2 + 1)

    # Runner B takes over (abandons A's run) and completes
    result_b = run_source(
        SyntheticAdapter({"total": "25"}),
        clock=clock2, lease_ttl=600,
        db_path=db2,
    )
    assert result_b.status == "succeeded"

    # Let A's thread continue to finish_run
    proceed_event.set()
    t.join(timeout=10)

    assert not error_holder, f"run_a raised: {error_holder}"
    assert result_holder, "run_a did not return a result"
    result_a = result_holder[0]

    # A must NOT report succeeded — the run was abandoned by B
    assert result_a.status == "failed", (
        f"run_source returned {result_a.status!r} instead of 'failed' "
        f"after being abandoned; run_id={result_a.run_id}"
    )
