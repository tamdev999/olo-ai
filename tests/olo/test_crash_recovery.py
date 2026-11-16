"""Crash and restart recovery tests for olo.runner.

§8.4 (crash recovery portion) acceptance criteria.
Uses subprocess os._exit() to simulate hard kills.
Waits on process exit, never sleeps.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from olo.store import (
    connect,
    count_signals,
    get_checkpoint,
    get_lease,
    get_run,
    migrate,
)
from tests.olo.conftest import FakeClock
from datetime import UTC


def _db(tmp_path: Path) -> Path:
    return tmp_path / "olo" / "olo.db"


def _run_subprocess(tmp_path: Path, script: str, env_extra: dict | None = None) -> int:
    """Run *script* in a fresh Python subprocess; return its exit code."""
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "hermes_home")
    env["OLO_TEST_DB"] = str(_db(tmp_path))
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env=env, cwd=str(Path(__file__).parent.parent.parent), check=False,  # repo root
    )
    return proc.returncode


# ── Adapter failure at batch 1 (run row already created) ─────────────────────

def test_adapter_failure_after_run_created_batch1(tmp_path: Path) -> None:
    """fail_at_batch=1: run row created by acquire_run, then first fetch raises.
    Run → failed; 0 signals; no checkpoint row; lease released."""
    from olo.adapters.synthetic import SyntheticAdapter
    from olo.runner import run_source

    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(Exception):  # SourceError re-raised
        run_source(SyntheticAdapter({"total": "25", "fail_at_batch": "1"}),
                   db_path=db)

    conn = connect(db)
    assert count_signals("synthetic", conn) == 0
    assert get_checkpoint("synthetic", conn) is None
    assert get_lease("synthetic", conn) is None

    run = conn.execute("SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert run is not None
    assert run["status"] == "failed"
    assert "SourceError" in (run["error"] or "")

    # Recovery run succeeds
    result = run_source(SyntheticAdapter({"total": "25"}), db_path=db)
    assert result.status == "succeeded"
    assert count_signals("synthetic", conn) == 25
    conn.close()


# ── Adapter failure mid-run at batch 3 ────────────────────────────────────────

def test_adapter_failure_mid_run_batch3(tmp_path: Path) -> None:
    """fail_at_batch=3, limit=10: 2 batches (20 signals) committed, then failure."""
    from olo.adapters.synthetic import SyntheticAdapter
    from olo.runner import run_source

    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(Exception):
        run_source(SyntheticAdapter({"total": "25", "fail_at_batch": "3"}),
                   limit=10, db_path=db)

    conn = connect(db)
    assert count_signals("synthetic", conn) == 20
    assert get_checkpoint("synthetic", conn) == "20"

    run = conn.execute("SELECT * FROM ingest_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    assert run["status"] == "failed"
    assert run["batches"] == 2
    assert run["inserted"] == 20

    # Recovery run from cursor "20"
    result = run_source(SyntheticAdapter({"total": "25"}), limit=10, db_path=db)
    assert result.status == "succeeded"
    assert count_signals("synthetic", conn) == 25
    assert get_checkpoint("synthetic", conn) == "25"
    conn.close()


# ── Invalid signal mid-batch rolls back whole batch ───────────────────────────

def test_invalid_signal_mid_batch_rolls_back_whole_batch(tmp_path: Path) -> None:
    """Adapter returns 10 signals in batch 2; 7th has wrong source → batch 2 fully rolled back."""
    from olo.adapters import FetchBatch, MonitorAdapter, SourceError
    from olo.runner import run_source
    from olo.signals import NormalizedSignal
    from datetime import datetime, timezone

    class PartiallyBadAdapter(MonitorAdapter):
        source = "synthetic"
        _call = 0

        def fetch(self, cursor, *, limit):
            self._call += 1
            if self._call == 1:
                # Good batch
                sigs = tuple(
                    NormalizedSignal(
                        source="synthetic",
                        external_id=f"syn-1-{n:05d}",
                        kind="payment.failed",
                        occurred_at=datetime(2026, 1, 1, 0, n, 0, tzinfo=UTC),
                        subject=None, payload={"seq": n},
                    )
                    for n in range(1, 11)
                )
                return FetchBatch(sigs, "10", True)
            # Batch 2: 7th signal has wrong source
            sigs = []
            for n in range(11, 21):
                src = "wrong_source" if n == 17 else "synthetic"
                try:
                    s = NormalizedSignal(
                        source=src,
                        external_id=f"syn-1-{n:05d}",
                        kind="payment.failed",
                        occurred_at=datetime(2026, 1, 1, 0, n % 60, 0, tzinfo=UTC),
                        subject=None, payload={"seq": n},
                    )
                    sigs.append(s)
                except ValueError:
                    s = NormalizedSignal(
                        source="synthetic",
                        external_id=f"syn-1-{n:05d}",
                        kind="payment.failed",
                        occurred_at=datetime(2026, 1, 1, 0, n % 60, 0, tzinfo=UTC),
                        subject=None, payload={"seq": n, "corrupted": True},
                    )
                    sigs.append(s)
            # Mark 7th as wrong source directly by patching source
            # We can't set source after creation, so return them directly
            # and patch the runner's source check by injecting a wrong-source signal
            sigs[6] = NormalizedSignal(
                source="bad_source",  # will fail AdapterContractError check in runner
                external_id="syn-1-00017",
                kind="payment.failed",
                occurred_at=datetime(2026, 1, 1, 0, 17 % 60, 0, tzinfo=UTC),
                subject=None, payload={"seq": 17},
            )
            return FetchBatch(tuple(sigs), "20", False)

    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(Exception):
        run_source(PartiallyBadAdapter(), limit=10, db_path=db)

    conn = connect(db)
    # Only batch 1 (signals 1-10) committed; batch 2 fully rolled back
    assert count_signals("synthetic", conn) == 10
    assert get_checkpoint("synthetic", conn) == "10"
    conn.close()


# ── Hard kill mid-transaction ─────────────────────────────────────────────────

def test_hard_kill_mid_transaction_loses_nothing(tmp_path: Path) -> None:
    """Subprocess runner calls os._exit(137) at before_commit of batch 2.
    DB: 10 signals, checkpoint "10", run row running, lease present."""
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    # Pre-migrate
    conn = connect(db)
    migrate(conn)
    conn.close()

    script = f"""
import os, sys
sys.path.insert(0, r'{Path(__file__).parent.parent.parent!s}')
from pathlib import Path
from olo.adapters.synthetic import SyntheticAdapter
from olo.runner import run_source

call = [0]
def fault(point):
    if point == 'before_commit':
        call[0] += 1
        if call[0] == 2:
            os._exit(137)

try:
    run_source(
        SyntheticAdapter({{'total': '25'}}),
        limit=10, max_batches=5,
        db_path=Path(r'{db!s}'),
        _fault=fault,
    )
except Exception as e:
    sys.exit(1)
"""
    rc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env={**os.environ, "HERMES_HOME": str(tmp_path / "hermes_home")},
        cwd=str(Path(__file__).parent.parent.parent), check=False,
    ).returncode
    # os._exit(137) → returncode 137
    assert rc == 137

    conn = connect(db)
    assert count_signals("synthetic", conn) == 10
    assert get_checkpoint("synthetic", conn) == "10"
    # Run row is still "running" (killed before finish_run)
    run_row = conn.execute(
        "SELECT status FROM ingest_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    assert run_row["status"] == "running"
    # Lease is still present
    assert get_lease("synthetic", conn) is not None
    conn.close()


def test_restart_before_lease_expiry_is_skipped(tmp_path: Path) -> None:
    """Immediately after the kill, re-running returns skipped_busy."""
    # Setup: pre-kill state (10 signals, run running, lease valid)
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    # Simulate post-kill state: run row running with active lease
    from olo.store import acquire_run
    from tests.olo.conftest import FakeClock
    clock = FakeClock()
    run_id = acquire_run("synthetic", "killed_proc:1:0", conn, clock=clock, ttl=900)
    conn.close()

    from olo.runner import run_source
    from olo.adapters.synthetic import SyntheticAdapter
    result = run_source(SyntheticAdapter({"total": "25"}), db_path=db, clock=clock)
    assert result.status == "skipped_busy"

    # Killed run still "running"
    conn2 = connect(db)
    run_row = get_run(run_id, conn2)
    assert run_row["status"] == "running"
    conn2.close()


def test_restart_after_lease_expiry_recovers(tmp_path: Path) -> None:
    """After lease expires, new run abandons killed run and succeeds."""
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    from olo.store import acquire_run, ingest_batch
    from olo.adapters.synthetic import SyntheticAdapter
    from olo.signals import NormalizedSignal
    from datetime import datetime, timezone
    clock = FakeClock()
    SHORT_TTL = 10

    # Simulate: 10 signals ingested, then kill
    run_id = acquire_run("synthetic", "killed_proc:1:0", conn, clock=clock, ttl=SHORT_TTL)
    sigs = tuple(
        NormalizedSignal(
            source="synthetic",
            external_id=f"syn-1-{n:05d}",
            kind="payment.failed",
            occurred_at=datetime(2026, 1, 1, 0, n % 60, 0, tzinfo=UTC),
            subject=None, payload={"seq": n},
        )
        for n in range(1, 11)
    )
    ingest_batch("synthetic", run_id, sigs, "10", None, conn, clock)
    conn.close()

    # Advance clock past TTL
    clock.advance(SHORT_TTL + 1)

    from olo.runner import run_source
    result = run_source(SyntheticAdapter({"total": "25"}), db_path=db, clock=clock)
    assert result.status == "succeeded"

    conn2 = connect(db)
    assert count_signals("synthetic", conn2) == 25
    assert get_checkpoint("synthetic", conn2) == "25"

    # Killed run was abandoned
    old_run = get_run(run_id, conn2)
    assert old_run["status"] == "abandoned"
    conn2.close()


def test_kill_after_commit_does_not_duplicate(tmp_path: Path) -> None:
    """os._exit at after_commit of batch 2; recovery: 25 signals, no dups."""
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    conn.close()

    script = f"""
import os, sys
sys.path.insert(0, r'{Path(__file__).parent.parent.parent!s}')
from pathlib import Path
from olo.adapters.synthetic import SyntheticAdapter
from olo.runner import run_source

call = [0]
def fault(point):
    if point == 'after_commit':
        call[0] += 1
        if call[0] == 2:
            os._exit(0)  # clean exit after batch 2 committed

try:
    run_source(
        SyntheticAdapter({{'total': '25'}}),
        limit=10, max_batches=5,
        db_path=Path(r'{db!s}'),
        _fault=fault,
    )
except Exception:
    pass
"""
    subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        env={**os.environ, "HERMES_HOME": str(tmp_path / "hermes_home")},
        cwd=str(Path(__file__).parent.parent.parent), check=False,
    )

    # 20 signals committed, lease and run_row in-flight
    conn = connect(db)
    n = count_signals("synthetic", conn)
    # Get the actual lease expires_at so we can advance past it
    lease = get_lease("synthetic", conn)
    conn.close()
    assert n == 20

    # Build a clock that is past the lease expiry
    from datetime import datetime, timezone
    if lease:
        expires = datetime.fromisoformat(lease["expires_at"])
        # Advance 1 second past expiry
        past_expiry = expires.timestamp() + 1
        recovery_clock = FakeClock(
            datetime.fromtimestamp(past_expiry, tz=UTC)
        )
    else:
        clock = FakeClock()
        clock.advance(901)
        recovery_clock = clock

    from olo.runner import run_source
    from olo.adapters.synthetic import SyntheticAdapter
    result = run_source(SyntheticAdapter({"total": "25"}), db_path=db, clock=recovery_clock)
    assert result.status == "succeeded"

    conn2 = connect(db)
    assert count_signals("synthetic", conn2) == 25
    # No duplicates from the recovery run
    all_runs = conn2.execute("SELECT * FROM ingest_runs ORDER BY started_at").fetchall()
    total_dupes = sum(r["duplicates"] for r in all_runs)
    # At most 20 dups from recovery (the already-committed batch)
    assert total_dupes <= 20
    conn2.close()
