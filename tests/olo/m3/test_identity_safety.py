"""P1/P2/P3 identity-safety tests: fail-closed disputes, event recording, restricted resolution.

Covers the three fixes from identity-safety-fix-1-of-3:

P1 — Disputed identifiers must fail closed
  - incoming call unassigned while dispute active
  - incoming call assigned only to winner after authorized resolution
  - undisputed identifiers unaffected throughout

P2 — Record every new conflict event
  - fresh claim after resolution creates a new unresolved row
  - replaying the same sync is idempotent

P3 — Restrict conflict resolution
  - unauthorized operator rejected
  - non-claimant rejected
  - authorized claimant succeeds
  - repeated resolution is a no-op
  - cross-business isolation
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from olo.adapters.contacts import (
    make_fixture_contact_id,
    migrate_contacts,
    populate_contacts_from_crm,
    load_contact_index,
    resolve_contact_from_signal,
    resolve_identifier_conflict,
    get_identifier_conflicts,
    AUTHORIZED_OPERATORS,
    IdentityResolutionError,
    OperatorToken,
    create_operator_token,
)
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email
from olo.m3.synthetic.generator import ENTITIES

ALICE_EMAIL = "alice.chen@acme-corp.example"
ALICE_PHONE = "+14155550101"


def _db(tmp_path: Path, subdir: str) -> sqlite3.Connection:
    from olo.store import connect, migrate
    db_path = tmp_path / subdir / "olo.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    migrate_contacts(conn)
    return conn


# ---------------------------------------------------------------------------
# P1 — Fail-closed dispute tests
# ---------------------------------------------------------------------------

class TestP1FailClosed:
    """P1: Disputed identifiers must resolve to neither claimant."""

    def test_alice_bob_conflict_incoming_call_unassigned(self, tmp_path: Path) -> None:
        """P1 spec: Alice → Bob conflict → incoming call unassigned."""
        conn = _db(tmp_path, "p1_spec")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        bob_cid   = get_contact_id_for_email(ENTITIES[1].email)

        # Alice already owns the phone (via full CRM fixture)
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        # Bob claims Alice's phone
        hijack = [{
            "contact_id": bob_cid,
            "canonical_email": "bob@override.example",
            "display_name": "Bob Override",
            "organization_name": "Override Org",
            "identifiers": [("phone", ALICE_PHONE)],
        }]
        populate_contacts_from_crm(hijack, conn, source="crm_v2")

        # Conflict recorded; phone excluded from both parties
        idx, _ = load_contact_index(conn)
        assert idx.get(("phone", ALICE_PHONE)) is None, (
            "Phone must be excluded (unassigned) while dispute is active."
        )

        # Incoming call → unassigned
        call_cid = resolve_contact_from_signal(
            "call.transcript", {"phone": ALICE_PHONE}, idx
        )
        assert call_cid == "", (
            f"Incoming call must be unassigned during dispute. Got {call_cid!r}"
        )
        conn.close()

    def test_resolution_to_bob_assigns_call_to_bob_only(self, tmp_path: Path) -> None:
        """P1 spec: authorized resolution to Bob → incoming call assigned only to Bob."""
        conn = _db(tmp_path, "p1_res")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        bob_cid   = get_contact_id_for_email(ENTITIES[1].email)

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        hijack = [{
            "contact_id": bob_cid,
            "canonical_email": "bob.p1@example.com",
            "display_name": "Bob", "organization_name": "O",
            "identifiers": [("phone", ALICE_PHONE)],
        }]
        populate_contacts_from_crm(hijack, conn, source="crm_v2")

        # Confirm unassigned first
        idx, _ = load_contact_index(conn)
        assert idx.get(("phone", ALICE_PHONE)) is None

        # Admin resolves to Bob
        resolve_identifier_conflict("phone", ALICE_PHONE, bob_cid, create_operator_token("admin"), conn)

        # After resolution: phone assigned to Bob only
        idx2, _ = load_contact_index(conn)
        assert idx2.get(("phone", ALICE_PHONE)) == bob_cid, (
            f"After resolution to Bob, phone must resolve to Bob. "
            f"Got {idx2.get(('phone', ALICE_PHONE))!r}"
        )
        assert idx2.get(("phone", ALICE_PHONE)) != alice_cid, (
            "Phone must NOT resolve to Alice after resolution to Bob"
        )
        conn.close()

    def test_undisputed_identifiers_unaffected(self, tmp_path: Path) -> None:
        """P1: Emails of both parties remain usable during a phone dispute."""
        conn = _db(tmp_path, "p1_undisputed")
        cid_x = make_fixture_contact_id("test:p1_x")
        cid_y = make_fixture_contact_id("test:p1_y")
        fix = [
            {"contact_id": cid_x, "canonical_email": "px@ex.com",
             "display_name": "X", "organization_name": "O",
             "identifiers": [("email", "px@ex.com"), ("phone", "+17001112222")]},
            {"contact_id": cid_y, "canonical_email": "py@ex.com",
             "display_name": "Y", "organization_name": "O",
             "identifiers": [("email", "py@ex.com"), ("phone", "+17001112222")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fixture")
        idx, _ = load_contact_index(conn)

        assert idx.get(("phone", "+17001112222")) is None, (
            "Disputed phone must be excluded"
        )
        assert idx.get(("email", "px@ex.com")) == cid_x, (
            "Undisputed email must still resolve"
        )
        assert idx.get(("email", "py@ex.com")) == cid_y, (
            "Undisputed email must still resolve"
        )
        conn.close()


# ---------------------------------------------------------------------------
# P2 — Record every new conflict event
# ---------------------------------------------------------------------------

class TestP2ConflictRecording:
    """P2: New disputes are always recorded; replay stays idempotent."""

    def test_new_dispute_recorded_after_resolution(self, tmp_path: Path) -> None:
        """P2: After a conflict is resolved, a fresh claim creates a new event."""
        conn = _db(tmp_path, "p2_new")
        cid_a = make_fixture_contact_id("test:p2_a")
        cid_b = make_fixture_contact_id("test:p2_b")
        fix_a = [{"contact_id": cid_a, "canonical_email": "a@p2.com",
                   "display_name": "A", "organization_name": "O",
                   "identifiers": [("email", "a@p2.com"), ("phone", "+19991112222")]}]
        fix_b = [{"contact_id": cid_b, "canonical_email": "b@p2.com",
                   "display_name": "B", "organization_name": "O",
                   "identifiers": [("email", "b@p2.com"), ("phone", "+19991112222")]}]

        # Round 1: A owns phone; B tries to claim → 1 conflict
        populate_contacts_from_crm(fix_a, conn, source="crm_v1")
        populate_contacts_from_crm(fix_b, conn, source="crm_v2")
        n_r1 = len(get_identifier_conflicts(conn, unresolved_only=True))
        assert n_r1 >= 1, "First dispute must be recorded"

        # Resolve to B
        resolve_identifier_conflict("phone", "+19991112222", cid_b, create_operator_token("admin"), conn)
        assert len(get_identifier_conflicts(conn, unresolved_only=True)) == 0, (
            "No unresolved conflicts after resolution"
        )

        # Round 2: A re-claims the phone → NEW unresolved conflict
        populate_contacts_from_crm(fix_a, conn, source="crm_v3")
        n_r2 = len(get_identifier_conflicts(conn, unresolved_only=True))
        assert n_r2 >= 1, (
            f"P2: A fresh claim after resolution must create a new conflict event. "
            f"Got {n_r2} unresolved."
        )

        # Total conflict rows: at least 2
        all_conflicts = get_identifier_conflicts(conn, unresolved_only=False)
        assert len(all_conflicts) >= 2, (
            f"P2: At least 2 conflict events expected (one per round). "
            f"Got {len(all_conflicts)}"
        )
        conn.close()

    def test_replay_idempotent_no_duplicates(self, tmp_path: Path) -> None:
        """P2: Replaying the same CRM sync does not create duplicate events."""
        conn = _db(tmp_path, "p2_idem")
        cid_a = make_fixture_contact_id("test:p2_idem_a")
        cid_b = make_fixture_contact_id("test:p2_idem_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a@repl.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", "a@repl.com"), ("phone", "+18887776666")]},
            {"contact_id": cid_b, "canonical_email": "b@repl.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", "b@repl.com"), ("phone", "+18887776666")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fixture")
        n1 = len(get_identifier_conflicts(conn))
        # Replay
        populate_contacts_from_crm(fix, conn, source="crm_fixture")
        n2 = len(get_identifier_conflicts(conn))
        assert n1 == n2, (
            f"P2: Replay must not create duplicates. Got {n1} → {n2}"
        )
        conn.close()


# ---------------------------------------------------------------------------
# P3 — Restricted resolution
# ---------------------------------------------------------------------------

class TestP3RestrictedResolution:
    """P3: resolution requires authorized operator and legitimate claimant."""

    def _setup_conflict(self, tmp_path, subdir):
        conn = _db(tmp_path, subdir)
        cid_a = make_fixture_contact_id(f"test:{subdir}_a")
        cid_b = make_fixture_contact_id(f"test:{subdir}_b")
        cid_c = make_fixture_contact_id(f"test:{subdir}_c")  # non-claimant
        fix = [
            {"contact_id": cid_a, "canonical_email": f"a{subdir}@ex.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", f"a{subdir}@ex.com"), ("phone", "+18001234567")]},
            {"contact_id": cid_b, "canonical_email": f"b{subdir}@ex.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", f"b{subdir}@ex.com"), ("phone", "+18001234567")]},
            {"contact_id": cid_c, "canonical_email": f"c{subdir}@ex.com",
             "display_name": "C", "organization_name": "O",
             "identifiers": [("email", f"c{subdir}@ex.com")]},  # no phone claim
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fixture")
        return conn, cid_a, cid_b, cid_c

    def test_unauthorized_actor_raises(self, tmp_path: Path) -> None:
        """P3: an arbitrary resolved_by string is rejected with IdentityResolutionError."""
        conn, cid_a, cid_b, _ = self._setup_conflict(tmp_path, "p3_unauth")
        with pytest.raises(IdentityResolutionError, match="not an authorized operator"):
            resolve_identifier_conflict("phone", "+18001234567", cid_b, create_operator_token("anyone"), conn)
        # Conflict remains unresolved
        assert len(get_identifier_conflicts(conn, unresolved_only=True)) >= 1
        conn.close()

    def test_non_claimant_raises(self, tmp_path: Path) -> None:
        """P3: resolving to a non-claimant contact is rejected."""
        conn, cid_a, cid_b, cid_c = self._setup_conflict(tmp_path, "p3_nonclaim")
        with pytest.raises(IdentityResolutionError, match="not a claimant"):
            resolve_identifier_conflict("phone", "+18001234567", cid_c, create_operator_token("admin"), conn)
        # Conflict remains unresolved
        assert len(get_identifier_conflicts(conn, unresolved_only=True)) >= 1
        conn.close()

    def test_authorized_claimant_succeeds(self, tmp_path: Path) -> None:
        """P3: authorized resolution to a legitimate claimant succeeds."""
        conn, cid_a, cid_b, _ = self._setup_conflict(tmp_path, "p3_ok")
        resolved = resolve_identifier_conflict("phone", "+18001234567", cid_b, create_operator_token("admin"), conn)
        assert resolved >= 1
        idx, _ = load_contact_index(conn)
        assert idx.get(("phone", "+18001234567")) == cid_b
        conn.close()

    def test_repeated_resolution_is_noop(self, tmp_path: Path) -> None:
        """P3: calling resolve twice with same target is a no-op."""
        conn, cid_a, cid_b, _ = self._setup_conflict(tmp_path, "p3_idem")
        resolve_identifier_conflict("phone", "+18001234567", cid_b, create_operator_token("admin"), conn)
        second = resolve_identifier_conflict("phone", "+18001234567", cid_b, create_operator_token("admin"), conn)
        assert second == 0, "Second call with same resolution must return 0"
        conn.close()

    def test_cross_business_isolation(self, tmp_path: Path) -> None:
        """P3: resolution on biz_a's db cannot affect biz_b's db."""
        conn_a, cid_a_a, cid_b_a, _ = self._setup_conflict(tmp_path, "p3_biz_a")
        conn_b, cid_a_b, cid_b_b, _ = self._setup_conflict(tmp_path, "p3_biz_b")

        # Resolve in biz_a
        resolve_identifier_conflict("phone", "+18001234567", cid_b_a, create_operator_token("admin"), conn_a)

        # biz_b's conflict must remain unresolved
        biz_b_conflicts = get_identifier_conflicts(conn_b, unresolved_only=True)
        assert len(biz_b_conflicts) >= 1, "biz_b's conflict must not be affected"
        idx_b, _ = load_contact_index(conn_b)
        assert idx_b.get(("phone", "+18001234567")) is None, (
            "biz_b's phone must remain excluded after biz_a's resolution"
        )
        conn_a.close()
        conn_b.close()

    def test_authorized_operators_not_satisfiable_by_source_names(self) -> None:
        """P3: AUTHORIZED_OPERATORS are distinct from CRM source names."""
        assert "admin" in AUTHORIZED_OPERATORS
        assert "crm_admin" in AUTHORIZED_OPERATORS
        assert "" not in AUTHORIZED_OPERATORS
        assert "crm_fixture" not in AUTHORIZED_OPERATORS
        assert "crm_v1" not in AUTHORIZED_OPERATORS
        # Cannot be satisfied by arbitrary caller-supplied text
        assert "anyone" not in AUTHORIZED_OPERATORS
