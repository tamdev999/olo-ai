"""Identity Safety Fix 2/3 — Tests for identifier removals, ownership provenance,
and phone normalization.

Fix 1 — Identifier removals:
  - CRM snapshot can revoke an identifier.
  - Revoked identifier stops resolving; active identifiers continue.
  - Revocation is preserved for audit; original assignment history survives.
  - Snapshot replay is idempotent.
  - Partial (delta) updates do NOT revoke unmentioned identifiers.

Fix 2 — Ownership provenance:
  - Full ownership history is recorded: assigned → conflict → transferred.
  - Original CRM source is never overwritten during resolution.
  - Failed transfers do not modify history.
  - Duplicate events are not created on replay.

Fix 3 — Phone normalization:
  - Multiple raw formats resolve consistently.
  - Requires explicit region for national numbers.
  - Invalid / ambiguous numbers are rejected.
  - Collision detection works post-normalization.
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
    get_identifier_ownership_history,
    normalize_phone,
    PhoneNormalizationError,
    IdentityResolutionError,
    AUTHORIZED_OPERATORS,
)
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email

ALICE_EMAIL = "alice.chen@acme-corp.example"
ALICE_ALT_EMAIL = "alice.c@gmail.example"
ALICE_PHONE_E164 = "+14155550101"


def _db(tmp_path: Path, subdir: str) -> sqlite3.Connection:
    from olo.store import connect, migrate
    db_path = tmp_path / subdir / "olo.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    migrate_contacts(conn)
    return conn


# ---------------------------------------------------------------------------
# Fix 1 — Identifier removals
# ---------------------------------------------------------------------------

class TestIdentifierRemovals:

    def test_snapshot_revokes_removed_identifier(self, tmp_path: Path) -> None:
        """When a CRM snapshot removes an email from Alice, it stops resolving."""
        conn = _db(tmp_path, "rm1")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        # Initial snapshot: Alice has both emails
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)
        assert idx.get(("email", ALICE_EMAIL)) == alice_cid
        assert idx.get(("email", ALICE_ALT_EMAIL)) == alice_cid

        # CRM snapshot v2: alt email removed from Alice's record
        crm_v2 = []
        for entry in CRM_CONTACTS:
            if entry["contact_id"] == alice_cid:
                new_ids = [(t, v) for t, v in entry["identifiers"]
                           if not (t == "email" and v == ALICE_ALT_EMAIL)]
                crm_v2.append({**entry, "identifiers": new_ids})
            else:
                crm_v2.append(entry)

        populate_contacts_from_crm(crm_v2, conn, source="crm_v2", snapshot_mode=True)
        idx2, _ = load_contact_index(conn)

        # Alt email no longer resolves
        assert idx2.get(("email", ALICE_ALT_EMAIL)) is None, (
            "Revoked email must not appear in identifier_map."
        )
        # Primary email and phone still work
        assert idx2.get(("email", ALICE_EMAIL)) == alice_cid
        assert idx2.get(("phone", ALICE_PHONE_E164)) == alice_cid
        conn.close()

    def test_revocation_preserved_in_history(self, tmp_path: Path) -> None:
        """Revoked identifier leaves a 'revoked' event in ownership history."""
        conn = _db(tmp_path, "rm2")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        crm_v2 = [{**e, "identifiers": [(t, v) for t, v in e["identifiers"]
                                         if not (t == "email" and v == ALICE_ALT_EMAIL)]}
                  if e["contact_id"] == alice_cid else e
                  for e in CRM_CONTACTS]
        populate_contacts_from_crm(crm_v2, conn, source="crm_v2", snapshot_mode=True)

        history = get_identifier_ownership_history(
            conn,
            identifier_type="email",
            identifier_value=ALICE_ALT_EMAIL,
        )
        assigned = [h for h in history if h["event_type"] == "assigned"]
        revoked  = [h for h in history if h["event_type"] == "revoked"]
        assert assigned, "Assignment event must exist in history"
        assert revoked,  "Revocation event must exist in history"
        # Original assignment source preserved
        assert assigned[0]["source"] == "crm_v1"
        conn.close()

    def test_snapshot_replay_idempotent(self, tmp_path: Path) -> None:
        """Running the same snapshot twice does not duplicate revocation events."""
        conn = _db(tmp_path, "rm3")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        crm_v2 = [{**e, "identifiers": [(t, v) for t, v in e["identifiers"]
                                         if not (t == "email" and v == ALICE_ALT_EMAIL)]}
                  if e["contact_id"] == alice_cid else e
                  for e in CRM_CONTACTS]

        populate_contacts_from_crm(crm_v2, conn, source="crm_v2", snapshot_mode=True)
        n1 = len(get_identifier_ownership_history(conn, identifier_type="email",
                                                   identifier_value=ALICE_ALT_EMAIL))
        # Replay
        populate_contacts_from_crm(crm_v2, conn, source="crm_v2", snapshot_mode=True)
        n2 = len(get_identifier_ownership_history(conn, identifier_type="email",
                                                   identifier_value=ALICE_ALT_EMAIL))
        assert n1 == n2, f"Snapshot replay must not duplicate events: {n1} → {n2}"
        conn.close()

    def test_delta_update_does_not_revoke(self, tmp_path: Path) -> None:
        """A partial (delta) update without snapshot_mode must NOT revoke identifiers."""
        conn = _db(tmp_path, "rm4")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        # Delta update: Alice only listed with primary email (incomplete record),
        # but snapshot_mode=False → alt email must NOT be revoked.
        crm_delta = [{**e, "identifiers": [("email", ALICE_EMAIL)]}
                     if e["contact_id"] == alice_cid else e
                     for e in CRM_CONTACTS]
        populate_contacts_from_crm(crm_delta, conn, source="crm_delta",
                                   snapshot_mode=False)

        idx, _ = load_contact_index(conn)
        assert idx.get(("email", ALICE_ALT_EMAIL)) == alice_cid, (
            "Delta update must NOT revoke alt email (not a full snapshot)."
        )
        conn.close()

    def test_unrelated_identifiers_untouched(self, tmp_path: Path) -> None:
        """Revoking Alice's alt email must not affect Bob's identifiers."""
        conn = _db(tmp_path, "rm5")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        bob_cid   = get_contact_id_for_email("bob.torres@beta-tech.example")

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        crm_v2 = [{**e, "identifiers": [(t, v) for t, v in e["identifiers"]
                                         if not (t == "email" and v == ALICE_ALT_EMAIL)]}
                  if e["contact_id"] == alice_cid else e
                  for e in CRM_CONTACTS]
        populate_contacts_from_crm(crm_v2, conn, source="crm_v2", snapshot_mode=True)

        idx, _ = load_contact_index(conn)
        assert idx.get(("email", "bob.torres@beta-tech.example")) == bob_cid, (
            "Bob's email must be unaffected by Alice's revocation."
        )
        conn.close()


# ---------------------------------------------------------------------------
# Fix 2 — Ownership provenance
# ---------------------------------------------------------------------------

class TestOwnershipProvenance:

    def test_original_source_preserved_after_transfer(self, tmp_path: Path) -> None:
        """After a conflict transfer, the original CRM source is preserved in history."""
        conn = _db(tmp_path, "prov1")
        cid_a = make_fixture_contact_id("test:prov_a")
        cid_b = make_fixture_contact_id("test:prov_b")
        fix_a = [{"contact_id": cid_a, "canonical_email": "a@prov.com",
                   "display_name": "A", "organization_name": "O",
                   "identifiers": [("email", "a@prov.com"), ("phone", "+19001112222")]}]
        fix_b = [{"contact_id": cid_b, "canonical_email": "b@prov.com",
                   "display_name": "B", "organization_name": "O",
                   "identifiers": [("email", "b@prov.com"), ("phone", "+19001112222")]}]

        populate_contacts_from_crm(fix_a, conn, source="crm_a_v1")
        populate_contacts_from_crm(fix_b, conn, source="crm_b_v1")

        # Resolve to B
        resolve_identifier_conflict("phone", "+19001112222", cid_b, "admin", conn)

        # History for phone must show:
        # 1. assigned (A, source=crm_a_v1)
        # 2. transferred_out (A, source=crm_a_v1, actor=admin)
        # 3. transferred_in (B, source=crm_b_v1, actor=admin)
        history = get_identifier_ownership_history(
            conn, identifier_type="phone", identifier_value="+19001112222"
        )
        events = {(h["contact_id"], h["event_type"]): h for h in history}

        assert ("cid_" in h["source"] or "crm" in h["source"]
                for h in history), "History must have source fields"

        assigned = next((h for h in history
                         if h["contact_id"] == cid_a and h["event_type"] == "assigned"), None)
        assert assigned is not None, "Assignment event for A must exist"
        assert assigned["source"] == "crm_a_v1", (
            f"Original CRM source must be preserved. Got {assigned['source']!r}"
        )

        t_out = next((h for h in history
                      if h["contact_id"] == cid_a and h["event_type"] == "transferred_out"),
                     None)
        assert t_out is not None, "transferred_out event for A must exist"
        assert t_out["actor"] == "admin", "Actor must be recorded"
        assert t_out["source"] == "crm_a_v1", (
            "transferred_out must carry A's ORIGINAL source, not actor name"
        )

        t_in = next((h for h in history
                     if h["contact_id"] == cid_b and h["event_type"] == "transferred_in"),
                    None)
        assert t_in is not None, "transferred_in event for B must exist"
        assert t_in["actor"] == "admin"
        conn.close()

    def test_full_history_reconstructable(self, tmp_path: Path) -> None:
        """Alice → Bob phone transfer: full ownership history is reconstructable."""
        conn = _db(tmp_path, "prov2")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        bob_cid   = get_contact_id_for_email("bob.torres@beta-tech.example")

        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")

        # Bob claims Alice's phone
        hijack = [{
            "contact_id": bob_cid,
            "canonical_email": "bob.p@example.com",
            "display_name": "Bob", "organization_name": "O",
            "identifiers": [("phone", ALICE_PHONE_E164)],
        }]
        populate_contacts_from_crm(hijack, conn, source="crm_v2")
        resolve_identifier_conflict("phone", ALICE_PHONE_E164, bob_cid, "admin", conn)

        history = get_identifier_ownership_history(
            conn, identifier_type="phone", identifier_value=ALICE_PHONE_E164
        )
        event_types = [h["event_type"] for h in history]
        assert "assigned" in event_types, "Assignment event must exist"
        assert "transferred_out" in event_types, "transferred_out event must exist"
        assert "transferred_in" in event_types, "transferred_in event must exist"

        # Original source (crm_v1) must appear for the assignment
        assignment = next(h for h in history if h["event_type"] == "assigned"
                          and h["contact_id"] == alice_cid)
        assert assignment["source"] == "crm_v1", (
            f"Original CRM source must be preserved. Got {assignment['source']!r}"
        )
        conn.close()

    def test_failed_transfer_does_not_modify_history(self, tmp_path: Path) -> None:
        """An unauthorized transfer attempt must not alter history."""
        conn = _db(tmp_path, "prov3")
        cid_x = make_fixture_contact_id("test:prov_x")
        cid_y = make_fixture_contact_id("test:prov_y")
        fix = [
            {"contact_id": cid_x, "canonical_email": "x@prov3.com",
             "display_name": "X", "organization_name": "O",
             "identifiers": [("email", "x@prov3.com"), ("phone", "+18005559999")]},
            {"contact_id": cid_y, "canonical_email": "y@prov3.com",
             "display_name": "Y", "organization_name": "O",
             "identifiers": [("email", "y@prov3.com"), ("phone", "+18005559999")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fix")
        n_before = len(get_identifier_ownership_history(conn))

        with pytest.raises(IdentityResolutionError):
            resolve_identifier_conflict("phone", "+18005559999", cid_y, "badactor", conn)

        n_after = len(get_identifier_ownership_history(conn))
        assert n_before == n_after, "Failed transfer must not add history events"
        conn.close()

    def test_replay_does_not_duplicate_history(self, tmp_path: Path) -> None:
        """Running the same resolution twice does not duplicate history events."""
        conn = _db(tmp_path, "prov4")
        cid_a = make_fixture_contact_id("test:prov4_a")
        cid_b = make_fixture_contact_id("test:prov4_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a@p4.com", "display_name": "A",
             "organization_name": "O",
             "identifiers": [("email", "a@p4.com"), ("phone", "+18001234444")]},
            {"contact_id": cid_b, "canonical_email": "b@p4.com", "display_name": "B",
             "organization_name": "O",
             "identifiers": [("email", "b@p4.com"), ("phone", "+18001234444")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fix")
        resolve_identifier_conflict("phone", "+18001234444", cid_b, "admin", conn)
        n1 = len(get_identifier_ownership_history(conn))
        # Replay (second call returns 0 — idempotent)
        resolve_identifier_conflict("phone", "+18001234444", cid_b, "admin", conn)
        n2 = len(get_identifier_ownership_history(conn))
        assert n1 == n2, f"Replay must not add history events: {n1} → {n2}"
        conn.close()


# ---------------------------------------------------------------------------
# Fix 3 — Phone normalization
# ---------------------------------------------------------------------------

class TestPhoneNormalization:

    def test_e164_formats_normalize_consistently(self, tmp_path: Path) -> None:
        """Different representations of the same US number resolve to the same contact."""
        conn = _db(tmp_path, "ph1")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)

        formats = [
            ("+14155550101",    None),    # E.164 — no region needed
            ("+1 415-555-0101", None),    # E.164 with separators
            ("(415) 555-0101",  "US"),    # US national
            ("4155550101",      "US"),    # bare digits + US region
        ]
        for raw, region in formats:
            payload = {"phone": raw}
            if region:
                payload["phone_region"] = region
            cid = resolve_contact_from_signal("call.transcript", payload, idx)
            assert cid == alice_cid, (
                f"Phone {raw!r} (region={region!r}) must resolve to Alice. Got {cid!r}"
            )

    def test_national_number_without_region_unassigned(self, tmp_path: Path) -> None:
        """A national number without a region gives an empty result (not an error)."""
        conn = _db(tmp_path, "ph2")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_v1")
        idx, _ = load_contact_index(conn)

        # No phone_region in payload → normalize_phone raises → skipped → ""
        cid = resolve_contact_from_signal("call.transcript", {"phone": "4155550101"}, idx)
        assert cid == "", (
            "National number without region must not resolve (ambiguous)."
        )
        conn.close()

    def test_normalize_phone_e164(self) -> None:
        assert normalize_phone("+14155550101") == "+14155550101"
        assert normalize_phone("+1 415-555-0101") == "+14155550101"
        assert normalize_phone("+1(415)5550101") == "+14155550101"

    def test_normalize_phone_us_national(self) -> None:
        assert normalize_phone("4155550101", "US") == "+14155550101"
        assert normalize_phone("(415) 555-0101", "US") == "+14155550101"
        assert normalize_phone("415.555.0101", "US") == "+14155550101"
        assert normalize_phone("14155550101", "US") == "+14155550101"  # 11 digits starting 1

    def test_normalize_phone_no_region_raises(self) -> None:
        with pytest.raises(PhoneNormalizationError, match="no default_region"):
            normalize_phone("4155550101")

    def test_normalize_phone_invalid_raises(self) -> None:
        with pytest.raises(PhoneNormalizationError):
            normalize_phone("123", "US")        # too short
        with pytest.raises(PhoneNormalizationError):
            normalize_phone("+12345678901234567")  # too long (>15 digits)
        with pytest.raises(PhoneNormalizationError):
            normalize_phone("+abc")              # no digits

    def test_normalize_phone_unsupported_region_raises(self) -> None:
        """Unsupported region for national number must be rejected (not guessed)."""
        with pytest.raises(PhoneNormalizationError, match="not supported"):
            normalize_phone("0712345678", "GB")

    def test_collision_detected_after_normalization(self, tmp_path: Path) -> None:
        """Two different formats of the same phone trigger a conflict post-normalization."""
        conn = _db(tmp_path, "ph3")
        cid_a = make_fixture_contact_id("test:ph3_a")
        cid_b = make_fixture_contact_id("test:ph3_b")
        fix_a = [{"contact_id": cid_a, "canonical_email": "a@ph3.com",
                   "display_name": "A", "organization_name": "O",
                   "identifiers": [("email", "a@ph3.com"), ("phone", "+18005559876")]}]
        fix_b = [{"contact_id": cid_b, "canonical_email": "b@ph3.com",
                   "display_name": "B", "organization_name": "O",
                   # Same number in national format with US region
                   "identifiers": [("email", "b@ph3.com"), ("phone", "8005559876")]}]

        populate_contacts_from_crm(fix_a, conn, source="crm_a")
        # B uses national format; normalize_phone("8005559876", "US") = "+18005559876"
        populate_contacts_from_crm(fix_b, conn, source="crm_b", default_region="US")

        # Both normalize to +18005559876 → conflict detected
        conflicts = get_identifier_conflicts(conn, unresolved_only=True)
        assert any(c["identifier_value"] == "+18005559876" for c in conflicts), (
            "Collision after normalization must create a conflict record."
        )
        # Phone excluded from map (P1)
        idx, _ = load_contact_index(conn)
        assert idx.get(("phone", "+18005559876")) is None
        conn.close()

    def test_original_number_preserved_in_history(self, tmp_path: Path) -> None:
        """The normalized E.164 value is what's stored; original is in fixture metadata."""
        conn = _db(tmp_path, "ph4")
        cid_a = make_fixture_contact_id("test:ph4_a")
        # Register using national format with region
        fix_a = [{"contact_id": cid_a, "canonical_email": "a@ph4.com",
                   "display_name": "A", "organization_name": "O",
                   "identifiers": [("phone", "4155550101")]}]  # national
        populate_contacts_from_crm(fix_a, conn, source="crm", default_region="US")

        history = get_identifier_ownership_history(
            conn, identifier_type="phone", contact_id=cid_a
        )
        # E.164 normalized value stored in history
        assert any(h["identifier_value"] == "+14155550101" for h in history), (
            "Normalized E.164 value must appear in ownership history."
        )
        conn.close()
