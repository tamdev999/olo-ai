"""Adversarial tests for the durable, resumable identity migration protocol.

Covers all nine required scenarios:

  1. Hindsight retain #2 fails after #1 succeeds.
  2. All retains succeed but SQLite commit fails.
  3. Process crashes between remote success and checkpoint.
  4. Resume after partial success.
  5. Repeated migration is a no-op.
  6. Ambiguous identifiers are rejected.
  7. All three legacy-ID formats are detected.
  8. A stale Hindsight link causes verification failure.
  9. Tenant isolation and evidence hashes remain intact.

Additional:
  - Dry-run produces manifest without modifying contacts tables.
  - step_alias renames cid_email_derived without touching ent_* docs.
  - SQLite commit of operation_id precedes every Hindsight call.

Conventions
-----------
- Tests use disposable in-memory or tmp_path SQLite databases.
- Hindsight is replaced with callable mocks (retain_fn, get_document_fn).
- Never mutates an existing bank or the production CRM fixture.
- State assertions read the identity_migration_docs table directly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from olo.adapters.contacts import (
    make_contact_id,
    migrate_contacts,
    populate_contacts_from_crm,
    load_contact_index,
)
from olo.adapters.entity_extractor import entity_id_from_email
from olo.adapters.contacts_migration import (
    LegacyMigrationAmbiguous,
    LegacyMigrationError,
    detect_legacy_format,
    discover_legacy_links,
    get_migration_status,
    migrate_migration_schema,
    plan_identity_migration,
    resume_migration,
    step_alias,
    step_retain,
    step_verify,
)
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ALICE_EMAIL = "alice.chen@acme-corp.example"
BOB_EMAIL = "bob.torres@beta-tech.example"


def _db(tmp_path: Path, subdir: str = "biz") -> sqlite3.Connection:
    from olo.store import connect, migrate
    db_path = tmp_path / subdir / "olo.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    migrate_contacts(conn)
    return conn


def _run_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _insert_legacy_contact(
    conn: sqlite3.Connection,
    email: str,
    display_name: str = "Test",
) -> str:
    """Insert a contact with an email-derived cid_ ID and return that ID."""
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


def _hindsight_doc(doc_id: str, entity_link: str) -> dict:
    """Minimal Hindsight document dict."""
    return {
        "document_id": doc_id,
        "entities": [{"text": entity_link, "type": "person"}],
        "content_sha256": hashlib.sha256(doc_id.encode()).hexdigest(),
    }


def _ok_retain(item: dict, op_id: str) -> None:
    """Mock retain that always succeeds."""


def _ok_get_doc(doc_id: str, entity_link: str) -> dict:
    """Return a document dict with the expected entity link."""
    return {"document_id": doc_id, "entities": [{"text": entity_link, "type": "person"}]}


# ---------------------------------------------------------------------------
# Detection tests
# ---------------------------------------------------------------------------

class TestLegacyFormatDetection:

    def test_all_three_formats_detected(self, tmp_path: Path) -> None:
        """All three legacy-ID formats must be recognised as legacy."""
        conn = _db(tmp_path, "det")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        # Format 1: cid_email_derived -- make_contact_id(email)
        legacy_cid = _insert_legacy_contact(conn, "legacy@example.com")
        fmt1 = detect_legacy_format(legacy_cid, conn)
        assert fmt1 == "cid_email_derived", f"Expected cid_email_derived, got {fmt1!r}"

        # Format 2: ent_email_derived -- entity_id_from_email(email)
        ent_email = entity_id_from_email(ALICE_EMAIL)
        # Insert a row in contact_identifiers so the detection DB check passes
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO contact_identifiers"
            " (identifier_type, identifier_value, contact_id, source, verified_at)"
            " VALUES ('email', ?, ?, 'crm_v1', '2024-01-01T00:00:00+00:00')",
            (ALICE_EMAIL, alice_cid),
        )
        conn.execute("COMMIT")
        fmt2 = detect_legacy_format(ent_email, conn)
        assert fmt2 == "ent_email_derived", f"Expected ent_email_derived, got {fmt2!r}"

        # Format 3: ent_ulid -- generator-style ULID
        ent_ulid = "ent_01ACME000000000000000000"
        fmt3 = detect_legacy_format(ent_ulid, conn)
        assert fmt3 == "ent_ulid", f"Expected ent_ulid, got {fmt3!r}"

        # Canonical IDs must NOT be detected as legacy
        assert detect_legacy_format(alice_cid, conn) is None, (
            "Canonical fixture ID must not be detected as legacy"
        )
        conn.close()

    def test_canonical_uuid_id_not_detected(self, tmp_path: Path) -> None:
        """mint_contact_id() produces 36-char IDs that are not legacy."""
        from olo.adapters.contacts import mint_contact_id
        conn = _db(tmp_path, "det2")
        migrate_migration_schema(conn)
        cid = mint_contact_id()  # cid_ + uuid4().hex = 36 chars
        assert detect_legacy_format(cid, conn) is None


# ---------------------------------------------------------------------------
# Plan step
# ---------------------------------------------------------------------------

class TestPlanStep:

    def test_dry_run_does_not_modify_contacts(self, tmp_path: Path) -> None:
        """Dry-run plan must not change the contacts or contact_identifiers tables."""
        conn = _db(tmp_path, "plan1")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:001", legacy_id)]
        run_id = _run_id()
        manifest = plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=True)

        assert manifest["run_id"] == run_id
        assert any(m["old_entity_link"] == legacy_id for m in manifest["mappings"])
        assert any(m["new_entity_link"] == alice_cid for m in manifest["mappings"])

        # contacts table still has legacy ID (dry-run = no alias)
        assert conn.execute(
            "SELECT id FROM contacts WHERE id=?", (legacy_id,)
        ).fetchone(), "Dry-run must not alias the contacts table"

        status = get_migration_status(run_id, conn)
        assert status["counts"].get("planned", 0) >= 1
        conn.close()

    def test_ambiguous_mapping_raises_before_creating_records(
        self, tmp_path: Path
    ) -> None:
        """Ambiguous discovery must raise before writing any run records."""
        conn = _db(tmp_path, "plan2")

        # Insert legacy contact whose emails map to two different CRM entries
        legacy_id = make_contact_id(ALICE_EMAIL)
        now = "2024-01-01T00:00:00+00:00"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO contacts"
            " (id, organization_id, display_name, source, created_at, updated_at)"
            " VALUES (?, NULL, 'Ambiguous', 'old_crm', ?, ?)",
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

        migrate_migration_schema(conn)
        hdocs = [_hindsight_doc("sig:amb", legacy_id)]
        run_id = _run_id()

        with pytest.raises(LegacyMigrationAmbiguous):
            plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs)

        # No run record created
        row = conn.execute(
            "SELECT run_id FROM identity_migration_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        assert row is None, "No run record should exist after a failed plan"
        conn.close()


# ---------------------------------------------------------------------------
# Alias step
# ---------------------------------------------------------------------------

class TestAliasStep:

    def test_alias_renames_cid_and_marks_aliased(self, tmp_path: Path) -> None:
        """step_alias must rename the email-derived cid_ and mark docs aliased."""
        conn = _db(tmp_path, "alias1")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:a1", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)

        aliased = step_alias(run_id, conn)
        assert aliased == 1

        # New canonical ID exists in contacts
        assert conn.execute(
            "SELECT id FROM contacts WHERE id=?", (alice_cid,)
        ).fetchone()

        # Old ID gone
        assert not conn.execute(
            "SELECT id FROM contacts WHERE id=?", (legacy_id,)
        ).fetchone()

        # Doc marked aliased
        state = conn.execute(
            "SELECT state FROM identity_migration_docs WHERE run_id=? AND document_id=?",
            (run_id, "sig:a1"),
        ).fetchone()[0]
        assert state == "aliased"
        conn.close()

    def test_alias_does_not_touch_ent_documents(self, tmp_path: Path) -> None:
        """ent_ulid documents stay in state=planned after step_alias."""
        conn = _db(tmp_path, "alias2")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        ent_id = "ent_01ACME000000000000000000"
        hdocs = [_hindsight_doc("sig:ent1", ent_id)]
        run_id = _run_id()

        import olo.adapters.contacts_migration as _m
        orig = _m._build_entity_email_map

        def mock_map(_conn):
            return {ent_id: [ALICE_EMAIL]}

        _m._build_entity_email_map = mock_map
        try:
            plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        finally:
            _m._build_entity_email_map = orig

        step_alias(run_id, conn)

        state = conn.execute(
            "SELECT state FROM identity_migration_docs WHERE run_id=? AND document_id=?",
            (run_id, "sig:ent1"),
        ).fetchone()
        assert state is not None, "ent_ulid doc must be in migration_docs"
        assert state[0] == "planned", (
            "ent_ulid doc must stay planned after step_alias (no SQLite rename needed)"
        )
        conn.close()


# ---------------------------------------------------------------------------
# Retain step
# ---------------------------------------------------------------------------

class TestRetainStep:

    def test_retain_2_fails_after_retain_1_succeeds(self, tmp_path: Path) -> None:
        """Scenario 1: second retain fails; first must remain in state=retained."""
        conn = _db(tmp_path, "ret1")
        legacy_a = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        legacy_b = _insert_legacy_contact(conn, BOB_EMAIL, "Bob")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        bob_cid = get_contact_id_for_email(BOB_EMAIL)

        hdocs = [
            _hindsight_doc("sig:r1a", legacy_a),
            _hindsight_doc("sig:r1b", legacy_b),
        ]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        step_alias(run_id, conn)

        call_count = [0]

        def partial_retain(item: dict, op_id: str) -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("Simulated network failure on second retain")

        result = step_retain(run_id, conn, lambda d, l: {"document_id": d, "entities": [{"text": l}]}, partial_retain)

        assert result["retained"] >= 1
        assert result["failed"] >= 1

        # First doc retained, second failed
        states = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT document_id, state FROM identity_migration_docs WHERE run_id=?",
                (run_id,),
            ).fetchall()
        }
        assert "retained" in states.values(), "At least one doc must be retained"
        assert "failed" in states.values(), "Second retain failure must be recorded"

        status = get_migration_status(run_id, conn)
        assert status["incomplete"]
        assert status["recoverable"]
        conn.close()

    def test_operation_id_committed_before_external_call(
        self, tmp_path: Path
    ) -> None:
        """operation_id must be in SQLite before retain_fn is invoked."""
        conn = _db(tmp_path, "ret2")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        hdocs = [_hindsight_doc("sig:opid1", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        step_alias(run_id, conn)

        observed_op_id_before_call = []

        def retain_that_checks_op_id(item: dict, op_id: str) -> None:
            # Verify SQLite already has the operation_id before we return
            row = conn.execute(
                "SELECT retain_operation_id FROM identity_migration_docs"
                " WHERE run_id=? AND document_id=?",
                (run_id, "sig:opid1"),
            ).fetchone()
            observed_op_id_before_call.append(row[0] if row else None)

        step_retain(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            retain_that_checks_op_id,
        )

        assert observed_op_id_before_call, "retain_fn must have been called"
        assert observed_op_id_before_call[0] is not None, (
            "operation_id must be committed to SQLite before retain_fn is called"
        )
        conn.close()

    def test_crash_between_remote_success_and_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """Scenario 3: retain succeeds remotely, but SQLite commit to 'retained' fails.

        On resume, the same operation_id is reused (idempotent).  The document
        must eventually reach 'retained' state.
        """
        conn = _db(tmp_path, "crash1")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        hdocs = [_hindsight_doc("sig:crash1", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        step_alias(run_id, conn)

        retained_calls = []

        def crash_on_commit_retain(item: dict, op_id: str) -> None:
            # Retain succeeds remotely (called once), but then we simulate
            # the process dying before the SQLite 'retained' commit.
            retained_calls.append(op_id)

        # Monkeypatch _commit_doc_state to raise on the first 'retained' transition
        import olo.adapters.contacts_migration as _m
        original_commit = _m._commit_doc_state
        commit_calls = [0]

        def crash_commit(run_id_, doc_id_, state_, error_, conn_):
            if state_ == "retained":
                commit_calls[0] += 1
                if commit_calls[0] == 1:
                    # Simulate crash: state stays un-updated
                    raise RuntimeError("Simulated crash before commit")
            original_commit(run_id_, doc_id_, state_, error_, conn_)

        _m._commit_doc_state = crash_commit
        try:
            step_retain(
                run_id, conn,
                lambda d, l: {"document_id": d, "entities": [{"text": l}]},
                crash_on_commit_retain,
            )
        except Exception:
            pass
        finally:
            _m._commit_doc_state = original_commit

        # Verify: operation_id IS committed (pre-call checkpoint survived)
        row = conn.execute(
            "SELECT retain_operation_id, state FROM identity_migration_docs"
            " WHERE run_id=? AND document_id='sig:crash1'",
            (run_id,),
        ).fetchone()
        assert row is not None
        op_id_saved = row[0]
        assert op_id_saved is not None, "operation_id must be committed"

        # On resume: retain_fn called again with the SAME operation_id
        seen_op_ids = []

        def idempotent_retain(item: dict, op_id: str) -> None:
            seen_op_ids.append(op_id)

        step_retain(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            idempotent_retain,
        )
        assert seen_op_ids, "retain_fn must be called on resume"
        assert seen_op_ids[0] == op_id_saved, (
            "Resume must reuse the saved operation_id for idempotent retry"
        )

        state = conn.execute(
            "SELECT state FROM identity_migration_docs"
            " WHERE run_id=? AND document_id='sig:crash1'",
            (run_id,),
        ).fetchone()[0]
        assert state == "retained", f"After resume, state must be retained; got {state!r}"
        conn.close()

    def test_all_retains_succeed_sqlite_commit_fails(
        self, tmp_path: Path
    ) -> None:
        """Scenario 2: all Hindsight retains succeed but SQLite commit of 'retained' fails.

        On resume, retain_fn is called again with the same operation_id.
        """
        conn = _db(tmp_path, "sqliteFail")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        hdocs = [_hindsight_doc("sig:sqlf1", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        step_alias(run_id, conn)

        import olo.adapters.contacts_migration as _m
        original_commit = _m._commit_doc_state
        commit_blocked = [False]

        def block_retained_commit(run_id_, doc_id_, state_, error_, conn_):
            if state_ == "retained" and not commit_blocked[0]:
                commit_blocked[0] = True
                raise RuntimeError("Simulated SQLite COMMIT failure after Hindsight success")
            original_commit(run_id_, doc_id_, state_, error_, conn_)

        _m._commit_doc_state = block_retained_commit
        try:
            step_retain(
                run_id, conn,
                lambda d, l: {"document_id": d, "entities": [{"text": l}]},
                _ok_retain,
            )
        except Exception:
            pass
        finally:
            _m._commit_doc_state = original_commit

        # State is NOT 'retained' (commit failed)
        state = conn.execute(
            "SELECT state FROM identity_migration_docs"
            " WHERE run_id=? AND document_id='sig:sqlf1'",
            (run_id,),
        ).fetchone()[0]
        assert state != "retained", "SQLite commit failed — state must not be 'retained'"

        # Resume: op_id was committed before the external call
        op_id_saved = conn.execute(
            "SELECT retain_operation_id FROM identity_migration_docs"
            " WHERE run_id=? AND document_id='sig:sqlf1'",
            (run_id,),
        ).fetchone()[0]
        assert op_id_saved is not None

        seen_op_ids = []

        def recording_retain(item: dict, op_id: str) -> None:
            seen_op_ids.append(op_id)

        step_retain(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            recording_retain,
        )
        assert seen_op_ids and seen_op_ids[0] == op_id_saved, (
            "Resume must reuse the same operation_id"
        )
        conn.close()


# ---------------------------------------------------------------------------
# Verify step
# ---------------------------------------------------------------------------

class TestVerifyStep:

    def test_stale_link_causes_verification_failure(self, tmp_path: Path) -> None:
        """Scenario 8: Hindsight document still has the old entity link."""
        conn = _db(tmp_path, "ver1")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:v1", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        step_alias(run_id, conn)
        step_retain(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            _ok_retain,
        )

        # get_document returns the OLD entity link (stale)
        def stale_get_doc(doc_id: str) -> dict:
            return _ok_get_doc(doc_id, legacy_id)  # still the old link!

        result = step_verify(run_id, conn, stale_get_doc)

        assert result["verified"] == 0
        assert result["failed"] >= 1
        assert result["stale_links"], "Stale link must be reported"
        assert result["stale_links"][0]["old_link"] == legacy_id

        status = get_migration_status(run_id, conn)
        assert status["incomplete"]
        conn.close()

    def test_correct_link_verified(self, tmp_path: Path) -> None:
        """Step verify succeeds when Hindsight document has the new entity link."""
        conn = _db(tmp_path, "ver2")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:v2", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        step_alias(run_id, conn)
        step_retain(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            _ok_retain,
        )

        result = step_verify(run_id, conn, lambda d: _ok_get_doc(d, alice_cid))

        assert result["verified"] == 1
        assert result["failed"] == 0
        assert not result["stale_links"]

        status = get_migration_status(run_id, conn)
        assert status["state"] == "completed"
        assert not status["incomplete"]
        conn.close()


# ---------------------------------------------------------------------------
# Resume and idempotency
# ---------------------------------------------------------------------------

class TestResumeMigration:

    def test_resume_after_partial_success(self, tmp_path: Path) -> None:
        """Scenario 4: resume picks up retained docs and verifies them."""
        conn = _db(tmp_path, "res1")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:res1", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)
        # Manually alias to simulate partial progress
        step_alias(run_id, conn)
        # Manually set state=retained but do NOT verify
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE identity_migration_docs SET state='retained', retain_operation_id='fake-op'"
            " WHERE run_id=? AND document_id='sig:res1'",
            (run_id,),
        )
        conn.execute("COMMIT")

        result = resume_migration(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            _ok_retain,
            lambda d: _ok_get_doc(d, alice_cid),
        )

        assert result["verified"] >= 1
        assert result["run_state"] == "completed"
        conn.close()

    def test_repeated_migration_is_noop(self, tmp_path: Path) -> None:
        """Scenario 5: running resume on a completed run does not duplicate work."""
        conn = _db(tmp_path, "res2")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:res2", legacy_id)]
        run_id = _run_id()
        plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=False)

        retain_calls = [0]

        def counting_retain(item: dict, op_id: str) -> None:
            retain_calls[0] += 1

        # First full run
        result1 = resume_migration(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            counting_retain,
            lambda d: _ok_get_doc(d, alice_cid),
        )
        assert result1["run_state"] == "completed"
        first_calls = retain_calls[0]

        # Second run (resume on completed) -- must be a no-op
        result2 = resume_migration(
            run_id, conn,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            counting_retain,
            lambda d: _ok_get_doc(d, alice_cid),
        )
        assert retain_calls[0] == first_calls, (
            "retain_fn must NOT be called again on a completed run"
        )
        conn.close()

    def test_tenant_isolation(self, tmp_path: Path) -> None:
        """Scenario 9: migration in tenant A must not affect tenant B's data."""
        conn_a = _db(tmp_path, "tenant_a")
        conn_b = _db(tmp_path, "tenant_b")

        legacy_a = _insert_legacy_contact(conn_a, ALICE_EMAIL, "Alice A")
        legacy_b = _insert_legacy_contact(conn_b, ALICE_EMAIL, "Alice B")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs_a = [_hindsight_doc("sig:ta1", legacy_a)]
        run_id_a = _run_id()
        plan_identity_migration(run_id_a, conn_a, CRM_CONTACTS, hdocs_a, is_dry_run=False)

        resume_migration(
            run_id_a, conn_a,
            lambda d, l: {"document_id": d, "entities": [{"text": l}]},
            _ok_retain,
            lambda d: _ok_get_doc(d, alice_cid),
        )

        # Tenant B must be completely untouched
        assert conn_b.execute(
            "SELECT id FROM contacts WHERE id=?", (legacy_b,)
        ).fetchone(), "Tenant B's legacy contact must still exist"

        assert not conn_b.execute(
            "SELECT id FROM contacts WHERE id=?", (alice_cid,)
        ).fetchone(), "Tenant B must NOT have alice_cid (migration was for A only)"

        conn_a.close()
        conn_b.close()

    def test_evidence_hashes_preserved(self, tmp_path: Path) -> None:
        """Evidence hashes in migration docs must be stable and non-empty."""
        conn = _db(tmp_path, "evid")
        legacy_id = _insert_legacy_contact(conn, ALICE_EMAIL, "Alice")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        hdocs = [_hindsight_doc("sig:ev1", legacy_id)]
        run_id = _run_id()
        manifest = plan_identity_migration(run_id, conn, CRM_CONTACTS, hdocs, is_dry_run=True)

        hashes = [m["evidence_hash"] for m in manifest["mappings"]]
        assert all(h and len(h) == 64 for h in hashes), (
            "Each mapping must have a non-empty 64-hex evidence hash"
        )

        # Rebuild with same input -- must produce same hash
        manifest2 = plan_identity_migration(
            _run_id(), conn, CRM_CONTACTS, hdocs, is_dry_run=True
        )
        hashes2 = [m["evidence_hash"] for m in manifest2["mappings"]]
        assert hashes == hashes2, "Evidence hashes must be deterministic"
        conn.close()


# ---------------------------------------------------------------------------
# Benchmark fingerprint regression
# ---------------------------------------------------------------------------

class TestFingerprintUnchanged:

    def test_benchmark_fingerprint_unchanged(self, tmp_path: Path) -> None:
        """Migration module must not affect the frozen benchmark fingerprint."""
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
            f"Frozen fingerprint mismatch.\n"
            f"  Expected: {FROZEN_FULL_ITEM_FP}\n"
            f"  Got:      {result.actual}"
        )
