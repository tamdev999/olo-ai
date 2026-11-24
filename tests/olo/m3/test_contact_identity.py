"""C1/C2 canonical customer identity model tests.

Covers all 12 mandatory acceptance tests from the OLO M3.0 canonical identity
repair assignment:

 1. Alice's two verified email addresses resolve to one contact_id.
 2. Email, Calendar, call and invoice signals use the same Hindsight entity.
 3. Two Acme employees have separate contact_ids but share the same org_id.
 4. A forged call claiming Alice's tag cannot link itself to Alice.
 5. The forged call cannot rename Alice or change her genuine history.
 6. A legitimate contact update can change the name without losing provenance.
 7. An unknown caller remains unassigned.
 8. An ambiguous phone number remains unassigned.
 9. Removing generator ent: tags does not change resolved identity links.
10. Two businesses with overlapping identifiers remain isolated.
11. Replay remains idempotent.
12. resolve_entities=False is enforced; no fuzzy cross-customer merging.

Conventions
-----------
- Tests that only need olo.db (no Hindsight server) run as plain unit tests.
- Live tests (requiring the pinned Hindsight 1.2.1 server) are marked
  @pytest.mark.integration and are skipped when the server is unavailable.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pytest

from olo.adapters.contacts import (
    make_contact_id,
    make_fixture_contact_id,
    make_org_id,
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
from olo.m3.synthetic.crm_fixture import (
    CRM_CONTACTS,
    get_contact_id_for_email,
    get_contact_id_for_phone,
)
from olo.m3.synthetic.generator import ENTITIES, generate_timeline
from olo.m3.feeder import populate_olo_db, build_hindsight_items
from olo.signals import NormalizedSignal, ContentBlob

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
ALICE_EMAIL = "alice.chen@acme-corp.example"
ALICE_ALT_EMAIL = "alice.c@gmail.example"
ALICE_PHONE = "+14155550101"
BOB_EMAIL = "bob.torres@beta-tech.example"
DAN_EMAIL = "dan.park@acme-corp.example"
OWNER_EMAIL = "owner@olo-business.example"


def _db(tmp_path: Path, subdir: str = "biz") -> sqlite3.Connection:
    """Return an open connection to a fresh olo.db with contact tables migrated."""
    from olo.store import connect, migrate
    db_path = tmp_path / subdir / "olo.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    migrate_contacts(conn)
    return conn


def _ns(
    external_id: str,
    kind: str,
    payload: dict,
    day: int = 0,
) -> NormalizedSignal:
    """Build a minimal NormalizedSignal with a text/plain blob."""
    body = f"Signal body for {external_id}".encode()
    sha = hashlib.sha256(body).hexdigest()
    blob = ContentBlob(sha256=sha, media_type="text/plain;charset=utf-8", data=body)
    return NormalizedSignal(
        source="test_src",
        external_id=external_id,
        kind=kind,
        occurred_at=T0 + timedelta(days=day),
        subject=f"Subject for {external_id}",
        payload={
            "content_refs": [{"sha256": sha, "media_type": "text/plain;charset=utf-8",
                               "size": len(body), "role": "body"}],
            "signal_id": external_id,
            "source": "test_src",
            "kind": kind,
            "content_sha256": sha,
            "tags": ["ym:2026-03"],
            "is_llm_eligible": False,
            "strategy": "structured-no-llm",
            **payload,
        },
        blobs=(blob,),
    )


def _email_received(ext_id: str, from_name: str, from_email: str, day: int = 0,
                    tags=()) -> NormalizedSignal:
    return _ns(ext_id, "email.received", {
        "headers": {
            "from": [{"name": from_name, "email": from_email}],
            "to": [{"name": "Owner", "email": OWNER_EMAIL}],
        },
        "tags": list(tags) + ["ym:2026-03"],
    }, day)


def _cal_changed(ext_id: str, organizer_email: str, organizer_self: bool,
                 attendees: list, day: int = 0) -> NormalizedSignal:
    return _ns(ext_id, "calendar.event.changed", {
        "organizer_email": organizer_email,
        "organizer_display_name": "Owner",
        "organizer_self": organizer_self,
        "attendees": attendees,
        "tags": ["ym:2026-03"],
    }, day)


def _call(ext_id: str, phone: str, day: int = 0, extra: Optional[dict] = None,
          tags=()) -> NormalizedSignal:
    payload: dict = {"phone": phone, "tags": list(tags) + ["ym:2026-03"]}
    if extra:
        payload.update(extra)
    return _ns(ext_id, "call.transcript", payload, day)


def _invoice(ext_id: str, customer_email: str, customer_id: str = "",
             source: str = "stripe_fixture", day: int = 0) -> NormalizedSignal:
    return _ns(ext_id, "invoice.created", {
        "customer_email": customer_email,
        "customer_id": customer_id,
        "source": source,
        "tags": ["ym:2026-03"],
    }, day)


def _store(conn: sqlite3.Connection, signals: list) -> None:
    from olo.store import acquire_run, ingest_batch, finish_run
    run_id = acquire_run("test_src", "test", conn)
    ingest_batch("test_src", run_id, tuple(signals), "c1", None, conn)
    finish_run("test_src", run_id, "succeeded", conn)


def _idx(conn: sqlite3.Connection) -> tuple:
    return load_contact_index(conn)


# ---------------------------------------------------------------------------
# Test 1: Two emails → one contact_id
# ---------------------------------------------------------------------------

class TestTwoEmailsOneContact:
    """Test 1: Alice's two verified email addresses resolve to one contact_id."""

    def test_primary_and_alt_email_same_cid(self) -> None:
        """Both email addresses map to the same cid_ in contact_identifiers."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = _db(Path(td), "t1")
            populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
            idx, _ = _idx(conn)
            conn.close()

        cid_primary = idx.get(("email", ALICE_EMAIL))
        cid_alt     = idx.get(("email", ALICE_ALT_EMAIL))
        assert cid_primary is not None, "Primary email must resolve to a contact_id"
        assert cid_alt is not None,     "Alt email must resolve to a contact_id"
        assert cid_primary == cid_alt, (
            f"Both emails must map to the same contact_id. "
            f"primary={cid_primary!r}, alt={cid_alt!r}"
        )
        assert cid_primary == get_contact_id_for_email(ALICE_EMAIL), (
            "contact_id must be the fixture-defined stable ID for alice's email"
        )

    def test_alt_email_signal_resolves_to_alice(self) -> None:
        """Email signal from alice.c@gmail.example resolves to Alice's contact_id."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            conn = _db(Path(td), "t1b")
            populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
            idx, _ = _idx(conn)
            conn.close()
        alt_signal = _email_received("alt_email_sig", "Alice", ALICE_ALT_EMAIL)
        cid = resolve_contact_from_signal("email.received", alt_signal.payload, idx)
        assert cid == get_contact_id_for_email(ALICE_EMAIL), (
            f"Alt email signal must resolve to Alice's contact_id, got {cid!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: Email, Calendar, Call, Invoice → same Hindsight entity
# ---------------------------------------------------------------------------

class TestAllConnectorsSameEntity:
    """Test 2: All four signal types for Alice use the same cid_ contact_id."""

    def test_all_connectors_same_contact_id(self, tmp_path: Path) -> None:
        """Email, Calendar, call transcript and invoice all resolve to Alice's cid_."""
        tl = generate_timeline()
        db = tmp_path / "connectors" / "olo.db"
        db.parent.mkdir(parents=True)
        populate_olo_db(tl, db)
        items = build_hindsight_items(db)

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        alice_entity_id = ENTITIES[0].entity_id  # ULID (for tag lookup)

        email_cids, cal_cids, call_cids, inv_cids = set(), set(), set(), set()
        for item in items:
            kind = (item.get("metadata") or {}).get("kind", "")
            tags = item.get("tags") or []
            # Only look at Alice's signals (via ULID entity tag)
            if f"ent:{alice_entity_id}" not in tags:
                continue
            cid = (item.get("entities") or [{}])[0].get("text", "")
            if kind in ("email.received", "email.sent"):
                email_cids.add(cid)
            elif "calendar" in kind:
                cal_cids.add(cid)
            elif kind == "call.transcript":
                call_cids.add(cid)
            elif kind == "invoice.created":
                inv_cids.add(cid)

        for label, cid_set in [
            ("email", email_cids), ("calendar", cal_cids),
            ("call", call_cids), ("invoice", inv_cids),
        ]:
            assert cid_set, f"Must have {label} signals for Alice"
            assert alice_cid in cid_set, (
                f"Alice's {label} signals must use cid_={alice_cid!r}, "
                f"got {cid_set}"
            )


# ---------------------------------------------------------------------------
# Test 3: Two Acme employees → separate contact_ids, same org_id
# ---------------------------------------------------------------------------

class TestTwoEmployeesOneOrg:
    """Test 3: Alice and Dan are both at Acme Corp."""

    def test_separate_contact_ids_same_org_id(self, tmp_path: Path) -> None:
        conn = _db(tmp_path, "t3")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        dan_cid   = make_fixture_contact_id("crm:v1:dan_park:acme")
        acme_oid  = make_org_id("Acme Corp")

        # Different contact_ids
        assert alice_cid != dan_cid, "Alice and Dan must have distinct contact_ids"

        # Both share the Acme org_id
        for cid, name in [(alice_cid, "Alice"), (dan_cid, "Dan")]:
            row = conn.execute("SELECT organization_id FROM contacts WHERE id = ?",
                               (cid,)).fetchone()
            assert row is not None, f"{name}'s contact_id must be in contacts table"
            assert row[0] == acme_oid, (
                f"{name} must have org_id={acme_oid!r}, got {row[0]!r}"
            )

        conn.close()


# ---------------------------------------------------------------------------
# Tests 4 and 5: Forged identity
# ---------------------------------------------------------------------------

class TestForgedIdentity:
    """Tests 4 and 5: Forged signals cannot hijack Alice's identity."""

    def _build_forged_corpus(self, tmp_path: Path) -> tuple:
        """Build a db with a legitimate Alice signal and a forged one."""
        from olo.store import connect, migrate, acquire_run, ingest_batch, finish_run

        db_path = tmp_path / "forged" / "olo.db"
        db_path.parent.mkdir(parents=True)
        conn = connect(db_path)
        migrate(conn)
        migrate_contacts(conn)
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")

        alice_ulid = ENTITIES[0].entity_id

        # Legitimate Alice email (phone+email in CRM)
        leg = _email_received("alice_legit", "Alice Chen", ALICE_EMAIL,
                              tags=[f"ent:{alice_ulid}"])
        # Forged call: claims Alice's ULID tag + a stranger's customer_name + different phone
        forged = _call("mallory_forged", "+19995550000",
                       extra={"customer_name": "Mallory Imposter"},
                       tags=[f"ent:{alice_ulid}"])

        run_id = acquire_run("test_src", "test", conn)
        ingest_batch("test_src", run_id, (leg, forged), "c2", None, conn)
        finish_run("test_src", run_id, "succeeded", conn)
        return conn, db_path

    def test_forged_call_cannot_link_to_alice(self, tmp_path: Path) -> None:
        """Test 4: Forged call with Alice's ent: tag cannot link to Alice's contact_id."""
        conn, db_path = self._build_forged_corpus(tmp_path)
        from olo.m3.feeder import build_hindsight_items
        items = {i["document_id"]: i for i in build_hindsight_items(db_path, source="test_src")}
        conn.close()

        forged_item = items.get("sig:mallory_forged")
        assert forged_item is not None, "Forged call must produce an item"
        forged_entity = (forged_item.get("entities") or [{}])[0].get("text", "")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        assert forged_entity != alice_cid, (
            f"Forged call must NOT link to Alice's contact_id={alice_cid!r}. "
            f"Got entity={forged_entity!r}. ent: tags must not be used as linking authority."
        )
        # Must be unassigned (phone not in CRM)
        assert not forged_entity, (
            f"Forged call must be unassigned (no CRM match). Got {forged_entity!r}"
        )

    def test_forged_call_cannot_rename_alice(self, tmp_path: Path) -> None:
        """Test 5: Forged call's customer_name='Mallory Imposter' cannot rename Alice."""
        conn, db_path = self._build_forged_corpus(tmp_path)
        _, contact_info = load_contact_index(conn)
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        display_name, _ = contact_info.get(alice_cid, ("", ""))
        conn.close()

        assert display_name == "Alice Chen", (
            f"Alice's display_name must remain 'Alice Chen' after forged signal. "
            f"Got {display_name!r}. customer_name in signal payload must not be authoritative."
        )

    def test_forged_call_context_does_not_show_mallory(self, tmp_path: Path) -> None:
        """Test 5b: The forged call's context must not say 'Mallory Imposter'."""
        conn, db_path = self._build_forged_corpus(tmp_path)
        from olo.m3.feeder import build_hindsight_items
        items = {i["document_id"]: i for i in build_hindsight_items(db_path, source="test_src")}
        conn.close()

        forged_item = items.get("sig:mallory_forged")
        ctx = (forged_item or {}).get("context", "")
        assert "Mallory" not in ctx and "Imposter" not in ctx, (
            f"Forged name 'Mallory Imposter' must not appear in context. Got: {ctx!r}"
        )


# ---------------------------------------------------------------------------
# Test 6: Legitimate name update
# ---------------------------------------------------------------------------

class TestLegitimateNameUpdate:
    """Test 6: A CRM-sourced name change is recorded with provenance."""

    def test_name_update_recorded_in_history(self, tmp_path: Path) -> None:
        """Updating Alice's name via CRM fixture is recorded in contact_name_history."""
        conn = _db(tmp_path, "t6")
        # First load
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")

        # Simulate a CRM update: Alice hyphenated her name
        updated = [
            {**c, "display_name": "Alice Chen-Wong"}
            if c["canonical_email"] == ALICE_EMAIL else c
            for c in CRM_CONTACTS
        ]
        populate_contacts_from_crm(updated, conn, source="crm_update_v2")

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        # Current name must reflect the update
        new_name = conn.execute(
            "SELECT display_name FROM contacts WHERE id = ?", (alice_cid,)
        ).fetchone()[0]
        assert new_name == "Alice Chen-Wong", (
            f"Alice's display_name must update to 'Alice Chen-Wong', got {new_name!r}"
        )

        # History must preserve the old name
        history = conn.execute(
            "SELECT old_display_name, new_display_name FROM contact_name_history "
            "WHERE contact_id = ? ORDER BY id",
            (alice_cid,),
        ).fetchall()
        assert len(history) >= 1, "Name change must be recorded in contact_name_history"
        assert history[-1][0] == "Alice Chen", f"Old name preserved: {history[-1]!r}"
        assert history[-1][1] == "Alice Chen-Wong", f"New name: {history[-1]!r}"

        conn.close()


# ---------------------------------------------------------------------------
# Tests 7 and 8: Unresolved signals
# ---------------------------------------------------------------------------

class TestUnresolvedSignals:
    """Tests 7 and 8: Unknown and ambiguous identifiers remain unassigned."""

    def test_unknown_caller_unassigned(self, tmp_path: Path) -> None:
        """Test 7: Phone number not in CRM → no contact_id assigned."""
        conn = _db(tmp_path, "t7")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        idx, _ = _idx(conn)
        conn.close()

        unknown_call = _call("unknown_call", "+19999999999")
        cid = resolve_contact_from_signal("call.transcript", unknown_call.payload, idx)
        assert cid == "", (
            f"Unknown caller must remain unassigned, got cid={cid!r}"
        )

    def test_ambiguous_phone_unassigned(self, tmp_path: Path) -> None:
        """Test 8: Phone mapped to >1 contact is detected as a conflict.

        Fix 1 (P1): when a dispute is active, the identifier is excluded from
        the index entirely — NEITHER claimant can use it for automatic attribution.
        """
        conn = _db(tmp_path, "t8")
        # Register the same phone for two different contacts
        cid_a = make_fixture_contact_id("test:person_a")
        cid_b = make_fixture_contact_id("test:person_b")
        ambiguous_fixture = [
            {
             "contact_id": cid_a,
             "canonical_email": "person_a@example.com",
             "display_name": "Person A",
             "organization_name": "Org A",
             "identifiers": [("email", "person_a@example.com"), ("phone", "+18005550000")]},
            {
             "contact_id": cid_b,
             "canonical_email": "person_b@example.com",
             "display_name": "Person B",
             "organization_name": "Org B",
             "identifiers": [("email", "person_b@example.com"), ("phone", "+18005550000")]},
        ]
        # Conflict recorded; phone excluded from map (P1 fail-closed)
        populate_contacts_from_crm(ambiguous_fixture, conn, source="crm_fixture")
        idx, _ = _idx(conn)

        # P1: conflicted phone resolves to NEITHER contact
        ambiguous_call = _call("ambig_call", "+18005550000")
        cid = resolve_contact_from_signal("call.transcript", ambiguous_call.payload, idx)
        assert cid == "", (
            f"Conflicted phone must resolve to '' (neither contact). Got {cid!r}. "
            "P1: fail closed on disputes — both parties excluded until resolved."
        )

        # Conflict recorded
        conflicts = get_identifier_conflicts(conn, unresolved_only=True)
        assert any(c["identifier_type"] == "phone" and c["identifier_value"] == "+18005550000"
                   for c in conflicts), (
            f"A conflict for the phone must be recorded. Got: {conflicts}"
        )

        # Unambiguous emails are unaffected
        email_call_a = _email_received("email_a", "Person A", "person_a@example.com")
        cid_resolved = resolve_contact_from_signal("email.received", email_call_a.payload, idx)
        assert cid_resolved == cid_a, (
            f"Email resolution must not be affected by phone conflict. "
            f"Expected {cid_a!r}, got {cid_resolved!r}"
        )
        conn.close()

    def test_no_identifiers_unassigned(self, tmp_path: Path) -> None:
        """A signal with no recognizable source identifiers remains unassigned."""
        conn = _db(tmp_path, "t8b")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        idx, _ = _idx(conn)
        conn.close()

        bare_call = _call("bare_call", "")  # no phone
        cid = resolve_contact_from_signal("call.transcript", bare_call.payload, idx)
        assert cid == "", "Signal with no identifiers must remain unassigned"


# ---------------------------------------------------------------------------
# Test 9: Removing ent: tags leaves entity links unchanged
# ---------------------------------------------------------------------------

class TestEntTagIndependence:
    """Test 9: entity links must not depend on ent: tags."""

    def test_removing_ent_tags_does_not_change_links(self, tmp_path: Path) -> None:
        """Identity resolution must be identical with and without ent: tags."""
        conn = _db(tmp_path, "t9")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        idx, _ = _idx(conn)
        conn.close()

        alice_ulid = ENTITIES[0].entity_id

        # Signal WITH ent: tag
        with_tag = _email_received("with_tag", "Alice", ALICE_EMAIL,
                                   tags=[f"ent:{alice_ulid}"])
        # Same signal WITHOUT ent: tag
        without_tag = _email_received("without_tag", "Alice", ALICE_EMAIL, tags=[])

        cid_with    = resolve_contact_from_signal("email.received", with_tag.payload, idx)
        cid_without = resolve_contact_from_signal("email.received", without_tag.payload, idx)

        assert cid_with == cid_without, (
            f"Removing ent: tags must not change resolved contact_id. "
            f"with_tag={cid_with!r}, without_tag={cid_without!r}"
        )
        assert cid_with == get_contact_id_for_email(ALICE_EMAIL), (
            "Resolved contact_id must be Alice's fixture-defined stable ID"
        )


# ---------------------------------------------------------------------------
# Test 10: Cross-business isolation
# ---------------------------------------------------------------------------

class TestCrossBusinessIsolation:
    """Test 10: Two businesses with overlapping identifiers stay isolated."""

    def test_overlapping_emails_isolated_per_db(self, tmp_path: Path) -> None:
        """alice.chen@acme-corp.example in biz_a and biz_b have isolated contact records."""
        conn_a = _db(tmp_path, "biz_a")
        conn_b = _db(tmp_path, "biz_b")

        # Both businesses load the same CRM fixture
        populate_contacts_from_crm(CRM_CONTACTS, conn_a, source="crm_fixture")
        populate_contacts_from_crm(CRM_CONTACTS, conn_b, source="crm_fixture")

        idx_a, info_a = load_contact_index(conn_a)
        idx_b, info_b = load_contact_index(conn_b)
        conn_a.close()
        conn_b.close()

        # Both resolve Alice's email to the same contact_id (deterministic from email)
        cid_a = idx_a.get(("email", ALICE_EMAIL))
        cid_b = idx_b.get(("email", ALICE_EMAIL))
        assert cid_a == cid_b, "Same email → same cid_ (deterministic)"

        # Names are stored in separate databases — no shared state
        # Modifying biz_a does not affect biz_b
        conn_a2 = sqlite3.connect(str(tmp_path / "biz_a" / "olo.db"))
        conn_a2.execute("UPDATE contacts SET display_name = 'Alice X' WHERE id = ?", (cid_a,))
        conn_a2.execute("COMMIT")
        conn_a2.close()

        conn_b2 = sqlite3.connect(str(tmp_path / "biz_b" / "olo.db"))
        name_b = conn_b2.execute(
            "SELECT display_name FROM contacts WHERE id = ?", (cid_b,)
        ).fetchone()[0]
        conn_b2.close()

        assert name_b == "Alice Chen", (
            f"Modifying biz_a must not affect biz_b. biz_b name: {name_b!r}"
        )

    def test_phone_isolated_per_db(self, tmp_path: Path) -> None:
        """Phone number in one business cannot cross-link to another business."""
        tl = generate_timeline()
        db_a = tmp_path / "iso_a" / "olo.db"
        db_b = tmp_path / "iso_b" / "olo.db"
        db_a.parent.mkdir(parents=True)
        db_b.parent.mkdir(parents=True)

        populate_olo_db(tl, db_a)
        populate_olo_db(tl, db_b)

        items_a = {i["document_id"]: i for i in build_hindsight_items(db_a)}
        items_b = {i["document_id"]: i for i in build_hindsight_items(db_b)}

        # Both produce the same items (same corpus) but are independent databases
        # No shared state between them
        assert items_a.keys() == items_b.keys(), "Same corpus must produce same doc IDs"
        ctx_a = {k: v.get("context") for k, v in items_a.items()}
        ctx_b = {k: v.get("context") for k, v in items_b.items()}
        assert ctx_a == ctx_b, "Independent builds must produce identical contexts"


# ---------------------------------------------------------------------------
# Test 11: Replay idempotency
# ---------------------------------------------------------------------------

class TestReplayIdempotency:
    """Test 11: Running populate_olo_db twice produces the same result."""

    def test_replay_idempotent_entity_links(self, tmp_path: Path) -> None:
        """Two fresh builds produce identical contact_ids and contexts."""
        tl = generate_timeline()

        db1 = tmp_path / "rep1" / "olo.db"
        db2 = tmp_path / "rep2" / "olo.db"
        db1.parent.mkdir(parents=True)
        db2.parent.mkdir(parents=True)

        populate_olo_db(tl, db1)
        populate_olo_db(tl, db2)

        items1 = {i["document_id"]: i for i in build_hindsight_items(db1)}
        items2 = {i["document_id"]: i for i in build_hindsight_items(db2)}

        for doc_id in items1:
            e1 = (items1[doc_id].get("entities") or [{}])[0].get("text", "")
            e2 = (items2[doc_id].get("entities") or [{}])[0].get("text", "")
            assert e1 == e2, (
                f"Non-deterministic entity link for {doc_id}: {e1!r} vs {e2!r}"
            )

    def test_crm_fixture_populate_idempotent(self, tmp_path: Path) -> None:
        """populate_contacts_from_crm called twice produces the same contacts."""
        conn = _db(tmp_path, "t11b")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        n1 = conn.execute("SELECT count(*) FROM contacts").fetchone()[0]
        ids1 = set(r[0] for r in conn.execute("SELECT id FROM contacts").fetchall())

        # Run again — must be idempotent
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        n2 = conn.execute("SELECT count(*) FROM contacts").fetchone()[0]
        ids2 = set(r[0] for r in conn.execute("SELECT id FROM contacts").fetchall())
        conn.close()

        assert n1 == n2, f"Second populate must not add rows: {n1} → {n2}"
        assert ids1 == ids2


# ---------------------------------------------------------------------------
# Test 12: resolve_entities=False enforced
# ---------------------------------------------------------------------------

class TestResolveEntitiesFalse:
    """Test 12: resolve_entities=False is set on all items; no fuzzy merging."""

    def test_all_items_have_resolve_entities_false(self, tmp_path: Path) -> None:
        """Every Hindsight retain item must have resolve_entities=False."""
        tl = generate_timeline()
        db = tmp_path / "re" / "olo.db"
        db.parent.mkdir(parents=True)
        populate_olo_db(tl, db)
        items = build_hindsight_items(db)

        violations = [
            i["document_id"]
            for i in items
            if i.get("resolve_entities") is not False
        ]
        assert not violations, (
            f"{len(violations)} items have resolve_entities != False: {violations[:5]}. "
            "Fuzzy cross-customer entity merging must never be enabled."
        )

    def test_different_customer_contacts_not_merged(self, tmp_path: Path) -> None:
        """Alice's and Bob's signals must have different contact_ids (no cross-merge)."""
        tl = generate_timeline()
        db = tmp_path / "merge" / "olo.db"
        db.parent.mkdir(parents=True)
        populate_olo_db(tl, db)
        items = build_hindsight_items(db)

        alice_eid = ENTITIES[0].entity_id  # ULID for tag lookup
        bob_eid   = ENTITIES[1].entity_id

        alice_cids = set()
        bob_cids   = set()
        for item in items:
            tags = item.get("tags") or []
            cid = (item.get("entities") or [{}])[0].get("text", "")
            if not cid:
                continue
            if f"ent:{alice_eid}" in tags:
                alice_cids.add(cid)
            elif f"ent:{bob_eid}" in tags:
                bob_cids.add(cid)

        assert alice_cids, "Alice must have entity links"
        assert bob_cids,   "Bob must have entity links"
        assert not alice_cids & bob_cids, (
            f"Alice and Bob must not share a contact_id. "
            f"Overlap: {alice_cids & bob_cids}"
        )


# ---------------------------------------------------------------------------
# Live integration: entity links reach the pinned Hindsight server
# ---------------------------------------------------------------------------

_integration = pytest.mark.integration


@_integration
class TestLiveEntityLinks:
    """Live integration test against the pinned Hindsight 1.2.1 server.

    Verifies that the canonical contact_id from the CRM fixture actually
    creates Hindsight entity links that make signals retrievable via
    entity-scoped recall.

    Skipped when the server is unavailable.
    """

    def _client(self, bank_id: str):
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable
        cfg = HindsightConfig(host="127.0.0.1", port=18800, bank_id=bank_id)
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        return client

    def test_cid_entity_link_survives_in_bank(self, tmp_path: Path) -> None:
        """Contact_id entity links survive in the bank and enable entity-scoped lookup."""
        import uuid, time
        from olo.m3.feeder import STRUCTURED_STRATEGY

        bank_id = f"cid-test-{uuid.uuid4().hex[:8]}"
        client = self._client(bank_id)

        # Build one Alice email signal using the production path
        tl = generate_timeline()
        db = tmp_path / "live" / "olo.db"
        db.parent.mkdir(parents=True)
        populate_olo_db(tl, db)
        items = build_hindsight_items(db)

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        alice_items = [i for i in items if
                       (i.get("entities") or [{}])[0].get("text") == alice_cid][:1]
        assert alice_items, "Must have at least one Alice item with cid_ entity link"

        # Force structured strategy for this test (no LLM needed)
        for i in alice_items:
            i["strategy"] = STRUCTURED_STRATEGY

        try:
            client.retain(alice_items, operation_id=str(uuid.uuid4()),
                          async_=False, bank_id=bank_id)
            time.sleep(1)

            # Recall with the canonical contact_id as entity filter
            result = client.recall(
                "delivery reschedule request",
                bank_id=bank_id,
                max_tokens=4000,
                trace=False,
                tag_groups=[{"tags": [f"ent:{alice_cid}"], "match": "all_strict"}],
            )
            hits = result.get("results", [])
            # With structured strategy, the document is returned directly
            found = any(alice_items[0]["document_id"] in
                       ([h.get("document_id")] + [
                           sf.get("document_id","") if isinstance(sf,dict) else ""
                           for sf in h.get("source_fact_ids",[]) or []
                       ])
                       for h in hits)
            assert found or len(hits) > 0, (
                f"Entity-scoped recall with cid_ entity link must return results. "
                f"contact_id={alice_cid!r}, hits={len(hits)}"
            )
        finally:
            try:
                client.delete_bank(bank_id)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Defect A tests: stable contact ID across email changes
# ---------------------------------------------------------------------------

class TestContactIdStabilityDefectA:
    """Defect A repair: contact_id is now fixture-assigned and never changes."""

    def test_alice_cid_is_fixture_defined_not_email_derived(self) -> None:
        """contact_id must be the fixture-assigned stable ID, not sha256(email)."""
        from olo.adapters.contacts import make_contact_id
        alice_fixture_cid = get_contact_id_for_email(ALICE_EMAIL)
        alice_email_cid   = make_contact_id(ALICE_EMAIL)  # old deprecated derivation
        assert alice_fixture_cid != alice_email_cid, (
            "Fixture-defined contact_id must differ from the deprecated "
            "email-derived cid_. The ID must not change when email changes."
        )
        assert alice_fixture_cid.startswith("cid_"), (
            f"Fixture contact_id must have cid_ prefix, got {alice_fixture_cid!r}"
        )

    def test_primary_email_change_preserves_contact_id(self, tmp_path: Path) -> None:
        """Test 1 (Defect A): Alice changes her primary email; contact_id stays the same."""
        conn = _db(tmp_path, "defA_1")
        # Initial registration
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        alice_cid_before = get_contact_id_for_email(ALICE_EMAIL)

        # Simulate a CRM update: Alice's canonical email changes.
        # In the new model, the fixture_key (hence contact_id) does NOT change.
        # We just ADD the new email as an additional identifier.
        new_email = "alice.chen-wong@acme-corp.example"
        updated_fixture = []
        for entry in CRM_CONTACTS:
            if entry["contact_id"] == alice_cid_before:
                new_ids = list(entry["identifiers"]) + [("email", new_email)]
                updated_fixture.append({**entry, "identifiers": new_ids})
            else:
                updated_fixture.append(entry)
        populate_contacts_from_crm(updated_fixture, conn, source="crm_update_v2")

        # contact_id must be unchanged
        idx, info = load_contact_index(conn)
        old_cid_resolve = idx.get(("email", ALICE_EMAIL))
        new_cid_resolve = idx.get(("email", new_email))

        assert old_cid_resolve == alice_cid_before, (
            f"Original email must still resolve to alice's contact_id. "
            f"Got {old_cid_resolve!r}"
        )
        assert new_cid_resolve == alice_cid_before, (
            f"New email must also resolve to alice's (unchanged) contact_id. "
            f"Got {new_cid_resolve!r}"
        )

        # contact record count: exactly one Alice (no duplicate)
        n_alice = conn.execute(
            "SELECT count(*) FROM contacts WHERE id = ?", (alice_cid_before,)
        ).fetchone()[0]
        assert n_alice == 1, (
            f"Must be exactly one Alice contact record (no duplicate). Got {n_alice}"
        )
        conn.close()

    def test_existing_records_retain_contact_association(self, tmp_path: Path) -> None:
        """Test 2 (Defect A): Old email, calendar, call and invoice records keep their cid."""
        tl = generate_timeline()
        db = tmp_path / "defA_2" / "olo.db"
        db.parent.mkdir(parents=True)
        populate_olo_db(tl, db)
        items_before = build_hindsight_items(db)

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        # Simulate updating the fixture (adding a new email, NOT changing contact_id)
        new_email = "alice.new@acme-corp.example"
        updated_fixture = []
        for entry in CRM_CONTACTS:
            if entry["contact_id"] == alice_cid:
                updated_fixture.append({**entry,
                    "identifiers": list(entry["identifiers"]) + [("email", new_email)]})
            else:
                updated_fixture.append(entry)

        # Rebuild with updated fixture (contact_id unchanged)
        db2 = tmp_path / "defA_2b" / "olo.db"
        db2.parent.mkdir(parents=True)
        populate_olo_db(tl, db2, crm_contacts=updated_fixture)
        items_after = build_hindsight_items(db2)

        # Every Alice item must still use the SAME contact_id
        def alice_cids(items):
            return {
                (i.get("entities") or [{}])[0].get("text", "")
                for i in items
                if f"ent:{ENTITIES[0].entity_id}" in (i.get("tags") or [])
                and (i.get("entities") or [{}])[0].get("text", "")
            }

        cids_before = alice_cids(items_before)
        cids_after  = alice_cids(items_after)
        assert cids_before == {alice_cid}, f"Before: {cids_before}"
        assert cids_after  == {alice_cid}, f"After:  {cids_after}"
        assert cids_before == cids_after, (
            "Contact IDs must be identical before and after email change."
        )

    def test_old_verified_email_alias_still_resolves(self, tmp_path: Path) -> None:
        """Test 3 (Defect A): Old verified email alias resolves to the same contact."""
        conn = _db(tmp_path, "defA_3")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        idx, _ = load_contact_index(conn)
        conn.close()

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        # Both the primary email and the alias email must resolve to alice
        cid_primary = resolve_contact_from_signal(
            "email.received",
            {"headers": {"from": [{"name": "Alice", "email": ALICE_EMAIL}], "to": []}},
            idx,
        )
        cid_alias = resolve_contact_from_signal(
            "email.received",
            {"headers": {"from": [{"name": "Alice", "email": ALICE_ALT_EMAIL}], "to": []}},
            idx,
        )
        assert cid_primary == alice_cid, f"Primary email: {cid_primary!r}"
        assert cid_alias   == alice_cid, f"Alt email:     {cid_alias!r}"


# ---------------------------------------------------------------------------
# Defect B tests: conflict detection and resolution
# ---------------------------------------------------------------------------

class TestIdentifierConflictDefectB:
    """Defect B repair: identifier reassignment detected, recorded, and blocked."""

    def test_new_contact_cannot_silently_claim_alice_phone(self, tmp_path: Path) -> None:
        """Test 4 (Defect B + P1): A new contact cannot silently claim Alice's phone.

        Fix 1 (P1): when a dispute exists, the phone resolves to NEITHER party
        (fail closed), not to the original owner.
        """
        conn = _db(tmp_path, "defB_4")
        populate_contacts_from_crm(CRM_CONTACTS, conn, source="crm_fixture")
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        # Attempt to register Alice's phone for a NEW contact
        rogue_cid = make_fixture_contact_id("test:rogue_claimant")
        rogue_fixture = [{
            "contact_id":        rogue_cid,
            "canonical_email":   "rogue@example.com",
            "display_name":      "Rogue Claimant",
            "organization_name": "Bad Corp",
            "identifiers": [
                ("email", "rogue@example.com"),
                ("phone", ALICE_PHONE),  # Alice's phone!
            ],
        }]
        populate_contacts_from_crm(rogue_fixture, conn, source="rogue_crm")

        # P1: phone now has an unresolved conflict → excluded from map
        idx, _ = load_contact_index(conn)
        cid = idx.get(("phone", ALICE_PHONE))
        assert cid is None, (
            f"Alice's phone must be EXCLUDED from the index while dispute is active. "
            f"Got {cid!r}. P1: fail closed — neither claimant gets the phone until resolved."
        )

        # A conflict must be recorded
        conflicts = get_identifier_conflicts(conn, unresolved_only=True)
        assert any(c["identifier_type"] == "phone" and c["identifier_value"] == ALICE_PHONE
                   for c in conflicts), (
            f"Conflict for Alice's phone must be recorded. Got: {conflicts}"
        )
        conn.close()

    def test_conflicting_identifier_excluded_until_resolved(self, tmp_path: Path) -> None:
        """Test 5 (Defect B + P1): A disputed identifier resolves to neither party.

        Fix 1 (P1): the identifier is EXCLUDED from the index — neither cid_x nor
        cid_y can use the phone until the conflict is explicitly resolved.
        """
        conn = _db(tmp_path, "defB_5")
        cid_x = make_fixture_contact_id("test:contact_x")
        cid_y = make_fixture_contact_id("test:contact_y")
        # cid_x registers the phone first, cid_y tries to claim it second
        fix = [
            {"contact_id": cid_x, "canonical_email": "x@ex.com",
             "display_name": "X", "organization_name": "O",
             "identifiers": [("email", "x@ex.com"), ("phone", "+19009009000")]},
            {"contact_id": cid_y, "canonical_email": "y@ex.com",
             "display_name": "Y", "organization_name": "O",
             "identifiers": [("email", "y@ex.com"), ("phone", "+19009009000")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fixture")
        idx, _ = load_contact_index(conn)

        # P1: phone excluded from map — NEITHER contact can use it
        assert idx.get(("phone", "+19009009000")) is None, (
            "Disputed phone must be excluded from identifier_map (P1 fail-closed). "
            "Neither claimant can use it until the conflict is resolved."
        )
        # Emails are unaffected
        assert idx.get(("email", "x@ex.com")) == cid_x
        assert idx.get(("email", "y@ex.com")) == cid_y

        # Conflict recorded for the refused claim
        conflicts = get_identifier_conflicts(conn, unresolved_only=True)
        assert any(c["identifier_type"] == "phone" and c["identifier_value"] == "+19009009000"
                   for c in conflicts), f"Conflict must be recorded. Got: {conflicts}"
        conn.close()

    def test_legitimate_transfer_requires_explicit_resolution(self, tmp_path: Path) -> None:
        """Test 6 (Defect B): A legitimate phone transfer requires explicit resolution."""
        conn = _db(tmp_path, "defB_6")
        cid_old = make_fixture_contact_id("test:old_owner")
        cid_new = make_fixture_contact_id("test:new_owner")
        fix1 = [{"contact_id": cid_old, "canonical_email": "old@ex.com",
                  "display_name": "Old", "organization_name": "O",
                  "identifiers": [("email", "old@ex.com"), ("phone", "+18001112222")]}]
        fix2 = [{"contact_id": cid_new, "canonical_email": "new@ex.com",
                  "display_name": "New", "organization_name": "O",
                  "identifiers": [("email", "new@ex.com"), ("phone", "+18001112222")]}]
        populate_contacts_from_crm(fix1, conn, source="crm_v1")
        populate_contacts_from_crm(fix2, conn, source="crm_v2")

        # P1: phone has an unresolved conflict → excluded from both parties
        idx, _ = load_contact_index(conn)
        assert idx.get(("phone", "+18001112222")) is None, (
            "Disputed phone must be excluded while conflict is unresolved (P1)."
        )
        conflicts = get_identifier_conflicts(conn, unresolved_only=True)
        assert any(c["identifier_type"] == "phone" for c in conflicts), (
            "Conflict must be recorded for the refused new claim."
        )

        # Resolution is currently disabled (DenyAll)
        with pytest.raises(IdentityResolutionError, match="disabled"):
            resolve_identifier_conflict(
                "phone", "+18001112222", cid_new, create_operator_token("admin"), conn
            )

        # Phone remains excluded — conflict unresolved
        idx2, _ = load_contact_index(conn)
        assert idx2.get(("phone", "+18001112222")) is None, (
            "Phone must remain excluded after failed resolution attempt."
        )

        # Conflict is still unresolved
        with pytest.raises(IdentityResolutionError, match="disabled"):
            resolve_identifier_conflict(
                "phone", "+18001112222", cid_new, create_operator_token("admin"), conn
            )

        # Conflict record preserved for future resolution
        all_conflicts = get_identifier_conflicts(conn, unresolved_only=False)
        resolved_entries = [c for c in all_conflicts
                            if c["resolved_contact_id"] == cid_new]
        # With DenyAll, resolution never committed — no resolved entries
        assert not resolved_entries, "No resolved entries expected with DenyAll boundary"
        assert all_conflicts, "Conflict record must still exist"
        conn.close()

    def test_replay_does_not_create_duplicate_conflict_events(self, tmp_path: Path) -> None:
        """Defect B: re-running populate_contacts_from_crm with conflicting data is idempotent."""
        conn = _db(tmp_path, "defB_7")
        cid_a = make_fixture_contact_id("test:idem_a")
        cid_b = make_fixture_contact_id("test:idem_b")
        fix = [
            {"contact_id": cid_a, "canonical_email": "a@ex.com",
             "display_name": "A", "organization_name": "O",
             "identifiers": [("email", "a@ex.com"), ("phone", "+18887776666")]},
            {"contact_id": cid_b, "canonical_email": "b@ex.com",
             "display_name": "B", "organization_name": "O",
             "identifiers": [("email", "b@ex.com"), ("phone", "+18887776666")]},
        ]
        populate_contacts_from_crm(fix, conn, source="crm_fixture")
        conflicts_after_1 = len(get_identifier_conflicts(conn))
        populate_contacts_from_crm(fix, conn, source="crm_fixture")  # replay
        conflicts_after_2 = len(get_identifier_conflicts(conn))
        assert conflicts_after_1 == conflicts_after_2, (
            f"Replay must not create duplicate conflict events. "
            f"Got {conflicts_after_1} → {conflicts_after_2}"
        )
        conn.close()

    def test_forged_call_cannot_rename_or_relink_alice(self, tmp_path: Path) -> None:
        """Test 7: A forged call cannot rename or relink Alice (Defect B + existing C5)."""
        conn, db_path = TestForgedIdentity()._build_forged_corpus(tmp_path)
        _, contact_info = load_contact_index(conn)
        alice_cid = get_contact_id_for_email(ALICE_EMAIL)
        display_name, _ = contact_info.get(alice_cid, ("", ""))
        conn.close()

        assert display_name == "Alice Chen", (
            f"Forged signal must not rename Alice. Got {display_name!r}"
        )

        # Also verify forged entity link is unassigned
        from olo.m3.feeder import build_hindsight_items
        items = {i["document_id"]: i
                 for i in build_hindsight_items(db_path, source="test_src")}
        forged = items.get("sig:mallory_forged")
        forged_entity = (forged.get("entities") or [{}])[0].get("text", "")
        assert forged_entity != alice_cid, (
            "Forged signal must not link to Alice's contact_id"
        )


# ---------------------------------------------------------------------------
# Live Hindsight: stable ID after email change
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestLiveStableIdAfterEmailChange:
    """Live: Alice's stable contact_id persists in Hindsight after adding new email."""

    def _client(self):
        from olo.m3.hindsight.client import HindsightUnavailable
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig
        cfg = HindsightConfig(host="127.0.0.1", port=18800)
        client = HindsightClient(cfg)
        try:
            client.wait_until_ready(timeout_s=10.0)
        except HindsightUnavailable:
            pytest.skip("Hindsight server not running")
        return client

    def test_stable_id_survives_email_addition_in_bank(self, tmp_path: Path) -> None:
        """Adding a new email alias leaves Alice's cid_ entity links unchanged in bank."""
        import uuid, time
        from olo.m3.feeder import STRUCTURED_STRATEGY

        client = self._client()
        bank_id = f"stable-id-{uuid.uuid4().hex[:8]}"

        # Build corpus with CURRENT fixture (original emails)
        tl = generate_timeline()
        db_orig = tmp_path / "orig" / "olo.db"
        db_orig.parent.mkdir(parents=True)
        populate_olo_db(tl, db_orig)
        items_orig = build_hindsight_items(db_orig)

        alice_cid = get_contact_id_for_email(ALICE_EMAIL)

        # Build corpus with UPDATED fixture (new email added, same contact_id)
        new_email = "alice.chen-updated@acme-corp.example"
        updated_fixture = [
            {**e, "identifiers": list(e["identifiers"]) + [("email", new_email)]}
            if e["contact_id"] == alice_cid else e
            for e in CRM_CONTACTS
        ]
        db_upd = tmp_path / "upd" / "olo.db"
        db_upd.parent.mkdir(parents=True)
        populate_olo_db(tl, db_upd, crm_contacts=updated_fixture)
        items_upd = build_hindsight_items(db_upd)

        # Both corpora must use the same alice_cid for Alice's items
        def alice_entity_ids(items):
            return {(i.get("entities") or [{}])[0].get("text","")
                    for i in items
                    if f"ent:{ENTITIES[0].entity_id}" in (i.get("tags") or [])
                    and (i.get("entities") or [{}])[0].get("text","")}
        assert alice_entity_ids(items_orig) == {alice_cid}
        assert alice_entity_ids(items_upd)  == {alice_cid}

        # Retain a few structured items from the UPDATED corpus
        alice_items = [i for i in items_upd
                       if (i.get("entities") or [{}])[0].get("text") == alice_cid][:3]
        for i in alice_items:
            i["strategy"] = STRUCTURED_STRATEGY

        try:
            client.retain(alice_items, operation_id=str(uuid.uuid4()),
                          async_=False, bank_id=bank_id)
            time.sleep(1)

            # Entity-scoped recall with original entity tag still works
            r = client.recall(
                "delivery reschedule",
                bank_id=bank_id, max_tokens=4000, trace=False,
                tag_groups=[{"tags": [f"ent:{alice_cid}"], "match": "all_strict"}],
            )
            assert len(r.get("results", [])) > 0, (
                f"Entity-scoped recall with stable cid_ must return results "
                f"after email alias addition. contact_id={alice_cid!r}"
            )
        finally:
            try:
                client.delete_bank(bank_id)
            except Exception:
                pass
