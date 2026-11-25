"""tests/olo/test_worker_recovery.py — S5 part 1: the worker recovers interrupted work.

Fault injection through run_source's ``_fault`` hook:

  after_ingest     — ingest committed, tracking rows not yet created
  before_identity  — tracking rows committed, identity stage not yet run
  after_backfill   — recovery itself interrupted on restart

Each crash is followed by a restart; the next run must backfill missing rows,
resume pending signals, stay idempotent on replay, never mark unrecorded work
complete, and never touch another source's signals.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo.adapters import FetchBatch
from olo.runner import run_source
from olo.signals import NormalizedSignal
from olo.store import connect, migrate

pytestmark = pytest.mark.not_integration

OWNER = "owner@biz.example"


class _Crash(Exception):
    pass


def _sig(n: int, source: str = "gmail_biz", sender: str | None = None) -> NormalizedSignal:
    return NormalizedSignal(
        source=source, external_id=f"m{n}", kind="email.received",
        occurred_at=datetime(2026, 4, 1, n, tzinfo=UTC), subject=f"Order {n}",
        payload={"headers": {"from": [{"email": sender or f"cust{n}@x.com", "name": f"C{n}"}],
                             "to": [{"email": OWNER, "name": "Owner"}]}},
    )


class _Adapter:
    """Serves *signals* once from cursor None; afterwards nothing new."""

    def __init__(self, signals: list[NormalizedSignal], source: str = "gmail_biz",
                 replay: bool = False) -> None:
        self.source = source
        self._signals = signals
        self._replay = replay

    def fetch(self, cursor, *, limit):
        if cursor is None or self._replay:
            self._replay = False
            return FetchBatch(signals=self._signals, has_more=False, next_cursor="c1")
        return FetchBatch(signals=[], has_more=False, next_cursor=cursor)


def _crash_at(point: str):
    def fault(name: str) -> None:
        if name == point:
            raise _Crash(point)
    return fault


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "olo.db"
    conn = connect(db)
    migrate(conn)
    conn.close()
    return db


def _statuses(db: Path, source: str = "gmail_biz") -> dict[str, str]:
    conn = connect(db)
    rows = conn.execute("SELECT signal_id, identity_status FROM signal_resolution"
                        " WHERE source=?", (source,)).fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _count(db: Path, sql: str) -> int:
    conn = connect(db)
    n = conn.execute(sql).fetchone()[0]
    conn.close()
    return n


class TestInterruptedIngest:
    def test_crash_after_ingest_is_backfilled_and_resolved(self, tmp_path) -> None:
        db = _db(tmp_path)
        sigs = [_sig(1), _sig(2), _sig(3)]
        with pytest.raises(_Crash):
            run_source(_Adapter(sigs), db_path=db, _fault=_crash_at("after_ingest"))
        assert _count(db, "SELECT COUNT(*) FROM signals") == 3
        assert _statuses(db) == {}                     # tracking rows were lost

        result = run_source(_Adapter(sigs), db_path=db)
        assert result.status == "succeeded"
        assert result.resolution_backfilled == 3
        assert result.identity_resumed == 3
        assert set(_statuses(db).values()) == {"resolved"}

    def test_crash_before_identity_resumes_pending(self, tmp_path) -> None:
        db = _db(tmp_path)
        sigs = [_sig(1), _sig(2)]
        with pytest.raises(_Crash):
            run_source(_Adapter(sigs), db_path=db, _fault=_crash_at("before_identity"))
        assert set(_statuses(db).values()) == {"pending"}

        result = run_source(_Adapter(sigs), db_path=db)
        assert result.resolution_backfilled == 0
        assert result.identity_resumed == 2 and result.identity_resolved == 2
        assert set(_statuses(db).values()) == {"resolved"}

    def test_crash_during_recovery_is_recovered_next_time(self, tmp_path) -> None:
        db = _db(tmp_path)
        sigs = [_sig(1)]
        with pytest.raises(_Crash):
            run_source(_Adapter(sigs), db_path=db, _fault=_crash_at("after_ingest"))
        with pytest.raises(_Crash):
            run_source(_Adapter(sigs), db_path=db, _fault=_crash_at("after_backfill"))
        assert _statuses(db) == {"m1": "pending"}      # backfilled, not yet processed

        result = run_source(_Adapter(sigs), db_path=db)
        assert result.identity_resumed == 1
        assert _statuses(db) == {"m1": "resolved"}


class TestIdempotency:
    def test_replay_changes_nothing(self, tmp_path) -> None:
        db = _db(tmp_path)
        sigs = [_sig(1), _sig(2)]
        run_source(_Adapter(sigs), db_path=db)
        before = (_statuses(db), _count(db, "SELECT COUNT(*) FROM contacts"),
                  _count(db, "SELECT COUNT(*) FROM signals"))

        replay = run_source(_Adapter(sigs, replay=True), db_path=db)
        assert replay.duplicates == 2 and replay.inserted == 0
        assert replay.resolution_backfilled == 0 and replay.identity_resumed == 0
        assert (_statuses(db), _count(db, "SELECT COUNT(*) FROM contacts"),
                _count(db, "SELECT COUNT(*) FROM signals")) == before

    def test_clean_restart_has_nothing_to_recover(self, tmp_path) -> None:
        db = _db(tmp_path)
        run_source(_Adapter([_sig(1)]), db_path=db)
        again = run_source(_Adapter([_sig(1)]), db_path=db)
        assert again.resolution_backfilled == 0 and again.identity_resumed == 0


class TestFailuresAreNotCompletion:
    def test_unrecorded_outcome_stays_pending_and_is_resumed(self, tmp_path, monkeypatch) -> None:
        from olo import runner

        db = _db(tmp_path)
        real = runner.record_identity_resolved
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk full")
            return real(*args, **kwargs)

        monkeypatch.setattr(runner, "record_identity_resolved", flaky)
        first = run_source(_Adapter([_sig(1)]), db_path=db)
        assert first.identity_failed == 1 and first.identity_resolved == 0
        assert _statuses(db) == {"m1": "pending"}      # never marked complete

        second = run_source(_Adapter([_sig(1)]), db_path=db)
        assert second.identity_resumed == 1 and second.identity_resolved == 1
        assert _statuses(db) == {"m1": "resolved"}


class TestBusinessIsolation:
    def test_recovery_only_touches_its_own_source(self, tmp_path) -> None:
        db = _db(tmp_path)
        with pytest.raises(_Crash):
            run_source(_Adapter([_sig(1, source="gmail_other")], source="gmail_other"),
                       db_path=db, _fault=_crash_at("after_ingest"))
        result = run_source(_Adapter([_sig(2)]), db_path=db)
        assert result.resolution_backfilled == 0
        assert _statuses(db, "gmail_other") == {}       # other source untouched
        assert _statuses(db) == {"m2": "resolved"}
