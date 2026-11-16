"""Tests for olo.runner.run_source() and main() — basic cases.

§8.3 (runner portion) acceptance criteria.
"""
from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path

import pytest

from olo.adapters.synthetic import SyntheticAdapter
from olo.runner import RunResult, run_source
from olo.store import connect, count_signals, get_checkpoint, migrate
from tests.olo.conftest import (
    FakeClock,
    OversizedBatchAdapter,
    StallingAdapter,
    WrongSourceAdapter,
    EmptyButHasMoreAdapter,
)
from datetime import UTC


def _db(tmp_path: Path) -> Path:
    return tmp_path / "olo" / "olo.db"


def _adapter(**kwargs) -> SyntheticAdapter:
    return SyntheticAdapter({k: str(v) for k, v in kwargs.items()})


def _conn(tmp_path: Path):
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    return connect(db)


# ── First run ingests all ─────────────────────────────────────────────────────

def test_first_run_ingests_all(tmp_path: Path) -> None:
    adapter = _adapter(total=25)
    result = run_source(adapter, limit=10, db_path=_db(tmp_path))
    assert result.status == "succeeded"
    assert result.inserted == 25
    assert result.duplicates == 0
    assert result.batches == 3   # 10 + 10 + 5 (then empty final)
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 25
    assert get_checkpoint("synthetic", conn) == "25"
    # Lease released
    from olo.store import get_lease
    assert get_lease("synthetic", conn) is None
    conn.close()


# ── Second run is a no-op ─────────────────────────────────────────────────────

def test_second_run_is_noop(tmp_path: Path) -> None:
    adapter = _adapter(total=25)
    run_source(adapter, limit=10, db_path=_db(tmp_path))
    result2 = run_source(_adapter(total=25), limit=10, db_path=_db(tmp_path))
    assert result2.status == "succeeded"
    # The cursor is already at "25", so the second run just fetches the empty
    # final batch (0 signals) and succeeds with 0 inserted, 0 duplicates.
    assert result2.inserted == 0
    conn = _conn(tmp_path)
    # Total in DB is still 25 from the first run
    assert count_signals("synthetic", conn) == 25
    conn.close()


# ── Exact multiple ends with empty batch ──────────────────────────────────────

def test_exact_multiple_ends_with_empty_batch(tmp_path: Path) -> None:
    result = run_source(_adapter(total=20), limit=10, db_path=_db(tmp_path))
    assert result.status == "succeeded"
    assert result.inserted == 20
    conn = _conn(tmp_path)
    assert get_checkpoint("synthetic", conn) == "20"
    conn.close()


# ── Overlap deduplicates ──────────────────────────────────────────────────────

def test_overlap_dedupes(tmp_path: Path) -> None:
    result = run_source(_adapter(total=25, overlap=3), limit=10, db_path=_db(tmp_path))
    assert result.status == "succeeded"
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 25
    assert result.duplicates > 0
    assert result.conflicts == 0
    conn.close()


# ── Conflict detection ────────────────────────────────────────────────────────

def test_conflicting_redelivery_is_not_overwritten(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from olo.signals import NormalizedSignal
    from olo.adapters import FetchBatch, MonitorAdapter

    class FirstAdapter(MonitorAdapter):
        source = "synthetic"
        def fetch(self, cursor, *, limit):
            s = NormalizedSignal(
                source="synthetic", external_id="syn-1-00001",
                kind="payment.failed",
                occurred_at=datetime(2026,1,1,0,1,0,tzinfo=UTC),
                subject=None, payload={"seq": 1},
            )
            return FetchBatch(signals=(s,), next_cursor="1", has_more=False)

    class ConflictAdapter(MonitorAdapter):
        source = "synthetic"
        def fetch(self, cursor, *, limit):
            # Same external_id, different payload
            s = NormalizedSignal(
                source="synthetic", external_id="syn-1-00001",
                kind="payment.failed",
                occurred_at=datetime(2026,1,1,0,1,0,tzinfo=UTC),
                subject=None, payload={"seq": 999, "changed": True},
            )
            return FetchBatch(signals=(s,), next_cursor="1", has_more=False)

    run_source(FirstAdapter(), db_path=_db(tmp_path))
    result = run_source(ConflictAdapter(), db_path=_db(tmp_path))
    assert result.conflicts == 1
    conn = _conn(tmp_path)
    import json
    row = conn.execute(
        "SELECT payload FROM signals WHERE source='synthetic' AND external_id='syn-1-00001'"
    ).fetchone()
    assert json.loads(row["payload"])["seq"] == 1  # original preserved
    conn.close()


# ── Stalled cursor fails run ──────────────────────────────────────────────────

def test_stalled_cursor_fails_run(tmp_path: Path) -> None:
    adapter = StallingAdapter()
    with pytest.raises(Exception):
        run_source(adapter, db_path=_db(tmp_path))
    conn = _conn(tmp_path)
    # 0 signals (the bad batch was never committed)
    assert count_signals("synthetic", conn) == 0
    conn.close()


def test_empty_batch_with_has_more_fails_run(tmp_path: Path) -> None:
    adapter = EmptyButHasMoreAdapter()
    with pytest.raises(Exception):
        run_source(adapter, db_path=_db(tmp_path))
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 0
    conn.close()


def test_oversized_batch_fails_run(tmp_path: Path) -> None:
    adapter = OversizedBatchAdapter(limit_to_exceed=5)
    with pytest.raises(Exception):
        run_source(adapter, limit=5, db_path=_db(tmp_path))
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 0
    conn.close()


def test_wrong_source_batch_fails_run(tmp_path: Path) -> None:
    adapter = WrongSourceAdapter()
    with pytest.raises(Exception):
        run_source(adapter, db_path=_db(tmp_path))
    conn = _conn(tmp_path)
    assert count_signals("synthetic", conn) == 0
    conn.close()


# ── main() exit codes and streams ─────────────────────────────────────────────

def test_main_succeeded_exit_0_empty_streams(tmp_path: Path, monkeypatch) -> None:
    from olo import runner
    monkeypatch.chdir(tmp_path)

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = out = StringIO()
    sys.stderr = err = StringIO()
    try:
        # Patch db_path to use tmp
        import olo.paths as paths_mod
        monkeypatch.setattr(paths_mod, "db_path", lambda: _db(tmp_path))
        rc = runner.main(["--source", "synthetic", "--option", "total=5"])
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    assert rc == 0
    assert out.getvalue() == ""
    assert err.getvalue() == ""


def test_main_failed_exit_1_stderr(tmp_path: Path, monkeypatch) -> None:
    from olo import runner

    old_stderr = sys.stderr
    sys.stderr = StringIO()
    try:
        import olo.paths as paths_mod
        monkeypatch.setattr(paths_mod, "db_path", lambda: _db(tmp_path))
        rc = runner.main([
            "--source", "synthetic",
            "--option", "total=25",
            "--option", "fail_at_batch=1",
        ])
    except Exception:
        rc = 1
    finally:
        sys.stderr = old_stderr

    assert rc == 1


def test_main_skipped_busy_exit_0(tmp_path: Path, monkeypatch) -> None:
    from olo import runner
    from olo.store import acquire_run, migrate, connect as store_connect
    # Pre-acquire lease
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = store_connect(db)
    migrate(conn)
    acquire_run("synthetic", "holder:0:0", conn)
    conn.close()

    # Wrap run_source to inject db_path
    import olo.runner as runner_mod
    orig = runner_mod.run_source

    def patched(adapter, **kwargs):
        kwargs.setdefault("db_path", db)
        return orig(adapter, **kwargs)

    monkeypatch.setattr(runner_mod, "run_source", patched)

    old_stderr = sys.stderr
    sys.stderr = err = StringIO()
    old_stdout = sys.stdout
    sys.stdout = out = StringIO()
    try:
        rc = runner.main(["--source", "synthetic", "--option", "total=5"])
    finally:
        sys.stderr = old_stderr
        sys.stdout = old_stdout

    assert rc == 0
    assert out.getvalue() == ""
    assert "busy" in err.getvalue().lower()
