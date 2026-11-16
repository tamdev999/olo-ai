"""Tests for olo.store — schema, leases, ingest_batch, reads.

§8.3 (store portion) acceptance criteria.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone, UTC
from pathlib import Path

import pytest

from olo.signals import NormalizedSignal
from olo.store import (
    DEFAULT_LEASE_TTL,
    CheckpointMoved,
    LeaseLost,
    SourceBusy,
    acquire_run,
    connect,
    count_signals,
    finish_run,
    get_checkpoint,
    get_lease,
    get_run,
    ingest_batch,
    migrate,
    utc_now,
)
from tests.olo.conftest import FakeClock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _db(tmp_path: Path) -> Path:
    return tmp_path / "olo" / "olo.db"


def _signal(n: int, source: str = "synthetic") -> NormalizedSignal:
    return NormalizedSignal(
        source=source,
        external_id=f"syn-1-{n:05d}",
        kind="payment.failed",
        occurred_at=datetime(2026, 1, 1, 0, n % 60, n % 60, tzinfo=UTC),
        subject=None,
        payload={"seq": n},
    )


def _make_conn(tmp_path: Path) -> sqlite3.Connection:
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    return connect(db)


# ── Schema and permissions ────────────────────────────────────────────────────

def test_migrate_idempotent_and_permissions(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db)
    migrate(conn)
    migrate(conn)  # second call is idempotent

    row = conn.execute("SELECT version FROM schema_version").fetchall()
    assert len(row) == 1
    assert row[0]["version"] == 1

    # Permissions (POSIX only; Windows chmod is a no-op)
    if os.name != "nt":
        dir_mode = oct(os.stat(str(db.parent)).st_mode)[-3:]
        assert dir_mode == "700"
        file_mode = oct(os.stat(str(db)).st_mode)[-3:]
        assert file_mode == "600"

    conn.close()


def test_audit_events_append_only(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO audit_events(at, actor, action, subject_type, subject_id, detail)"
        " VALUES ('2026-01-01T00:00:00.000000Z','test','test.event',NULL,NULL,'{}')"
    )
    conn.execute("COMMIT")

    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE audit_events SET action='hacked' WHERE id=1")
        conn.execute("COMMIT")

    conn.execute("ROLLBACK")

    with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM audit_events WHERE id=1")
        conn.execute("COMMIT")

    conn.close()


# ── Lease acquire / source busy ───────────────────────────────────────────────

def test_acquire_run_creates_run_and_lease(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock)
    assert run_id

    run = get_run(run_id, conn)
    assert run is not None
    assert run["status"] == "running"

    lease = get_lease("synthetic", conn)
    assert lease is not None
    assert lease["run_id"] == run_id
    conn.close()


def test_second_acquire_raises_source_busy(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    acquire_run("synthetic", "host:1:0", conn, clock=clock)

    with pytest.raises(SourceBusy):
        acquire_run("synthetic", "host:2:0", conn, clock=clock)
    conn.close()


def test_expired_lease_allows_new_acquire(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    old_run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock, ttl=10)

    # Advance past TTL
    clock.advance(11)
    new_run_id = acquire_run("synthetic", "host:2:0", conn, clock=clock, ttl=DEFAULT_LEASE_TTL)
    assert new_run_id != old_run_id

    # Old run was abandoned
    old_run = get_run(old_run_id, conn)
    assert old_run["status"] == "abandoned"
    conn.close()


# ── finish_run ────────────────────────────────────────────────────────────────

def test_finish_run_removes_lease(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock)
    finish_run("synthetic", run_id, "succeeded", conn, clock=clock)

    run = get_run(run_id, conn)
    assert run["status"] == "succeeded"
    assert get_lease("synthetic", conn) is None
    conn.close()


def test_finish_run_noop_if_already_abandoned(tmp_path: Path) -> None:
    """If run was abandoned by a successor, finish_run is a no-op."""
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    old_id = acquire_run("synthetic", "host:1:0", conn, clock=clock, ttl=10)
    clock.advance(11)
    # Successor acquires, abandoning old run
    acquire_run("synthetic", "host:2:0", conn, clock=clock)
    # Finish the old run after it was abandoned
    finish_run("synthetic", old_id, "succeeded", conn, clock=clock)
    # Row should still be abandoned, not succeeded
    old_run = get_run(old_id, conn)
    assert old_run["status"] == "abandoned"
    conn.close()


# ── ingest_batch ──────────────────────────────────────────────────────────────

def test_ingest_batch_inserts_signals(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock)
    signals = tuple(_signal(n) for n in range(1, 4))
    inserted, dupes, conflicts = ingest_batch(
        "synthetic", run_id, signals, "3", None, conn, clock
    )
    assert inserted == 3
    assert dupes == 0
    assert conflicts == 0
    assert count_signals("synthetic", conn) == 3
    assert get_checkpoint("synthetic", conn) == "3"
    conn.close()


def test_ingest_batch_duplicate_no_op(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock)
    signals = tuple(_signal(n) for n in range(1, 4))
    ingest_batch("synthetic", run_id, signals, "3", None, conn, clock)
    # Second ingest of same signals
    inserted, dupes, conflicts = ingest_batch(
        "synthetic", run_id, signals, "3", "3", conn, clock
    )
    assert inserted == 0
    assert dupes == 3
    assert conflicts == 0
    assert count_signals("synthetic", conn) == 3
    conn.close()


def test_ingest_batch_conflict_keeps_stored(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock)
    original = _signal(1)
    ingest_batch("synthetic", run_id, (original,), "1", None, conn, clock)

    # Same external_id, different payload → conflict
    conflict = NormalizedSignal(
        source="synthetic",
        external_id="syn-1-00001",
        kind="payment.failed",
        occurred_at=datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC),
        subject=None,
        payload={"seq": 999, "changed": True},
    )
    _, dupes, conflicts = ingest_batch(
        "synthetic", run_id, (conflict,), "1", "1", conn, clock
    )
    assert dupes == 0
    assert conflicts == 1
    # Original row NOT overwritten
    row = conn.execute(
        "SELECT payload FROM signals WHERE source='synthetic' AND external_id='syn-1-00001'"
    ).fetchone()
    import json
    stored_payload = json.loads(row["payload"])
    assert stored_payload["seq"] == 1  # original, not 999
    conn.close()


def test_checkpoint_moved_guard(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock)
    signals = tuple(_signal(n) for n in range(1, 4))
    ingest_batch("synthetic", run_id, signals, "3", None, conn, clock)

    # Stale expected_cursor
    with pytest.raises(CheckpointMoved):
        ingest_batch("synthetic", run_id, signals, "6", None, conn, clock)
    # Nothing written from the failed batch
    assert count_signals("synthetic", conn) == 3
    conn.close()


def test_lease_lost_during_ingest(tmp_path: Path) -> None:
    conn = _make_conn(tmp_path)
    migrate(conn)
    clock = FakeClock()
    run_id = acquire_run("synthetic", "host:1:0", conn, clock=clock, ttl=10)
    signals = tuple(_signal(n) for n in range(1, 4))
    # First batch succeeds
    ingest_batch("synthetic", run_id, signals, "3", None, conn, clock)
    # Expire lease and let successor take over
    clock.advance(11)
    acquire_run("synthetic", "host:2:0", conn, clock=clock)
    # Old run_id tries to ingest
    more_signals = tuple(_signal(n) for n in range(4, 7))
    with pytest.raises(LeaseLost):
        ingest_batch("synthetic", run_id, more_signals, "6", "3", conn, clock)
    assert count_signals("synthetic", conn) == 3
    conn.close()


# ── Profile isolation ──────────────────────────────────────────────────────────

def test_profile_isolation(tmp_path: Path) -> None:
    db_a = tmp_path / "home_a" / "olo" / "olo.db"
    db_b = tmp_path / "home_b" / "olo" / "olo.db"
    db_a.parent.mkdir(parents=True)
    db_b.parent.mkdir(parents=True)
    conn_a = connect(db_a)
    conn_b = connect(db_b)
    migrate(conn_a)
    migrate(conn_b)
    clock = FakeClock()
    run_a = acquire_run("synthetic", "h:1:0", conn_a, clock=clock)
    acquire_run("synthetic", "h:2:0", conn_b, clock=clock)
    signals = ((_signal(1)),)
    ingest_batch("synthetic", run_a, signals, "1", None, conn_a, clock)
    # DB B is unaffected
    assert count_signals("synthetic", conn_b) == 0
    assert get_checkpoint("synthetic", conn_b) is None
    conn_a.close()
    conn_b.close()


# ── Directory permission enforcement ──────────────────────────────────────────

@pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
def test_olo_home_restricts_existing_wide_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """olo_home() must enforce 0o700 on a pre-existing 0o755 directory."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Create the olo/ directory with overly-wide permissions (0o755)
    olo_dir = home / "olo"
    olo_dir.mkdir(mode=0o755)

    from olo.paths import olo_home
    olo_home()

    mode = oct(os.stat(str(olo_dir)).st_mode)[-3:]
    assert mode == "700", (
        f"olo_home() did not restrict existing 0o755 directory; mode={mode}"
    )


@pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
def test_connect_restricts_existing_wide_db_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect() must enforce 0o700 on a pre-existing 0o755 parent directory."""
    db_dir = tmp_path / "olo"
    db_dir.mkdir(mode=0o755)
    db = db_dir / "olo.db"

    connect(db)

    mode = oct(os.stat(str(db_dir)).st_mode)[-3:]
    assert mode == "700", (
        f"connect() did not restrict existing 0o755 db parent; mode={mode}"
    )


@pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
def test_olo_home_raises_on_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """olo_home() must NOT silently swallow a chmod OSError on POSIX.

    We simulate a chmod failure by monkeypatching os.chmod to raise,
    then confirm that olo_home() propagates the error.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import olo.paths as paths_mod

    def _failing_chmod(path, mode, **kwargs):
        raise OSError(1, "Operation not permitted (simulated)")

    monkeypatch.setattr(paths_mod.os, "chmod", _failing_chmod)

    with pytest.raises(OSError, match="simulated"):
        paths_mod.olo_home()


@pytest.mark.skipif(os.name == "nt", reason="chmod is a no-op on Windows")
def test_connect_raises_on_dir_chmod_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """connect() must NOT silently swallow a directory chmod OSError on POSIX."""
    import olo.store as store_mod

    db_dir = tmp_path / "olo"
    db_dir.mkdir(mode=0o755)
    db = db_dir / "olo.db"

    # Make os.chmod raise only for the directory, not the file (which hasn't
    # been created yet when the directory chmod runs).
    real_chmod = os.chmod

    def _failing_dir_chmod(path, mode, **kwargs):
        # Raise only for the directory, not the db file itself
        if str(path) == str(db_dir):
            raise OSError(1, "Operation not permitted (simulated)")
        real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(store_mod.os, "chmod", _failing_dir_chmod)

    with pytest.raises(OSError, match="simulated"):
        store_mod.connect(db)
