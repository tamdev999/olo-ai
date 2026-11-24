"""Adversarial identity-safety tests — Opus final audit corrections.

Correction 1 — DenyAll authorization boundary:
  - create_operator_token raises for every input (admin, crm_admin, any string, empty)
  - Directly constructed OperatorToken(actor=...) cannot authorize a transfer
  - Mutating AUTHORIZED_OPERATORS externally has no effect
  - resolve_identifier_conflict raises before any DB write for all callers

Correction 2 — Identifier lifecycle:
  - Full CRM snapshot revokes identifiers of contacts omitted entirely
  - Partial (delta) update with same omission does NOT revoke
  - Alphabetic phone inputs (vanity numbers, text) are rejected
  - phone_region from signal payload is not trusted;
    only the caller-supplied default_region is used for national-number lookup
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from olo.adapters.contacts import (
    AUTHORIZED_OPERATORS,
    IdentityResolutionError,
    OperatorToken,
    PhoneNormalizationError,
    create_operator_token,
    get_identifier_conflicts,
    get_identifier_ownership_history,
    load_contact_index,
    make_fixture_contact_id,
    migrate_contacts,
    normalize_phone,
    populate_contacts_from_crm,
    resolve_contact_from_signal,
    resolve_identifier_conflict,
)
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email

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
# Correction 1 — DenyAll authorization boundary
# ---------------------------------------------------------------------------

class TestDenyAllBoundary:
    """Every path to identifier transfer must be blocked."""

    def test_create_operator_token_raises_for_all_inputs(self) -> None:
        """create_operator_token must raise for every input — including former allowlist names."""
        for name in ("admin", "crm_admin", "", "anyone", "root",
                     "crm_fixture", "crm_v1", "operator", "ADMIN", "Admin"):
            with pytest.raises(IdentityResolutionError, match="disabled"):
                create_operator_token(name)

    def test_forged_token_direct_construction_denied(self, tmp_path: Path) -> None:
        """OperatorToken(actor=...) constructed without create_operator_token is denied."""
        conn = _db(tmp_path, "forged")
        cid_a = make_fixture_contact_id("test:forged_a")
        cid_b = make_fixture_contact_id("test:forged_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a@forged.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", "a@forged.com"), ("phone", "+18001110001")]},
            {"contact_id": cid_b, "canonical_email": "b@forged.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", "b@forged.com"), ("phone", "+18001110001")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fix")
        conflicts_before = len(get_identifier_conflicts(conn, unresolved_only=True))

        # Every actor name that could plausibly be used
        for actor in ("admin", "crm_admin", "super", "system"):
            forged = OperatorToken(actor=actor)
            with pytest.raises(IdentityResolutionError, match="disabled"):
                resolve_identifier_conflict(
                    "phone", "+18001110001", cid_b, forged, conn
                )

        # No DB writes — conflict count unchanged
        assert len(get_identifier_conflicts(conn, unresolved_only=True)) == conflicts_before
        conn.close()

    def test_allowlist_mutation_has_no_effect(self, tmp_path: Path) -> None:
        """Externally mutating AUTHORIZED_OPERATORS does not enable transfers."""
        conn = _db(tmp_path, "mutate")
        cid_a = make_fixture_contact_id("test:mut_a")
        cid_b = make_fixture_contact_id("test:mut_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a@mut.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", "a@mut.com"), ("phone", "+18001110002")]},
            {"contact_id": cid_b, "canonical_email": "b@mut.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", "b@mut.com"), ("phone", "+18001110002")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fix")

        # Attempt to mutate the module-level frozenset (this produces a NEW local
        # variable but cannot modify the original frozenset in the module).
        import olo.adapters.contacts as _contacts_mod
        original = _contacts_mod.AUTHORIZED_OPERATORS
        try:
            # frozenset |= produces a new object; patch it onto the module
            _contacts_mod.AUTHORIZED_OPERATORS = original | {"attacker"}
            # Token creation still raises (enforcement is in the function body,
            # not from the module-level set reference).
            with pytest.raises(IdentityResolutionError, match="disabled"):
                create_operator_token("attacker")
            # Direct token + resolve also raises
            forged = OperatorToken(actor="attacker")
            with pytest.raises(IdentityResolutionError, match="disabled"):
                resolve_identifier_conflict(
                    "phone", "+18001110002", cid_b, forged, conn
                )
        finally:
            _contacts_mod.AUTHORIZED_OPERATORS = original  # restore

        conn.close()

    def test_resolve_identifier_conflict_raises_before_any_db_write(
        self, tmp_path: Path
    ) -> None:
        """resolve_identifier_conflict must raise before touching the DB."""
        conn = _db(tmp_path, "pre_write")
        cid_a = make_fixture_contact_id("test:pw_a")
        cid_b = make_fixture_contact_id("test:pw_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a@pw.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", "a@pw.com"), ("phone", "+18001110003")]},
            {"contact_id": cid_b, "canonical_email": "b@pw.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", "b@pw.com"), ("phone", "+18001110003")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fix")

        history_before = len(get_identifier_ownership_history(conn))
        idx_before, _ = load_contact_index(conn)

        forged = OperatorToken(actor="admin")
        with pytest.raises(IdentityResolutionError, match="disabled"):
            resolve_identifier_conflict(
                "phone", "+18001110003", cid_b, forged, conn
            )

        # Ownership history unchanged — no event recorded
        assert len(get_identifier_ownership_history(conn)) == history_before
        # Identifier map unchanged — phone still excluded (disputed)
        idx_after, _ = load_contact_index(conn)
        assert idx_after.get(("phone", "+18001110003")) is None
        conn.close()

    def test_authorized_operators_is_empty(self) -> None:
        """AUTHORIZED_OPERATORS must be an empty frozenset (DenyAll signal)."""
        assert AUTHORIZED_OPERATORS == frozenset(), (
            f"Expected empty frozenset, got {AUTHORIZED_OPERATORS!r}"
        )
        assert "admin" not in AUTHORIZED_OPERATORS
        assert "crm_admin" not in AUTHORIZED_OPERATORS


# ---------------------------------------------------------------------------
# Correction 2 — Identifier lifecycle
# ---------------------------------------------------------------------------

class TestSnapshotOmittedContactRevocation:
    """Full CRM snapshots must revoke identifiers of contacts omitted entirely."""

    def test_full_snapshot_revokes_omitted_contact(self, tmp_path: Path) -> None:
        """When a contact is absent from a full snapshot, all its identifiers are revoked."""
        conn = _db(tmp_path, "snap_omit1")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        bob_cid = get_contact_id_for_email("bob.torres@beta-tech.example")

        # Initial full CRM load — Alice and Bob both present
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)
        assert idx.get(("email", ALICE_EMAIL)) == alice_cid

        # CRM snapshot v2 omits Alice entirely
        crm_no_alice = [e for e in CRM_CONTACTS if e["contact_id"] != alice_cid]
        populate_contacts_from_crm(crm_no_alice, conn, source="crm_v2", snapshot_mode=True)

        idx2, _ = load_contact_index(conn)

        # Alice's identifiers must be revoked
        assert idx2.get(("email", ALICE_EMAIL)) is None, (
            "Alice's primary email must be revoked when she's omitted from the snapshot"
        )
        assert idx2.get(("phone", ALICE_PHONE)) is None, (
            "Alice's phone must be revoked when she's omitted from the snapshot"
        )

        # Bob's identifiers must be untouched
        assert idx2.get(("email", "bob.torres@beta-tech.example")) == bob_cid, (
            "Bob must be unaffected — he's still in the snapshot"
        )

        # Revocation recorded in history
        history = get_identifier_ownership_history(
            conn, identifier_type="email", identifier_value=ALICE_EMAIL
        )
        assert any(h["event_type"] == "revoked" for h in history), (
            "Revocation event must be recorded for Alice's email"
        )
        conn.close()

    def test_partial_update_does_not_revoke_omitted_contact(
        self, tmp_path: Path
    ) -> None:
        """Delta update (snapshot_mode=False) must NOT revoke contacts absent from the call."""
        conn = _db(tmp_path, "snap_omit2")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        # Delta update omitting Alice — but NOT a full snapshot
        crm_no_alice = [e for e in CRM_CONTACTS if e["contact_id"] != alice_cid]
        populate_contacts_from_crm(
            crm_no_alice, conn, source="crm_delta", snapshot_mode=False
        )

        idx, _ = load_contact_index(conn)
        assert idx.get(("email", ALICE_EMAIL)) == alice_cid, (
            "Alice's email must NOT be revoked on a partial update"
        )
        assert idx.get(("phone", ALICE_PHONE)) == alice_cid, (
            "Alice's phone must NOT be revoked on a partial update"
        )
        conn.close()

    def test_snapshot_replay_idempotent_for_omitted_contact(
        self, tmp_path: Path
    ) -> None:
        """Repeating the same full snapshot does not duplicate revocation events."""
        conn = _db(tmp_path, "snap_omit3")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        crm_no_alice = [e for e in CRM_CONTACTS if e["contact_id"] != alice_cid]

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        populate_contacts_from_crm(crm_no_alice, conn, source="crm_v2", snapshot_mode=True)
        n1 = len(get_identifier_ownership_history(
            conn, identifier_type="email", identifier_value=ALICE_EMAIL
        ))

        # Replay
        populate_contacts_from_crm(crm_no_alice, conn, source="crm_v2", snapshot_mode=True)
        n2 = len(get_identifier_ownership_history(
            conn, identifier_type="email", identifier_value=ALICE_EMAIL
        ))

        assert n1 == n2, f"Snapshot replay must not duplicate revocation events: {n1} → {n2}"
        conn.close()


class TestPhoneValidation:
    """Phone numbers with alphabetic characters must be rejected."""

    def test_alphabetic_e164_rejected(self) -> None:
        """E.164 numbers with text characters must raise PhoneNormalizationError."""
        for bad in ("+1800FLOWERS", "+1800CALL123", "+44TEXT12345", "+1abc2345678"):
            with pytest.raises(PhoneNormalizationError, match="alphabetic"):
                normalize_phone(bad)

    def test_vanity_national_rejected(self) -> None:
        """National vanity numbers must be rejected regardless of region."""
        for bad, region in (
            ("800FLOWERS", "US"),
            ("1800CALL", "US"),
            ("1800TAXICAB", "CA"),
        ):
            with pytest.raises(PhoneNormalizationError, match="alphabetic"):
                normalize_phone(bad, region)

    def test_valid_phones_still_work(self) -> None:
        """Valid numeric phones must continue to normalize correctly."""
        assert normalize_phone("+14155550101") == "+14155550101"
        assert normalize_phone("+1 415-555-0101") == "+14155550101"
        assert normalize_phone("(415) 555-0101", "US") == "+14155550101"
        assert normalize_phone("4155550101", "US") == "+14155550101"

    def test_phone_region_in_payload_is_not_trusted(self, tmp_path: Path) -> None:
        """phone_region from signal payload must NOT be used for national-number lookup."""
        conn = _db(tmp_path, "ph_region")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)

        # Payload claims phone_region="US" but this must be ignored
        payload = {"phone": "4155550101", "phone_region": "US"}

        # Without a business-configured default_region, national number must NOT resolve
        cid_no_region = resolve_contact_from_signal("call.transcript", payload, idx)
        assert cid_no_region == "", (
            "National number from payload must not resolve when no default_region is set "
            "(phone_region in payload must not be trusted)"
        )

        # With a business-configured default_region, it resolves
        cid_with_region = resolve_contact_from_signal(
            "call.transcript", payload, idx, default_region="US"
        )
        assert cid_with_region == alice_cid, (
            "National number must resolve when business-configured default_region is set"
        )

    def test_e164_resolves_without_any_region(self, tmp_path: Path) -> None:
        """E.164 numbers always resolve without needing a region."""
        conn = _db(tmp_path, "ph_e164")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)

        cid = resolve_contact_from_signal(
            "call.transcript", {"phone": "+14155550101"}, idx
        )
        assert cid == alice_cid
        conn.close()

    def test_alphabetic_phone_in_signal_does_not_resolve(self, tmp_path: Path) -> None:
        """A signal with a text-bearing phone must not match any contact."""
        conn = _db(tmp_path, "ph_alpha_signal")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)

        cid = resolve_contact_from_signal(
            "call.transcript", {"phone": "+1800FLOWERS"}, idx, default_region="US"
        )
        assert cid == "", "Text-bearing phone must not resolve to any contact"
        conn.close()
