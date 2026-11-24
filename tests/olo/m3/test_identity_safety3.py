"""Identity Safety Fix 3/3 — Tests for persistent contact IDs, legacy migration,
and verifiable authorization boundary.

Fix 1 — Persistent production contact IDs:
  1. Production contact ID survives restart and repeated imports.
  2. Primary email and display-name changes preserve the ID.
  3. Unknown contacts are not fabricated.

Fix 2 — Legacy identity migration:
  4. Migration dry run reports exact affected IDs and documents.
  5. Ambiguous migration mappings fail closed.
  6. Successful migration preserves evidence and updates Hindsight links.
  7. Migration failure does not silently leave a partially certified bank.

Fix 3 — Verifiable authorization boundary:
  8. Unauthorized conflict resolution fails.
  9. Authorized resolution records a verified actor.

Regressions (unchanged behaviour):
  10. Tenant isolation and revocation behaviour remain correct.
  11. Phone normalization and conflict history remain correct.
  12. Frozen benchmark corpus and full-item fingerprint remain unchanged.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from olo.adapters.contacts import (
    make_contact_id,
    make_fixture_contact_id,
    migrate_contacts,
    populate_contacts_from_crm,
    load_contact_index,
    resolve_contact_from_signal,
    resolve_identifier_conflict,
    get_identifier_conflicts,
    get_identifier_ownership_history,
    normalize_phone,
    PhoneNormalizationError,
    IdentityResolutionError,
    AUTHORIZED_OPERATORS,
    OperatorToken,
    create_operator_token,
    mint_contact_id,
    create_verified_contact,
    plan_migration,
    execute_migration,
    verify_migration,
    LegacyContactMapping,
    MigrationPlan,
    MigrationResult,
)
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email


ALICE_EMAIL = "alice.chen@acme-corp.example"
BOB_EMAIL = "bob.torres@beta-tech.example"


def _db(tmp_path: Path, subdir: str) -> sqlite3.Connection:
    from olo.store import connect, migrate
    db_path = tmp_path / subdir / "olo.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    migrate_contacts(conn)
    return conn


def _db_path(tmp_path: Path, subdir: str) -> Path:
    return tmp_path / subdir / "olo.db"


def _fresh_conn(db_path: Path) -> sqlite3.Connection:
    from olo.store import connect, migrate
    conn = connect(db_path)
    migrate(conn)
    migrate_contacts(conn)
    return conn


# ---------------------------------------------------------------------------
# Fix 1 — Persistent production contact IDs
# ---------------------------------------------------------------------------

class TestPersistentContactIDs:
    """Production contact IDs must be random, opaque, and durable."""

    def test_production_id_survives_restart(self, tmp_path: Path) -> None:
        """Production contact ID must survive a DB close/reopen (simulated restart)."""
        db_path = _db_path(tmp_path, "prod1")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn1 = _fresh_conn(db_path)
        cid1 = create_verified_contact(
            "New Customer", "Acme", [("email", "new@acme.example")],
            conn1, source="api",
        )
        assert cid1.startswith("cid_"), f"Expected cid_ prefix, got {cid1!r}"
        conn1.close()

        # Simulated restart: open a fresh connection to the same file
        conn2 = _fresh_conn(db_path)
        cid2 = create_verified_contact(
            "New Customer", "Acme", [("email", "new@acme.example")],
            conn2, source="api",
        )
        assert cid2 == cid1, (
            f"Contact ID must survive restart. First={cid1!r}, second={cid2!r}"
        )

        # Also survives a repeated import with no changes
        cid3 = create_verified_contact(
            "New Customer", "Acme", [("email", "new@acme.example")],
            conn2, source="api",
        )
        assert cid3 == cid1, "Repeated import must return the same ID"
        conn2.close()

    def test_email_and_name_changes_preserve_id(self, tmp_path: Path) -> None:
        """Adding a new email or changing display name must NOT change the contact ID."""
        conn = _db(tmp_path, "prod2")

        cid_original = create_verified_contact(
            "Alice", "Acme",
            [("email", "alice@old.example")],
            conn, source="api",
        )
        assert cid_original.startswith("cid_")

        # Add a second email — must return the same ID
        cid_after_new_email = create_verified_contact(
            "Alice",
            "Acme",
            [("email", "alice@old.example"), ("email", "alice@new.example")],
            conn, source="api",
        )
        assert cid_after_new_email == cid_original, (
            "Adding a new email must not change the contact ID"
        )

        # Change display name — must return the same ID
        cid_after_rename = create_verified_contact(
            "Alice Smith",  # name changed
            "Acme",
            [("email", "alice@old.example")],
            conn, source="api",
        )
        assert cid_after_rename == cid_original, (
            "Renaming the contact must not change the contact ID"
        )

        # Lookup via the new email must also return the same ID
        idx, _ = load_contact_index(conn)
        assert idx.get(("email", "alice@new.example")) == cid_original, (
            "New email must resolve to the same contact ID"
        )
        conn.close()

    def test_unknown_contact_not_fabricated(self, tmp_path: Path) -> None:
        """create_verified_contact with no valid identifiers must return '' not create."""
        conn = _db(tmp_path, "prod3")

        # Empty identifier list → unknown identity
        result = create_verified_contact(
            "Unknown Person", "", [], conn, source="api"
        )
        assert result == "", "Unknown identity must return '', not a fabricated ID"

        # Only an invalid phone (no region for national number) → no valid identifiers
        result2 = create_verified_contact(
            "Unknown", "", [("phone", "5551234567")],
            conn, source="api",  # no default_region
        )
        assert result2 == "", (
            "Contact with only invalid identifiers must not be created"
        )

        # Verify no contacts were created in the DB
        n = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        assert n == 0, f"No contacts should have been created. Got {n}."
        conn.close()

    def test_mint_contact_id_is_random_and_opaque(self) -> None:
        """mint_contact_id must produce unique, opaque IDs with no mutable data."""
        ids = {mint_contact_id() for _ in range(50)}
        assert len(ids) == 50, "mint_contact_id must produce unique IDs"
        for cid in ids:
            assert cid.startswith("cid_"), "ID must have cid_ prefix"
            # Must not contain @, +, or other email/phone characters
            assert "@" not in cid and "+" not in cid, (
                f"ID must be opaque (no email/phone chars): {cid!r}"
            )

    def test_fixture_ids_deterministic_for_benchmark(self) -> None:
        """Fixture-assigned IDs must be deterministic across builds."""
        from olo.m3.synthetic.crm_fixture import _ALICE_CID, _BOB_CID
        alice_via_email = get_contact_id_for_email(ALICE_EMAIL)
        assert alice_via_email == _ALICE_CID, (
            "Fixture Alice ID must match the hardcoded constant"
        )
        # Fixture IDs must NOT look like UUID4 (random) IDs from mint_contact_id
        assert len(_ALICE_CID) < 36, "Fixture IDs are sha256-derived (< 36 chars)"


# ---------------------------------------------------------------------------
# Fix 2 — Legacy identity migration
# ---------------------------------------------------------------------------

class TestLegacyMigration:
    """Migration from email-derived legacy IDs to stable CRM-assigned IDs."""

    def _insert_legacy_contact(
        self, conn: sqlite3.Connection, email: str, display_name: str
    ) -> str:
        """Insert a legacy email-derived contact into the DB and return its ID."""
        legacy_id = make_contact_id(email)
        now = "2024-01-01T00:00:00+00:00"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO contacts"
            " (id, organization_id, display_name, source, created_at, updated_at)"
            " VALUES (?, NULL, ?, 'old_crm', ?, ?)",
            (legacy_id, display_name, now, now),
        )
        conn.execute(
            "INSERT INTO contact_identifiers"
            " (identifier_type, identifier_value, contact_id, source, verified_at)"
            " VALUES ('email', ?, ?, 'old_crm', ?)",
            (email, legacy_id, now),
        )
        conn.execute("COMMIT")
        return legacy_id

    def test_dry_run_reports_affected_ids(self, tmp_path: Path) -> None:
        """Migration dry run must report the exact legacy IDs without modifying the DB."""
        conn = _db(tmp_path, "mig1")
        legacy_id = self._insert_legacy_contact(conn, ALICE_EMAIL, "Alice Chen")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        plan = plan_migration(conn, crm_contacts=CRM_CONTACTS, dry_run=True)

        assert plan.is_dry_run, "Plan must report dry_run=True"
        assert plan.is_safe, f"Plan must be safe (no ambiguous). ambiguous={plan.ambiguous}"
        assert any(m.legacy_id == legacy_id for m in plan.mappings), (
            f"Plan must include the legacy ID {legacy_id!r}"
        )
        mapping = next(m for m in plan.mappings if m.legacy_id == legacy_id)
        assert mapping.canonical_id == alice_cid, (
            f"Must map to CRM canonical ID {alice_cid!r}, got {mapping.canonical_id!r}"
        )
        assert mapping.verified_email == ALICE_EMAIL, (
            "verified_email must be the email that proves the mapping"
        )

        # Dry run must NOT modify the database
        still_there = conn.execute(
            "SELECT id FROM contacts WHERE id = ?", (legacy_id,)
        ).fetchone()
        assert still_there, "Dry run must not modify the database"
        conn.close()

    def test_ambiguous_mapping_fails_closed(self, tmp_path: Path) -> None:
        """When a legacy contact's emails map to two CRM entries, plan.is_safe is False."""
        conn = _db(tmp_path, "mig2")

        # Insert a legacy contact whose ID is derived from Alice's email,
        # but who also holds Bob's email — an inherently ambiguous situation.
        legacy_id = make_contact_id(ALICE_EMAIL)
        now = "2024-01-01T00:00:00+00:00"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO contacts"
            " (id, organization_id, display_name, source, created_at, updated_at)"
            " VALUES (?, NULL, 'AmbiguousContact', 'old_crm', ?, ?)",
            (legacy_id, now, now),
        )
        for email in [ALICE_EMAIL, BOB_EMAIL]:
            conn.execute(
                "INSERT INTO contact_identifiers"
                " (identifier_type, identifier_value, contact_id, source, verified_at)"
                " VALUES ('email', ?, ?, 'old_crm', ?)",
                (email, legacy_id, now),
            )
        conn.execute("COMMIT")

        plan = plan_migration(conn, crm_contacts=CRM_CONTACTS, dry_run=True)

        assert not plan.is_safe, "Plan must NOT be safe when mappings are ambiguous"
        assert plan.ambiguous, "Ambiguous list must be non-empty"
        assert any(a["legacy_id"] == legacy_id for a in plan.ambiguous)

        # execute_migration must refuse an unsafe plan
        with pytest.raises(ValueError, match="ambiguous"):
            execute_migration(conn, plan)

        # DB must be unchanged
        assert conn.execute(
            "SELECT id FROM contacts WHERE id = ?", (legacy_id,)
        ).fetchone(), "Database must be unchanged after refusing an ambiguous plan"
        conn.close()

    def test_successful_migration_preserves_evidence_and_updates_links(
        self, tmp_path: Path
    ) -> None:
        """Successful migration must update DB IDs and call retain_fn for Hindsight."""
        conn = _db(tmp_path, "mig3")
        legacy_id = self._insert_legacy_contact(conn, ALICE_EMAIL, "Alice Chen")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        retain_calls: list[dict] = []

        plan = plan_migration(conn, crm_contacts=CRM_CONTACTS, dry_run=False)
        result = execute_migration(
            conn, plan, retain_fn=lambda item: retain_calls.append(item)
        )

        # New canonical ID must be in contacts
        assert conn.execute(
            "SELECT id FROM contacts WHERE id = ?", (alice_cid,)
        ).fetchone(), f"New canonical ID {alice_cid!r} must exist in contacts"

        # Old legacy ID must be gone
        assert not conn.execute(
            "SELECT id FROM contacts WHERE id = ?", (legacy_id,)
        ).fetchone(), f"Legacy ID {legacy_id!r} must be removed"

        # contact_identifiers must reference the new ID
        row = conn.execute(
            "SELECT contact_id FROM contact_identifiers"
            " WHERE identifier_value = ?",
            (ALICE_EMAIL,),
        ).fetchone()
        assert row and row[0] == alice_cid, (
            f"contact_identifiers must reference new ID. Got {row!r}"
        )

        # retain_fn must have been called (Hindsight links updated)
        assert retain_calls, "retain_fn must be called for affected mappings"
        assert any(
            item.get("canonical_contact_id") == alice_cid
            for item in retain_calls
        ), "retain_fn must receive the canonical contact ID"

        # Result metadata
        assert result.contacts_updated >= 1
        assert result.is_complete, "Migration must report complete when no stale IDs remain"

        # verify_migration confirms no stale IDs
        verification = verify_migration(conn)
        assert verification["is_complete"]
        assert legacy_id not in verification["stale_legacy_ids"]
        conn.close()

    def test_failure_does_not_leave_partial_state(self, tmp_path: Path) -> None:
        """If retain_fn raises, the entire DB migration must roll back atomically."""
        conn = _db(tmp_path, "mig4")
        legacy_id = self._insert_legacy_contact(conn, ALICE_EMAIL, "Alice Chen")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        def failing_retain(item: dict) -> None:
            raise RuntimeError("Simulated retain failure — connection lost")

        plan = plan_migration(conn, crm_contacts=CRM_CONTACTS, dry_run=False)
        with pytest.raises(RuntimeError, match="Simulated retain failure"):
            execute_migration(conn, plan, retain_fn=failing_retain)

        # DB must be in its original state — no partially certified bank
        assert conn.execute(
            "SELECT id FROM contacts WHERE id = ?", (legacy_id,)
        ).fetchone(), "Legacy ID must still exist after rollback"

        assert not conn.execute(
            "SELECT id FROM contacts WHERE id = ?", (alice_cid,)
        ).fetchone(), "New canonical ID must NOT exist after rollback"

        # contact_identifiers must still reference the old legacy ID
        row = conn.execute(
            "SELECT contact_id FROM contact_identifiers"
            " WHERE identifier_value = ?",
            (ALICE_EMAIL,),
        ).fetchone()
        assert row and row[0] == legacy_id, (
            f"contact_identifiers must still reference legacy ID after rollback. Got {row!r}"
        )
        conn.close()


# ---------------------------------------------------------------------------
# Fix 3 — Verifiable authorization boundary
# ---------------------------------------------------------------------------

class TestAuthorizationBoundary:
    """replace string-based authorization with a verifiable OperatorToken."""

    def _setup_conflict(self, conn: sqlite3.Connection) -> tuple[str, str]:
        """Create two contacts sharing a phone, returning (cid_a, cid_b)."""
        cid_a = make_fixture_contact_id("test:auth3_a")
        cid_b = make_fixture_contact_id("test:auth3_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a3@auth.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", "a3@auth.com"), ("phone", "+18887771234")]},
            {"contact_id": cid_b, "canonical_email": "b3@auth.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", "b3@auth.com"), ("phone", "+18887771234")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fix")
        return cid_a, cid_b

    def test_unauthorized_resolution_fails_at_token_creation(
        self, tmp_path: Path
    ) -> None:
        """Unauthorized callers cannot obtain a token — rejected before any DB write."""
        conn = _db(tmp_path, "auth_unauth")
        cid_a, cid_b = self._setup_conflict(conn)

        conflicts_before = len(get_identifier_conflicts(conn, unresolved_only=True))

        # Any unrecognised name must raise IdentityResolutionError
        for bad_name in ("anyone", "", "crm_fixture", "crm_v1", "operator"):
            with pytest.raises(IdentityResolutionError, match="not an authorized operator"):
                create_operator_token(bad_name)

        # Conflict must remain untouched — no DB writes occurred
        conflicts_after = len(get_identifier_conflicts(conn, unresolved_only=True))
        assert conflicts_after == conflicts_before, (
            "Unauthorized token attempt must not modify the DB"
        )
        conn.close()

    def test_authorized_resolution_records_verified_actor(
        self, tmp_path: Path
    ) -> None:
        """Authorized resolution must record the authenticated actor in audit history."""
        conn = _db(tmp_path, "auth_ok")
        cid_a, cid_b = self._setup_conflict(conn)

        token = create_operator_token("admin")
        assert isinstance(token, OperatorToken)
        assert token.actor == "admin"

        resolve_identifier_conflict(
            "phone", "+18887771234", cid_b, token, conn
        )

        # Both transfer events must carry actor = "admin"
        history = get_identifier_ownership_history(
            conn, identifier_type="phone", identifier_value="+18887771234"
        )
        transfer_events = [
            h for h in history
            if h["event_type"] in ("transferred_in", "transferred_out")
        ]
        assert transfer_events, "Transfer events must exist in ownership history"
        for event in transfer_events:
            assert event["actor"] == "admin", (
                f"Actor must be 'admin'. Got {event['actor']!r}"
            )
        conn.close()

    def test_crm_admin_token_also_valid(self, tmp_path: Path) -> None:
        """crm_admin is also a recognized authorized operator."""
        conn = _db(tmp_path, "auth_crm_admin")
        cid_a, cid_b = self._setup_conflict(conn)

        token = create_operator_token("crm_admin")
        assert token.actor == "crm_admin"

        resolved = resolve_identifier_conflict(
            "phone", "+18887771234", cid_a, token, conn
        )
        assert resolved >= 1
        conn.close()

    def test_authorized_operators_set_unchanged(self) -> None:
        """AUTHORIZED_OPERATORS frozenset content must match the expected set."""
        assert "admin" in AUTHORIZED_OPERATORS
        assert "crm_admin" in AUTHORIZED_OPERATORS
        assert "" not in AUTHORIZED_OPERATORS
        assert "crm_fixture" not in AUTHORIZED_OPERATORS
        assert "crm_v1" not in AUTHORIZED_OPERATORS


# ---------------------------------------------------------------------------
# Regressions — unchanged behaviour
# ---------------------------------------------------------------------------

class TestRegressionCorrectness:
    """Existing behaviour must be unaffected by Fix 3/3 changes."""

    def test_tenant_isolation_and_revocation_correct(
        self, tmp_path: Path
    ) -> None:
        """Revoking an identifier in tenant A must not affect tenant B."""
        cid = make_fixture_contact_id("test:reg_tenant")
        fix = [{"contact_id": cid, "canonical_email": "x@tenant.com",
                "display_name": "X", "organization_name": "O",
                "identifiers": [("email", "x@tenant.com"), ("phone", "+19009009001")]}]

        conn_a = _db(tmp_path, "reg_tenant_a")
        conn_b = _db(tmp_path, "reg_tenant_b")

        populate_contacts_from_crm(fix, conn_a, source="crm")
        populate_contacts_from_crm(fix, conn_b, source="crm")

        # Revoke phone in tenant A via snapshot
        no_phone = [{**fix[0], "identifiers": [("email", "x@tenant.com")]}]
        populate_contacts_from_crm(
            no_phone, conn_a, source="crm_v2", snapshot_mode=True
        )

        idx_a, _ = load_contact_index(conn_a)
        assert idx_a.get(("phone", "+19009009001")) is None, (
            "Phone must be revoked in tenant A"
        )

        idx_b, _ = load_contact_index(conn_b)
        assert idx_b.get(("phone", "+19009009001")) == cid, (
            "Phone must be untouched in tenant B"
        )

        conn_a.close()
        conn_b.close()

    def test_phone_normalization_and_conflict_history_correct(
        self, tmp_path: Path
    ) -> None:
        """Phone normalization and conflict history must remain correct."""
        conn = _db(tmp_path, "reg_phone")
        cid_a = make_fixture_contact_id("test:reg_ph_a")
        cid_b = make_fixture_contact_id("test:reg_ph_b")

        fix_a = [{"contact_id": cid_a, "canonical_email": "a@ph.com",
                   "display_name": "A", "organization_name": "O",
                   "identifiers": [("email", "a@ph.com"), ("phone", "+18001231235")]}]
        fix_b = [{"contact_id": cid_b, "canonical_email": "b@ph.com",
                   "display_name": "B", "organization_name": "O",
                   # Same number in national format with US region
                   "identifiers": [("email", "b@ph.com"), ("phone", "8001231235")]}]

        populate_contacts_from_crm(fix_a, conn, source="crm_a")
        populate_contacts_from_crm(fix_b, conn, source="crm_b", default_region="US")

        # Both normalize to +18001231235 → conflict
        conflicts = get_identifier_conflicts(conn, unresolved_only=True)
        assert any(c["identifier_value"] == "+18001231235" for c in conflicts), (
            "Post-normalization collision must create a conflict"
        )

        # Resolve with a typed token
        token = create_operator_token("crm_admin")
        resolve_identifier_conflict(
            "phone", "+18001231235", cid_a, token, conn
        )

        # History must show transfer events with actor recorded
        history = get_identifier_ownership_history(
            conn, identifier_type="phone", identifier_value="+18001231235"
        )
        transfer_events = [h for h in history if "transfer" in h["event_type"]]
        assert transfer_events, "Transfer events must exist in history"
        assert all(e["actor"] == "crm_admin" for e in transfer_events), (
            "All transfer events must carry actor='crm_admin'"
        )

        # Phone now resolves to winner
        idx, _ = load_contact_index(conn)
        assert idx.get(("phone", "+18001231235")) == cid_a
        conn.close()

    def test_benchmark_fingerprint_unchanged(self, tmp_path: Path) -> None:
        """Frozen benchmark corpus full-item fingerprint must be unchanged."""
        from olo.m3.synthetic.generator import generate_timeline
        from olo.m3.feeder import populate_olo_db, build_hindsight_items
        from olo.m3.benchmark_preflight import (
            FROZEN_FULL_ITEM_FP,
            check_full_item_fingerprint,
        )

        tl = generate_timeline()
        db_path = tmp_path / "corpus" / "olo.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        populate_olo_db(tl, db_path)
        items = build_hindsight_items(db_path)

        result = check_full_item_fingerprint(items)
        assert result.ok, (
            f"Frozen benchmark fingerprint mismatch.\n"
            f"  Expected: {FROZEN_FULL_ITEM_FP}\n"
            f"  Got:      {result.actual_fp}\n"
            "The frozen corpus must not change — do not modify retrieval models, "
            "fixture identities, or scoring."
        )
