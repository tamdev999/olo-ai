"""Tests for feeder.py entity metadata cross-tenant isolation.

Per Opus fourth-remediation audit P3:
- The old _ENTITY_META_LOADED global caused cross-tenant contamination when two
  tenants were processed in the same process sequentially: the first tenant's
  entities leaked into the second tenant's context, and colliding entity IDs
  returned the wrong tenant's display_name/company.
- The fix: _load_entity_meta_from_db returns a plain dict keyed by entity_id,
  bound locally in build_hindsight_items.  Each call gets a fresh, tenant-scoped
  snapshot.
"""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path

import pytest

from olo.m3.feeder import (
    _load_entity_meta_from_db,
    _get_entity_meta,
    FeedError,
)


def _make_entity_db(tmp_path: Path, entities: list[tuple[str, str, str]]) -> sqlite3.Connection:
    """Create an in-memory-style DB with entity_metadata pre-populated.

    entities: list of (entity_id, display_name, company)
    """
    db_path = tmp_path / "olo.db"
    from olo.store import connect, migrate
    conn = connect(db_path)
    migrate(conn)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute("BEGIN IMMEDIATE")
    for entity_id, display_name, company in entities:
        conn.execute(
            "INSERT OR REPLACE INTO entity_metadata"
            "(source, entity_id, display_name, company, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("test_source", entity_id, display_name, company, now, now),
        )
    conn.execute("COMMIT")
    return conn


class TestEntityMetaIsolation:
    """_load_entity_meta_from_db returns tenant-scoped dicts, not shared globals."""

    def test_returns_dict_not_none(self, tmp_path: Path) -> None:
        conn = _make_entity_db(tmp_path / "a", [("ent_A", "Alice", "Acme")])
        result = _load_entity_meta_from_db(conn)
        assert isinstance(result, dict)
        conn.close()

    def test_returns_entity_from_this_connection(self, tmp_path: Path) -> None:
        conn = _make_entity_db(tmp_path / "b", [("ent_B", "Bob", "Beta Corp")])
        result = _load_entity_meta_from_db(conn)
        assert "ent_B" in result
        assert result["ent_B"] == ("Bob", "Beta Corp")
        conn.close()

    def test_missing_entity_returns_empty_strings(self, tmp_path: Path) -> None:
        conn = _make_entity_db(tmp_path / "c", [("ent_C", "Carol", "Gamma")])
        result = _load_entity_meta_from_db(conn)
        name, company = _get_entity_meta("ent_missing", result)
        assert name == ""
        assert company == ""
        conn.close()

    def test_two_tenants_do_not_contaminate(self, tmp_path: Path) -> None:
        """Core isolation test: two sequential calls return independent dicts.

        Before the fix: _ENTITY_META_LOADED = True after the first call, so
        the second tenant's connection was ignored and the first tenant's cache
        was reused.  Any entity_id collision returned the first tenant's data.
        """
        conn_a = _make_entity_db(
            tmp_path / "tenant_a",
            [("ent_01", "Alice Smith", "Acme Corp")],
        )
        conn_b = _make_entity_db(
            tmp_path / "tenant_b",
            [("ent_01", "Bob Jones", "Beta LLC")],  # same entity_id, different business
        )
        cache_a = _load_entity_meta_from_db(conn_a)
        cache_b = _load_entity_meta_from_db(conn_b)

        name_a, company_a = _get_entity_meta("ent_01", cache_a)
        name_b, company_b = _get_entity_meta("ent_01", cache_b)

        assert name_a == "Alice Smith", (
            f"Tenant A's entity 'ent_01' should be Alice Smith, got {name_a!r}. "
            "Cross-tenant contamination detected."
        )
        assert name_b == "Bob Jones", (
            f"Tenant B's entity 'ent_01' should be Bob Jones, got {name_b!r}. "
            "Cross-tenant contamination detected."
        )
        assert company_a == "Acme Corp"
        assert company_b == "Beta LLC"

        conn_a.close()
        conn_b.close()

    def test_three_tenants_independent(self, tmp_path: Path) -> None:
        """Three tenants with overlapping entity IDs stay isolated."""
        for i, (name, company) in enumerate([
            ("Alice", "Acme"),
            ("Bob", "Beta"),
            ("Carol", "Gamma"),
        ]):
            conn = _make_entity_db(
                tmp_path / f"t{i}",
                [("shared_id", name, company)],
            )
            cache = _load_entity_meta_from_db(conn)
            n, c = _get_entity_meta("shared_id", cache)
            assert n == name, (
                f"Tenant {i}: expected {name!r}, got {n!r}. Cross-tenant leak."
            )
            conn.close()

    def test_empty_table_returns_empty_dict(self, tmp_path: Path) -> None:
        """An empty entity_metadata table returns an empty dict (no entities)."""
        conn = _make_entity_db(tmp_path / "empty", [])
        result = _load_entity_meta_from_db(conn)
        assert result == {}
        conn.close()

    def test_missing_table_returns_empty_dict(self, tmp_path: Path) -> None:
        """If entity_metadata table doesn't exist, return empty dict (not raise)."""
        # Create a DB WITHOUT running migrate (no entity_metadata table)
        db_path = tmp_path / "no_entity_table.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Only create signals table, not entity_metadata
        conn.execute(
            "CREATE TABLE signals (id INTEGER PRIMARY KEY, source TEXT)"
        )
        result = _load_entity_meta_from_db(conn)
        assert result == {}, (
            "Missing entity_metadata table must return empty dict, not raise. "
            "This is the safe fallback for new businesses and partially populated DBs."
        )
        conn.close()

    def test_process_restart_replay_isolation(self, tmp_path: Path) -> None:
        """After 'restarting' (new connection), tenant data loads fresh."""
        db_path = tmp_path / "restart" / "olo.db"
        db_path.parent.mkdir()
        from olo.store import connect, migrate
        # First 'process': load tenant A
        conn1 = connect(db_path)
        migrate(conn1)
        now = "2026-01-01T00:00:00+00:00"
        conn1.execute("BEGIN IMMEDIATE")
        conn1.execute(
            "INSERT OR REPLACE INTO entity_metadata"
            "(source, entity_id, display_name, company, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            ("src", "ent_X", "Before Restart", "Old Corp", now, now),
        )
        conn1.execute("COMMIT")
        cache_before = _load_entity_meta_from_db(conn1)
        assert cache_before["ent_X"][0] == "Before Restart"
        conn1.close()

        # Update the data (simulate: connector adds a new name)
        conn2 = connect(db_path)
        conn2.execute("BEGIN IMMEDIATE")
        conn2.execute(
            "UPDATE entity_metadata SET display_name='After Restart', updated_at=?"
            " WHERE entity_id='ent_X'",
            (now,),
        )
        conn2.execute("COMMIT")
        cache_after = _load_entity_meta_from_db(conn2)
        assert cache_after["ent_X"][0] == "After Restart", (
            "After DB update and re-open, fresh load must reflect updated data."
        )
        conn2.close()
