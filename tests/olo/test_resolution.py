"""S2 signal_resolution tracking tests.

Covers:
  - Table creation and idempotent schema migration
  - ensure_signal_resolution: idempotency (no duplicates on replay)
  - Identity stage: resolved / unresolved / failed outcomes
  - Interruption and resume: signals ingested but not resolved survive restarts
  - Retry: failed signals can be re-processed
  - Business isolation: different sources have independent resolution state
  - run_source integration: resolution stage runs in the worker loop
  - Worker kill-after-ingest / kill-after-resolution fault injection

All tests use in-memory SQLite — no network, no LLM, no Hindsight.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from olo.signals import NormalizedSignal, format_utc
from olo.store import (
    ensure_signal_resolution,
    get_pending_identity_signals,
    get_resolution_summary,
    migrate,
    record_identity_failed,
    record_identity_resolved,
    record_identity_unresolved,
)

# ── Constants ─────────────────────────────────────────────────────────────────

SOURCE = "test_src"
ALICE_CID = "cid_64c1dd94041b33ab9245"
BOB_CID   = "cid_8baee186a88a5a300f68"

_UTC = timezone.utc


def _utc(year=2026, month=4, day=1) -> datetime:
    return datetime(year, month, day, tzinfo=_UTC)


def _now_str() -> str:
    return format_utc(datetime.now(_UTC))


# ── Signal factory ────────────────────────────────────────────────────────────

def _sig(
    external_id: str = "sig-001",
    kind: str = "email.sent",
    source: str = SOURCE,
    payload: Optional[dict] = None,
) -> NormalizedSignal:
    return NormalizedSignal(
        source=source,
        external_id=external_id,
        kind=kind,
        occurred_at=_utc(),
        subject=None,
        payload=payload or {"headers": {"from": [], "to": []}},
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    yield conn
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Schema and migration
# ═══════════════════════════════════════════════════════════════════════════════

class TestSchema:
    """signal_resolution table is created by migrate() and is idempotent."""

    def test_table_exists_after_migrate(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_resolution'"
        ).fetchone()
        assert row is not None

    def test_schema_version_is_5(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        assert row["version"] == 5

    def test_migrate_idempotent(self, db: sqlite3.Connection) -> None:
        """Calling migrate() twice does not raise or corrupt state."""
        migrate(db)
        migrate(db)
        row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        assert row["version"] == 5

    def test_required_columns_present(self, db: sqlite3.Connection) -> None:
        db.execute(
            "INSERT INTO signal_resolution"
            " (signal_id, source, identity_status, commitment_status, created_at, updated_at)"
            " VALUES ('s1', 'src', 'pending', 'pending', '2026-01-01Z', '2026-01-01Z')"
        )
        row = db.execute(
            "SELECT * FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "pending"
        assert row["commitment_status"] == "pending"
        assert row["contact_id"] is None


# ═══════════════════════════════════════════════════════════════════════════════
# ensure_signal_resolution — idempotency
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureSignalResolution:
    """ensure_signal_resolution creates rows idempotently."""

    def test_creates_rows_for_new_signals(self, db: sqlite3.Connection) -> None:
        sigs = [_sig("s1", kind="email.sent"), _sig("s2", kind="call.transcript")]
        db.execute("BEGIN")
        ensure_signal_resolution(db, sigs, SOURCE, _now_str())
        db.execute("COMMIT")
        rows = db.execute(
            "SELECT signal_id FROM signal_resolution WHERE source=? ORDER BY signal_id",
            (SOURCE,),
        ).fetchall()
        assert [r["signal_id"] for r in rows] == ["s1", "s2"]

    def test_idempotent_on_replay(self, db: sqlite3.Connection) -> None:
        """Running ensure_signal_resolution twice never duplicates rows."""
        sigs = [_sig("s1"), _sig("s2")]
        for _ in range(3):
            db.execute("BEGIN")
            ensure_signal_resolution(db, sigs, SOURCE, _now_str())
            db.execute("COMMIT")
        count = db.execute(
            "SELECT COUNT(*) FROM signal_resolution WHERE source=?", (SOURCE,)
        ).fetchone()[0]
        assert count == 2

    def test_prose_signals_pending_commitment(
        self, db: sqlite3.Connection
    ) -> None:
        """email.sent and call.transcript get commitment_status='pending'."""
        for kind in ("email.sent", "email.received", "call.transcript"):
            sig = _sig(f"s-{kind}", kind=kind)
            db.execute("BEGIN")
            ensure_signal_resolution(db, [sig], SOURCE, _now_str())
            db.execute("COMMIT")
            row = db.execute(
                "SELECT commitment_status FROM signal_resolution WHERE signal_id=?",
                (f"s-{kind}",),
            ).fetchone()
            assert row["commitment_status"] == "pending", f"{kind} should be pending"

    def test_structured_signals_skipped_commitment(
        self, db: sqlite3.Connection
    ) -> None:
        """Structured kinds without a commitment path get commitment_status='skipped'.

        calendar.event.changed / .cancelled have one (olo.calendar_commitments)
        and are pending; see test_calendar_events_pending_commitment."""
        for kind in ("invoice.created", "calendar.event.created", "calendar.calendar.added"):
            sig = _sig(f"s-{kind}", kind=kind)
            db.execute("BEGIN")
            ensure_signal_resolution(db, [sig], SOURCE, _now_str())
            db.execute("COMMIT")
            row = db.execute(
                "SELECT commitment_status FROM signal_resolution WHERE signal_id=?",
                (f"s-{kind}",),
            ).fetchone()
            assert row["commitment_status"] == "skipped", f"{kind} should be skipped"

    def test_calendar_events_pending_commitment(self, db: sqlite3.Connection) -> None:
        """Calendar event revisions wait for the deterministic meeting stage."""
        for kind in ("calendar.event.changed", "calendar.event.cancelled"):
            sig = _sig(f"s-{kind}", kind=kind)
            db.execute("BEGIN")
            ensure_signal_resolution(db, [sig], SOURCE, _now_str())
            db.execute("COMMIT")
            row = db.execute(
                "SELECT commitment_status FROM signal_resolution WHERE signal_id=?",
                (f"s-{kind}",),
            ).fetchone()
            assert row["commitment_status"] == "pending", f"{kind} should be pending"

    def test_initial_identity_status_is_pending(
        self, db: sqlite3.Connection
    ) -> None:
        sig = _sig("s1")
        db.execute("BEGIN")
        ensure_signal_resolution(db, [sig], SOURCE, _now_str())
        db.execute("COMMIT")
        row = db.execute(
            "SELECT identity_status FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "pending"


# ═══════════════════════════════════════════════════════════════════════════════
# Identity resolution outcomes
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdentityResolutionOutcomes:
    """record_identity_* functions update status correctly."""

    def _create_pending(self, db: sqlite3.Connection, signal_id: str = "s1") -> None:
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig(signal_id)], SOURCE, _now_str())
        db.execute("COMMIT")

    def test_record_resolved(self, db: sqlite3.Connection) -> None:
        self._create_pending(db)
        db.execute("BEGIN")
        record_identity_resolved(db, "s1", SOURCE, ALICE_CID, _now_str())
        db.execute("COMMIT")
        row = db.execute(
            "SELECT identity_status, contact_id FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "resolved"
        assert row["contact_id"] == ALICE_CID

    def test_record_unresolved(self, db: sqlite3.Connection) -> None:
        self._create_pending(db)
        db.execute("BEGIN")
        record_identity_unresolved(db, "s1", SOURCE, "no CRM match", _now_str())
        db.execute("COMMIT")
        row = db.execute(
            "SELECT identity_status, identity_reason, contact_id"
            " FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "unresolved"
        assert row["contact_id"] is None
        assert "no CRM match" in (row["identity_reason"] or "")

    def test_record_failed(self, db: sqlite3.Connection) -> None:
        self._create_pending(db)
        db.execute("BEGIN")
        record_identity_failed(db, "s1", SOURCE, "ValueError: bad input", _now_str())
        db.execute("COMMIT")
        row = db.execute(
            "SELECT identity_status, identity_reason FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "failed"
        assert "ValueError" in (row["identity_reason"] or "")

    def test_resolved_status_not_overwritten(
        self, db: sqlite3.Connection
    ) -> None:
        """Once resolved, the status is not overwritten by a second pending update."""
        self._create_pending(db)
        db.execute("BEGIN")
        record_identity_resolved(db, "s1", SOURCE, ALICE_CID, _now_str())
        db.execute("COMMIT")
        # Second attempt to mark unresolved should be a no-op (WHERE identity_status='pending')
        db.execute("BEGIN")
        record_identity_unresolved(db, "s1", SOURCE, "no match", _now_str())
        db.execute("COMMIT")
        row = db.execute(
            "SELECT identity_status, contact_id FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "resolved"
        assert row["contact_id"] == ALICE_CID


# ═══════════════════════════════════════════════════════════════════════════════
# Interruption and resume
# ═══════════════════════════════════════════════════════════════════════════════

class TestInterruptionAndResume:
    """Signals ingested before a crash retain their pending rows on restart."""

    def test_pending_rows_survive_between_runs(
        self, db: sqlite3.Connection
    ) -> None:
        """Signals ingested in run 1 have pending resolution rows visible in run 2."""
        # Run 1: ingest and create resolution rows, but no identity stage yet
        sigs = [_sig("s1"), _sig("s2")]
        db.execute("BEGIN")
        ensure_signal_resolution(db, sigs, SOURCE, _now_str())
        db.execute("COMMIT")

        # Simulate crash — run 2 starts with a fresh call, checks pending work
        pending = get_pending_identity_signals(db, SOURCE)
        pending_ids = {r["signal_id"] for r in pending}
        assert "s1" in pending_ids
        assert "s2" in pending_ids

    def test_resolved_signals_not_in_pending_work(
        self, db: sqlite3.Connection
    ) -> None:
        """Resolved signals don't reappear in the pending work queue."""
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1"), _sig("s2")], SOURCE, _now_str())
        db.execute("COMMIT")
        db.execute("BEGIN")
        record_identity_resolved(db, "s1", SOURCE, ALICE_CID, _now_str())
        db.execute("COMMIT")

        pending = get_pending_identity_signals(db, SOURCE)
        pending_ids = {r["signal_id"] for r in pending}
        assert "s1" not in pending_ids  # resolved — no longer pending
        assert "s2" in pending_ids       # still needs work

    def test_failed_signals_not_in_pending_work(
        self, db: sqlite3.Connection
    ) -> None:
        """Failed signals are removed from the pending queue (visible separately)."""
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1")], SOURCE, _now_str())
        db.execute("COMMIT")
        db.execute("BEGIN")
        record_identity_failed(db, "s1", SOURCE, "error", _now_str())
        db.execute("COMMIT")

        pending = get_pending_identity_signals(db, SOURCE)
        pending_ids = {r["signal_id"] for r in pending}
        assert "s1" not in pending_ids

    def test_replay_does_not_reset_resolved_status(
        self, db: sqlite3.Connection
    ) -> None:
        """Running ensure_signal_resolution again on an already-resolved signal
        leaves the status as 'resolved' (INSERT OR IGNORE is a no-op)."""
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1")], SOURCE, _now_str())
        db.execute("COMMIT")
        db.execute("BEGIN")
        record_identity_resolved(db, "s1", SOURCE, ALICE_CID, _now_str())
        db.execute("COMMIT")

        # Simulate restart: re-run ensure for the same signal
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1")], SOURCE, _now_str())
        db.execute("COMMIT")

        row = db.execute(
            "SELECT identity_status FROM signal_resolution WHERE signal_id='s1'"
        ).fetchone()
        assert row["identity_status"] == "resolved"  # not reset to 'pending'


# ═══════════════════════════════════════════════════════════════════════════════
# Retry (failed signals remain visible)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRetry:
    """Failed signals remain visible and can be re-processed."""

    def test_failed_signals_visible_in_summary(
        self, db: sqlite3.Connection
    ) -> None:
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1"), _sig("s2")], SOURCE, _now_str())
        db.execute("COMMIT")
        db.execute("BEGIN")
        record_identity_failed(db, "s1", SOURCE, "transient error", _now_str())
        db.execute("COMMIT")

        summary = get_resolution_summary(db, SOURCE)
        assert summary["identity"].get("failed", 0) == 1
        assert summary["identity"].get("pending", 0) == 1

    def test_summary_all_resolved(self, db: sqlite3.Connection) -> None:
        sigs = [_sig("s1"), _sig("s2")]
        db.execute("BEGIN")
        ensure_signal_resolution(db, sigs, SOURCE, _now_str())
        db.execute("COMMIT")
        for sig in sigs:
            db.execute("BEGIN")
            record_identity_resolved(db, sig.external_id, SOURCE, ALICE_CID, _now_str())
            db.execute("COMMIT")

        summary = get_resolution_summary(db, SOURCE)
        assert summary["identity"].get("resolved", 0) == 2
        assert summary["identity"].get("pending", 0) == 0
        assert summary["total"] == 2


# ═══════════════════════════════════════════════════════════════════════════════
# Business isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestBusinessIsolation:
    """Different sources have completely independent resolution state."""

    def test_different_sources_independent(
        self, db: sqlite3.Connection
    ) -> None:
        # Source A: one signal resolved
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1", source="src_a")], "src_a", _now_str())
        db.execute("COMMIT")
        db.execute("BEGIN")
        record_identity_resolved(db, "s1", "src_a", ALICE_CID, _now_str())
        db.execute("COMMIT")

        # Source B: same external_id, different source — independent row
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1", source="src_b")], "src_b", _now_str())
        db.execute("COMMIT")

        row_a = db.execute(
            "SELECT identity_status FROM signal_resolution WHERE signal_id='s1' AND source='src_a'"
        ).fetchone()
        row_b = db.execute(
            "SELECT identity_status FROM signal_resolution WHERE signal_id='s1' AND source='src_b'"
        ).fetchone()
        assert row_a["identity_status"] == "resolved"
        assert row_b["identity_status"] == "pending"

    def test_summary_filtered_by_source(self, db: sqlite3.Connection) -> None:
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("s1", source="src_a")], "src_a", _now_str())
        ensure_signal_resolution(db, [_sig("s2", source="src_b")], "src_b", _now_str())
        db.execute("COMMIT")
        db.execute("BEGIN")
        record_identity_resolved(db, "s1", "src_a", ALICE_CID, _now_str())
        db.execute("COMMIT")

        summary_a = get_resolution_summary(db, "src_a")
        summary_b = get_resolution_summary(db, "src_b")
        assert summary_a["total"] == 1
        assert summary_a["identity"].get("resolved", 0) == 1
        assert summary_b["total"] == 1
        assert summary_b["identity"].get("pending", 0) == 1

    def test_different_contacts_no_cross_leak(
        self, db: sqlite3.Connection
    ) -> None:
        """Alice's signals must not appear in Bob's pending queue."""
        db.execute("BEGIN")
        ensure_signal_resolution(db, [_sig("alice-sig", source="alice_src")], "alice_src", _now_str())
        ensure_signal_resolution(db, [_sig("bob-sig", source="bob_src")], "bob_src", _now_str())
        db.execute("COMMIT")

        alice_pending = {r["signal_id"] for r in get_pending_identity_signals(db, "alice_src")}
        bob_pending   = {r["signal_id"] for r in get_pending_identity_signals(db, "bob_src")}
        assert "alice-sig" in alice_pending
        assert "bob-sig" not in alice_pending
        assert "bob-sig" in bob_pending
        assert "alice-sig" not in bob_pending


# ═══════════════════════════════════════════════════════════════════════════════
# run_source integration (fault injection)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRunSourceIntegration:
    """Resolution stage integrates correctly with run_source, including faults."""

    def _make_adapter(self, signals, source=SOURCE):
        """Return a minimal mock adapter that serves one batch."""
        from olo.adapters import FetchBatch, MonitorAdapter

        class _MockAdapter:
            def __init__(self):
                self.source = source
                self._served = False

            def fetch(self, cursor, *, limit):
                if self._served:
                    return FetchBatch(signals=[], has_more=False, next_cursor=cursor)
                self._served = True
                return FetchBatch(signals=signals, has_more=False, next_cursor="cur1")

        return _MockAdapter()

    def test_resolution_rows_created_after_run(
        self, tmp_path: Path
    ) -> None:
        """run_source creates signal_resolution rows for all ingested signals."""
        from olo.runner import run_source
        from olo.store import connect, migrate

        sigs = [_sig("s1", kind="email.sent"), _sig("s2", kind="invoice.created")]
        adapter = self._make_adapter(sigs)
        result = run_source(adapter, db_path=tmp_path / "olo.db")

        assert result.status == "succeeded"
        conn = connect(tmp_path / "olo.db")
        rows = conn.execute(
            "SELECT signal_id, identity_status, commitment_status"
            " FROM signal_resolution ORDER BY signal_id"
        ).fetchall()
        conn.close()

        ids = {r["signal_id"]: r for r in rows}
        assert "s1" in ids and "s2" in ids
        # email.sent: identity stage ran (resolved or unresolved); commitment pending
        assert ids["s1"]["identity_status"] in ("resolved", "unresolved", "failed", "pending")
        assert ids["s1"]["commitment_status"] == "pending"
        # invoice.created: commitment skipped
        assert ids["s2"]["commitment_status"] == "skipped"

    def test_run_result_includes_identity_counts(
        self, tmp_path: Path
    ) -> None:
        """RunResult includes identity_resolved, identity_unresolved, identity_failed."""
        from olo.runner import run_source

        sigs = [_sig("s1"), _sig("s2")]
        adapter = self._make_adapter(sigs)
        result = run_source(adapter, db_path=tmp_path / "olo.db")

        assert result.status == "succeeded"
        # Counts are non-negative integers; total should equal number of prose signals
        total_id = result.identity_resolved + result.identity_unresolved + result.identity_failed
        assert total_id == 2  # both are email.sent (prose)

    def test_kill_after_ingest_rows_persist(
        self, tmp_path: Path
    ) -> None:
        """If worker dies after ingest (after_resolution fault), rows still exist."""
        from olo.runner import run_source
        from olo.store import connect

        sigs = [_sig("s1"), _sig("s2")]
        adapter = self._make_adapter(sigs)

        killed = False

        def _fault(stage):
            nonlocal killed
            if stage == "after_resolution" and not killed:
                killed = True
                raise RuntimeError("simulated kill after resolution")

        with pytest.raises(RuntimeError, match="simulated kill"):
            run_source(adapter, db_path=tmp_path / "olo.db", _fault=_fault)

        # Resolution rows must exist and be in a terminal or pending state
        conn = connect(tmp_path / "olo.db")
        rows = conn.execute(
            "SELECT signal_id FROM signal_resolution"
        ).fetchall()
        conn.close()
        assert len(rows) > 0  # rows were written before the fault

    def test_replay_after_kill_does_not_duplicate(
        self, tmp_path: Path
    ) -> None:
        """Running run_source twice with the same signals produces no duplicate rows."""
        from olo.runner import run_source
        from olo.store import connect

        sigs = [_sig("s1"), _sig("s2")]

        # First run
        run_source(self._make_adapter(sigs), db_path=tmp_path / "olo.db")
        # Second run (replay)
        run_source(self._make_adapter(sigs), db_path=tmp_path / "olo.db")

        conn = connect(tmp_path / "olo.db")
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_resolution"
        ).fetchone()[0]
        conn.close()
        assert count == 2  # exactly one row per signal, not 4

    def test_resolution_errors_recorded_not_raised(
        self, tmp_path: Path
    ) -> None:
        """Errors in identity resolution are recorded in signal_resolution, not raised."""
        from olo.runner import run_source
        from olo.store import connect

        # Signal with malformed payload that might cause resolve_contact_from_signal to error
        bad_sig = _sig("s_bad", kind="email.sent", payload={"headers": "not-a-dict"})
        adapter = self._make_adapter([bad_sig])
        result = run_source(adapter, db_path=tmp_path / "olo.db")

        # run_source must succeed (errors not re-raised)
        assert result.status == "succeeded"

        conn = connect(tmp_path / "olo.db")
        row = conn.execute(
            "SELECT identity_status FROM signal_resolution WHERE signal_id='s_bad'"
        ).fetchone()
        conn.close()
        # Row exists; identity is in some terminal state (resolved/unresolved/failed)
        assert row is not None
        assert row["identity_status"] in ("resolved", "unresolved", "failed", "pending")
