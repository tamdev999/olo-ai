"""Pagination-specific tests for run_source().

§8.3 (pagination portion) acceptance criteria.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from olo.adapters.synthetic import SyntheticAdapter
from olo.runner import run_source
from olo.store import connect, count_signals, get_checkpoint


def _db(tmp_path: Path) -> Path:
    return tmp_path / "olo" / "olo.db"


def _adapter(**kwargs) -> SyntheticAdapter:
    return SyntheticAdapter({k: str(v) for k, v in kwargs.items()})


def _conn(tmp_path: Path):
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    return connect(db)


def test_max_batches_reports_partial_not_success(tmp_path: Path) -> None:
    # 25 events, limit=5, max_batches=2 → 10 signals, partial
    result = run_source(_adapter(total=25), limit=5, max_batches=2, db_path=_db(tmp_path))
    assert result.status == "partial"
    assert result.inserted == 10
    assert result.batches == 2
    conn = _conn(tmp_path)
    assert get_checkpoint("synthetic", conn) == "10"
    assert count_signals("synthetic", conn) == 10
    conn.close()


def test_partial_then_next_run_completes(tmp_path: Path) -> None:
    # First run: partial at 10
    run_source(_adapter(total=25), limit=5, max_batches=2, db_path=_db(tmp_path))
    # Second run: succeeds, no duplicates
    result2 = run_source(_adapter(total=25), limit=10, max_batches=50, db_path=_db(tmp_path))
    assert result2.status == "succeeded"
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 25
    assert result2.duplicates == 0
    conn.close()


def test_no_succeeded_audit_when_partial(tmp_path: Path) -> None:
    run_source(_adapter(total=25), limit=5, max_batches=2, db_path=_db(tmp_path))
    conn = _conn(tmp_path)
    # run.partial audit exists
    partial_audits = conn.execute(
        "SELECT * FROM audit_events WHERE action='run.partial'"
    ).fetchall()
    assert len(partial_audits) == 1
    # run.succeeded must NOT exist
    succeeded_audits = conn.execute(
        "SELECT * FROM audit_events WHERE action='run.succeeded'"
    ).fetchall()
    assert len(succeeded_audits) == 0
    conn.close()


def test_all_25_signals_once_with_overlap(tmp_path: Path) -> None:
    result = run_source(_adapter(total=25, overlap=3), limit=10, db_path=_db(tmp_path))
    assert result.status == "succeeded"
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 25
    conn.close()


def test_fault_after_signals_rolls_back_batch(tmp_path: Path) -> None:
    """_fault raised at after_signals in batch 2 → batch 2 not committed."""
    call_count = [0]

    def fault(point: str) -> None:
        if point == "after_signals":
            call_count[0] += 1
            if call_count[0] == 2:  # batch 2
                raise RuntimeError("injected fault at after_signals")

    with pytest.raises(RuntimeError, match="injected fault"):
        run_source(
            _adapter(total=25), limit=10, max_batches=5,
            db_path=_db(tmp_path), _fault=fault,
        )
    conn = _conn(tmp_path)
    # Only batch 1 committed
    assert count_signals("synthetic", conn) == 10
    assert get_checkpoint("synthetic", conn) == "10"
    conn.close()


def test_fault_after_checkpoint_rolls_back_batch(tmp_path: Path) -> None:
    call_count = [0]

    def fault(point: str) -> None:
        if point == "after_checkpoint":
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("injected fault at after_checkpoint")

    with pytest.raises(RuntimeError):
        run_source(
            _adapter(total=25), limit=10, max_batches=5,
            db_path=_db(tmp_path), _fault=fault,
        )
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 10
    assert get_checkpoint("synthetic", conn) == "10"
    conn.close()


def test_fault_before_commit_rolls_back_batch(tmp_path: Path) -> None:
    call_count = [0]

    def fault(point: str) -> None:
        if point == "before_commit":
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("injected fault at before_commit")

    with pytest.raises(RuntimeError):
        run_source(
            _adapter(total=25), limit=10, max_batches=5,
            db_path=_db(tmp_path), _fault=fault,
        )
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 10
    assert get_checkpoint("synthetic", conn) == "10"
    conn.close()
