"""tests/olo/test_schema_upgrade.py — S5: upgrading a pre-S5 (v5) olo.db in place.

The fixture database has the signal_resolution table exactly as shipped
before S5 part 2 (no commitment_outcome / commitment_attempts) and none of
the S5 part 3 tracking tables.  migrate() must add them without touching
existing rows, keep schema version 5, and be idempotent.
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.store import connect, migrate

pytestmark = pytest.mark.not_integration

_PRE_S5_SIGNAL_RESOLUTION = """CREATE TABLE signal_resolution (
    signal_id           TEXT NOT NULL,
    source              TEXT NOT NULL,
    identity_status     TEXT NOT NULL DEFAULT 'pending'
        CHECK (identity_status IN ('pending','resolved','unresolved','ambiguous','skipped','failed')),
    contact_id          TEXT,
    identity_reason     TEXT,
    commitment_status   TEXT NOT NULL DEFAULT 'pending'
        CHECK (commitment_status IN ('pending','resolved','unresolved','skipped','failed')),
    commitment_id       TEXT,
    commitment_version  INTEGER,
    commitment_reason   TEXT,
    processed_at        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (signal_id, source)
)"""

_S5_TABLES = ("hindsight_operations", "hindsight_operation_docs",
              "hindsight_budget_window", "olo_bindings")


def _pre_s5_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (5)")
    conn.execute(_PRE_S5_SIGNAL_RESOLUTION)
    conn.execute(
        "INSERT INTO signal_resolution (signal_id, source, identity_status, contact_id,"
        " commitment_status, created_at, updated_at)"
        " VALUES ('m1','gmail_biz','resolved','cid_a','pending','t0','t0')")
    conn.commit()
    conn.close()


def _columns(conn, table: str) -> dict[str, tuple]:
    return {r[1]: (r[2], r[3], r[4]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_pre_s5_database_upgrades_in_place(tmp_path) -> None:
    db = tmp_path / "olo.db"
    _pre_s5_db(db)
    conn = connect(db)
    assert "commitment_outcome" not in _columns(conn, "signal_resolution")

    migrate(conn)
    cols = _columns(conn, "signal_resolution")
    assert cols["commitment_outcome"][0] == "TEXT"
    assert cols["commitment_attempts"] == ("INTEGER", 1, "0")
    row = conn.execute("SELECT * FROM signal_resolution").fetchone()
    assert (row["signal_id"], row["contact_id"], row["identity_status"]) == ("m1", "cid_a", "resolved")
    assert row["commitment_attempts"] == 0 and row["commitment_outcome"] is None
    assert set(_S5_TABLES) <= _tables(conn)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 5


def test_repeat_migrations_are_idempotent(tmp_path) -> None:
    db = tmp_path / "olo.db"
    _pre_s5_db(db)
    conn = connect(db)
    migrate(conn)
    snapshot = (_columns(conn, "signal_resolution"), _tables(conn),
                conn.execute("SELECT * FROM signal_resolution").fetchall())
    for _ in range(3):
        migrate(conn)
    assert (_columns(conn, "signal_resolution"), _tables(conn),
            conn.execute("SELECT * FROM signal_resolution").fetchall()) == snapshot
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1


def test_fresh_database_has_the_same_shape(tmp_path) -> None:
    upgraded, fresh = tmp_path / "up.db", tmp_path / "fresh.db"
    _pre_s5_db(upgraded)
    up, fr = connect(upgraded), connect(fresh)
    migrate(up)
    migrate(fr)
    assert set(_columns(up, "signal_resolution")) == set(_columns(fr, "signal_resolution"))
    for table in _S5_TABLES:
        assert _columns(up, table) == _columns(fr, table)


def test_upgraded_database_runs_the_pipeline(tmp_path) -> None:
    """The commitment stage works on an upgraded database's existing rows."""
    from olo.commitments import _record

    db = tmp_path / "olo.db"
    _pre_s5_db(db)
    conn = connect(db)
    migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    _record(conn, "gmail_biz", "m1", "failed", "model_error", "x", "t1", attempted=True)
    conn.execute("COMMIT")
    row = conn.execute("SELECT commitment_status, commitment_attempts FROM signal_resolution"
                       " WHERE signal_id='m1'").fetchone()
    assert (row[0], row[1]) == ("failed", 1)


_PART3_OPERATIONS = """CREATE TABLE hindsight_operations (
    operation_id  TEXT NOT NULL PRIMARY KEY,
    bank_id       TEXT NOT NULL,
    source        TEXT NOT NULL,
    submitted_at  TEXT NOT NULL,
    doc_count     INTEGER NOT NULL,
    prose_count   INTEGER NOT NULL,
    status        TEXT NOT NULL
        CHECK (status IN ('submitted','completed','failed','cancelled','lost')),
    settled_at    TEXT,
    detail        TEXT
)"""


def test_part3_operations_gain_kind_and_keep_rows(tmp_path) -> None:
    """A database written by S5 part 3 (operations without ``kind``) keeps its
    operations, which are all retains, and accepts consolidation rows."""
    db = tmp_path / "olo.db"
    _pre_s5_db(db)
    raw = sqlite3.connect(str(db))
    raw.execute(_PART3_OPERATIONS)
    raw.execute("INSERT INTO hindsight_operations VALUES"
                " ('op1','olo-b','gmail_biz','t0',2,1,'completed','t1',NULL)")
    raw.commit()
    raw.close()

    conn = connect(db)
    migrate(conn)
    migrate(conn)
    assert [tuple(r) for r in conn.execute(
        "SELECT operation_id, kind, status FROM hindsight_operations")] == [
        ("op1", "retain", "completed")]
    conn.execute("INSERT INTO hindsight_operations (operation_id, bank_id, source,"
                 " submitted_at, doc_count, prose_count, status, kind)"
                 " VALUES ('c1','olo-b','gmail_biz','t2',0,0,'submitted','consolidation')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO hindsight_operations (operation_id, bank_id, source,"
                     " submitted_at, doc_count, prose_count, status, kind)"
                     " VALUES ('x1','olo-b','gmail_biz','t2',0,0,'submitted','other')")
    conn.close()
