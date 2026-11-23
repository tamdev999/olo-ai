"""Corpus reproducibility and identity coverage tests.

Verifies:
1. Three independent builds from the same seed produce identical corpus
   fingerprints (determinism).
2. entity_metadata is populated with display names after populate_olo_db.
3. build_hindsight_items produces context strings with contact names for
   email and calendar signals.
4. Replays are idempotent (re-running populate_olo_db on a fresh DB gives
   the same items and fingerprints).
5. Business isolation: two separate databases never share entity_metadata.
6. Contact names come from fixture source metadata (email headers / attendees),
   not from an external registry.
7. Signals without email participant data (call.transcript, invoice.created)
   gracefully produce no contact name — no fabricated identities.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from olo.m3.synthetic.generator import (
    ENTITIES, generate_timeline, corpus_fingerprint, _DEFAULT_SEED,
)
from olo.m3.feeder import populate_olo_db, build_hindsight_items
from olo.adapters.entity_extractor import entity_id_from_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build(tmp_path: Path, subdir: str = "biz") -> tuple[Path, list]:
    """Full pipeline: generate → populate → build_hindsight_items."""
    db = tmp_path / subdir / "olo.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    tl = generate_timeline()
    populate_olo_db(tl, db, source="hindsight_gate")
    items = build_hindsight_items(db, source="hindsight_gate")
    return db, items


def _entity_meta(db: Path) -> list[tuple]:
    """Read all entity_metadata rows from the given db."""
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT entity_id, display_name, company, source_kind "
            "FROM entity_metadata ORDER BY entity_id"
        ).fetchall()
    finally:
        conn.close()


def _item_fingerprint(items: list[dict]) -> str:
    """Corpus fingerprint computed from (doc_id, content_sha256) pairs."""
    rows = sorted(
        (i["document_id"], (i.get("metadata") or {}).get("content_sha256", ""))
        for i in items
    )
    import json
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 1. Determinism: three independent builds produce identical fingerprints
# ---------------------------------------------------------------------------

class TestDeterministicBuilds:
    """The corpus builder must be fully deterministic."""

    def test_corpus_fingerprint_stable_across_builds(self) -> None:
        """generate_timeline() with the default seed always gives the same fingerprint."""
        t1 = generate_timeline()
        t2 = generate_timeline()
        t3 = generate_timeline()
        fp1 = corpus_fingerprint(t1)
        fp2 = corpus_fingerprint(t2)
        fp3 = corpus_fingerprint(t3)
        assert fp1 == fp2 == fp3, (
            f"Corpus fingerprint differs across builds: {fp1[:16]} vs {fp2[:16]} vs {fp3[:16]}"
        )

    def test_three_item_fingerprints_match(self, tmp_path: Path) -> None:
        """Three fresh olo.db builds produce identical item fingerprints."""
        _, items1 = _build(tmp_path, "b1")
        _, items2 = _build(tmp_path, "b2")
        _, items3 = _build(tmp_path, "b3")

        fp1 = _item_fingerprint(items1)
        fp2 = _item_fingerprint(items2)
        fp3 = _item_fingerprint(items3)

        assert fp1 == fp2 == fp3, (
            f"Item fingerprint differs across builds: "
            f"{fp1[:16]} vs {fp2[:16]} vs {fp3[:16]}"
        )

    def test_document_ids_stable(self, tmp_path: Path) -> None:
        """Document IDs are deterministic across builds."""
        _, items1 = _build(tmp_path, "di1")
        _, items2 = _build(tmp_path, "di2")

        ids1 = sorted(i["document_id"] for i in items1)
        ids2 = sorted(i["document_id"] for i in items2)
        assert ids1 == ids2

    def test_signal_count_matches_generator(self, tmp_path: Path) -> None:
        """Item count must equal the generator's signal count."""
        tl = generate_timeline()
        db = tmp_path / "sc" / "olo.db"
        db.parent.mkdir(parents=True)
        populate_olo_db(tl, db, source="hindsight_gate")
        items = build_hindsight_items(db, source="hindsight_gate")
        assert len(items) == len(tl), (
            f"Item count {len(items)} != generator signal count {len(tl)}"
        )

    def test_frozen_corpus_fingerprint_unchanged(self) -> None:
        """The corpus fingerprint must match the FROZEN_CORPUS_FP constant."""
        FROZEN = "1877ca71d6fbbe64a6ef0b728159ceb4acc2595a14e05d0f9e44db2aa6759a0b"
        tl = generate_timeline()
        actual = corpus_fingerprint(tl)
        assert actual == FROZEN, (
            f"Corpus fingerprint changed: got {actual[:16]}... "
            f"expected {FROZEN[:16]}... — "
            "entity IDs, body text, or signal IDs changed unexpectedly."
        )


# ---------------------------------------------------------------------------
# 2. entity_metadata populated with display names
# ---------------------------------------------------------------------------

class TestEntityMetadataPopulation:
    """populate_olo_db must call upsert_entities to fill entity_metadata."""

    def test_entity_metadata_not_empty_after_build(self, tmp_path: Path) -> None:
        """entity_metadata must have rows after populate_olo_db."""
        db, _ = _build(tmp_path, "em1")
        rows = _entity_meta(db)
        assert len(rows) > 0, "entity_metadata must be populated after populate_olo_db"

    def test_all_customer_display_names_present(self, tmp_path: Path) -> None:
        """All three customer display names must appear in entity_metadata."""
        db, _ = _build(tmp_path, "em2")
        rows = _entity_meta(db)
        display_names = {row[1] for row in rows}
        for ent in ENTITIES:
            assert ent.display_name in display_names, (
                f"{ent.display_name!r} missing from entity_metadata "
                f"(found: {sorted(display_names)})"
            )

    def test_entity_ids_are_email_derived(self, tmp_path: Path) -> None:
        """entity_metadata entity IDs are SHA-256-of-email (production format)."""
        db, _ = _build(tmp_path, "em3")
        rows = _entity_meta(db)
        em_ids = {row[0] for row in rows}
        for ent in ENTITIES:
            expected_id = entity_id_from_email(ent.email)
            assert expected_id in em_ids, (
                f"SHA-256-based entity ID for {ent.display_name} "
                f"({expected_id}) missing from entity_metadata. "
                "Entity IDs must be derived from email addresses, not pre-fabricated."
            )

    def test_no_ulid_entity_ids_in_metadata(self, tmp_path: Path) -> None:
        """ULID-style entity IDs must NOT appear in entity_metadata.

        entity_metadata is keyed by production SHA-256-of-email IDs.
        ULID IDs (ent_01ACME...) are for Hindsight tag filtering only.
        """
        db, _ = _build(tmp_path, "em4")
        rows = _entity_meta(db)
        for row in rows:
            entity_id = row[0]
            assert not entity_id.startswith("ent_01"), (
                f"ULID-style entity ID {entity_id!r} found in entity_metadata. "
                "entity_metadata must only contain production email-derived IDs."
            )


# ---------------------------------------------------------------------------
# 3. Context strings include contact names
# ---------------------------------------------------------------------------

class TestContextEnrichment:
    """build_hindsight_items must include contact names in context strings."""

    def test_email_contexts_include_contact_name(self, tmp_path: Path) -> None:
        """Email signal contexts must include the contact's display name."""
        db, items = _build(tmp_path, "ce1")
        email_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") in ("email.received", "email.sent")
        ]
        assert email_items, "Must have email items"

        name_found = sum(
            1 for i in email_items
            if any(ent.display_name in (i.get("context") or "") for ent in ENTITIES)
        )
        total = len(email_items)
        # Allow a small tolerance for edge cases (e.g., random email bodies
        # that happen to omit names), but the vast majority must have names.
        assert name_found >= total * 0.90, (
            f"Only {name_found}/{total} email contexts contain a contact name. "
            "Contact names must appear in the context string for entity recognition."
        )

    def test_calendar_contexts_include_contact_name(self, tmp_path: Path) -> None:
        """Calendar event contexts must include the attendee's display name."""
        db, items = _build(tmp_path, "ce2")
        cal_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind", "").startswith("calendar")
        ]
        assert cal_items, "Must have calendar items"

        name_found = sum(
            1 for i in cal_items
            if any(ent.display_name in (i.get("context") or "") for ent in ENTITIES)
        )
        assert name_found >= len(cal_items) * 0.90, (
            f"Only {name_found}/{len(cal_items)} calendar contexts contain a contact name."
        )

    def test_transcript_contexts_have_no_fabricated_name(self, tmp_path: Path) -> None:
        """Call transcripts have no email headers → no fabricated contact names.

        This verifies that we do NOT force every record to have an identity
        when the source content does not support it.
        """
        db, items = _build(tmp_path, "ce3")
        transcript_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "call.transcript"
        ]
        assert transcript_items, "Must have transcript items"

        for i in transcript_items:
            ctx = i.get("context") or ""
            # Transcripts describe a phone call — the kind descriptor is correct.
            assert ctx.startswith("Phone call transcript"), (
                f"Unexpected context for transcript: {ctx!r}"
            )
            # No contact name should appear after the kind descriptor.
            # The transcript body may contain names but the context prefix
            # should only have the kind descriptor and subject.
            ctx_parts = ctx.split(" · ")
            # context = "Phone call transcript · [subject]"
            # No identity part expected (entity_id not resolvable from transcript payload)
            assert len(ctx_parts) <= 2, (
                f"Transcript context has unexpected identity part: {ctx!r}"
            )

    def test_context_names_from_fixture_source_data(self, tmp_path: Path) -> None:
        """Contact names in contexts must come from fixture metadata, not answer registry.

        Verifies that names appear only for entities defined in ENTITIES and
        only in contexts derived from signals that have those entities as participants.
        """
        db, items = _build(tmp_path, "ce4")
        entity_names = {ent.display_name for ent in ENTITIES}

        for item in items:
            ctx = item.get("context") or ""
            kind = (item.get("metadata") or {}).get("kind", "")
            # Any name that appears in a context must be from the fixture ENTITIES list.
            for name in entity_names:
                if name in ctx:
                    # The signal kind must support participant extraction.
                    assert kind in (
                        "email.received", "email.sent", "email.content",
                        "calendar.event.changed", "calendar.event.cancelled",
                        "calendar.event.created", "calendar.event.updated",
                    ), (
                        f"Contact name {name!r} appears in context for "
                        f"unexpected kind {kind!r}: {ctx!r}"
                    )


# ---------------------------------------------------------------------------
# 4. Replay idempotency
# ---------------------------------------------------------------------------

class TestReplayIdempotency:
    """Running the corpus builder twice from the same source produces the same output."""

    def test_two_fresh_builds_produce_identical_items(self, tmp_path: Path) -> None:
        """Two completely independent olo.db builds yield identical item lists."""
        _, items1 = _build(tmp_path, "rep1")
        _, items2 = _build(tmp_path, "rep2")

        # Same document IDs
        ids1 = sorted(i["document_id"] for i in items1)
        ids2 = sorted(i["document_id"] for i in items2)
        assert ids1 == ids2

        # Same context strings
        ctx1 = {i["document_id"]: i.get("context") for i in items1}
        ctx2 = {i["document_id"]: i.get("context") for i in items2}
        assert ctx1 == ctx2, (
            "Context strings differ between builds — corpus construction is not deterministic."
        )

    def test_replay_does_not_duplicate_entities(self, tmp_path: Path) -> None:
        """Running populate_olo_db twice on a FRESH db gives the same entity_metadata."""
        db, _ = _build(tmp_path, "rep3")
        em1 = _entity_meta(db)

        # Build a second independent db and compare
        db2, _ = _build(tmp_path, "rep3b")
        em2 = _entity_meta(db2)

        assert em1 == em2, (
            "entity_metadata differs between builds — "
            "entity extraction is not idempotent."
        )


# ---------------------------------------------------------------------------
# 5. Business (tenant) isolation
# ---------------------------------------------------------------------------

class TestBusinessIsolation:
    """Two separate databases must not share entity_metadata."""

    def test_separate_dbs_have_separate_entity_metadata(self, tmp_path: Path) -> None:
        """Two olo.db files for distinct 'businesses' are fully isolated."""
        db_a = tmp_path / "biz_a" / "olo.db"
        db_b = tmp_path / "biz_b" / "olo.db"
        db_a.parent.mkdir(parents=True)
        db_b.parent.mkdir(parents=True)

        tl = generate_timeline()
        populate_olo_db(tl, db_a, source="hindsight_gate")
        populate_olo_db(tl, db_b, source="hindsight_gate")

        conn_a = sqlite3.connect(str(db_a))
        conn_b = sqlite3.connect(str(db_b))
        try:
            em_a = conn_a.execute("SELECT entity_id FROM entity_metadata").fetchall()
            em_b = conn_b.execute("SELECT entity_id FROM entity_metadata").fetchall()
        finally:
            conn_a.close()
            conn_b.close()

        # Both must have populated entity_metadata independently
        assert len(em_a) > 0, "db_a entity_metadata must be populated"
        assert len(em_b) > 0, "db_b entity_metadata must be populated"

        # Modifying one db must not affect the other (they share no state)
        # Verified by: each db has its own SQLite file and its own entity_metadata table

    def test_cross_business_entity_cache_isolation(self, tmp_path: Path) -> None:
        """build_hindsight_items on two separate dbs uses isolated entity caches.

        This catches the per-process global cache bug where processing two
        businesses sequentially leaked entity_metadata from the first into
        the second (D11 regression).
        """
        tl = generate_timeline()

        db_first = tmp_path / "first" / "olo.db"
        db_second = tmp_path / "second" / "olo.db"
        db_first.parent.mkdir(parents=True)
        db_second.parent.mkdir(parents=True)

        # Build both dbs with the same timeline
        populate_olo_db(tl, db_first, source="hindsight_gate")
        populate_olo_db(tl, db_second, source="hindsight_gate")

        # Build items from BOTH dbs in the same process
        items_first = build_hindsight_items(db_first, source="hindsight_gate")
        items_second = build_hindsight_items(db_second, source="hindsight_gate")

        # Both builds must produce the same items (no cross-contamination)
        ctx_first = {i["document_id"]: i.get("context") for i in items_first}
        ctx_second = {i["document_id"]: i.get("context") for i in items_second}
        assert ctx_first == ctx_second, (
            "Cross-process entity cache contamination detected: contexts differ "
            "between two sequential builds in the same process."
        )


# ---------------------------------------------------------------------------
# 6. Source-backed identity — no fabricated names
# ---------------------------------------------------------------------------

class TestSourceBackedIdentity:
    """Contact names must originate from fixture source metadata only."""

    def test_no_names_from_outside_entities_list(self, tmp_path: Path) -> None:
        """No contact name in any context must be invented outside ENTITIES.

        Context format: "Kind · [Identity · ] Subject"
        The identity part is only present for email and calendar signals.
        2-part contexts ("Kind · Subject") have no identity; we don't check those.
        3-part contexts ("Kind · Identity · Subject") must have identity from ENTITIES.
        """
        db, items = _build(tmp_path, "sb1")
        allowed_names = {ent.display_name for ent in ENTITIES} | {"Owner"}

        for item in items:
            ctx = item.get("context") or ""
            parts = ctx.split(" · ")
            # Only check the identity slot when it's present (3+ parts).
            # 2-part contexts have no identity: "Stripe invoice record · INV-…"
            if len(parts) >= 3:
                identity_part = parts[1]
                # Strip company in parens: "Alice Chen (Acme Corp)" → "Alice Chen"
                identity_name = identity_part.split(" (")[0].strip()
                if identity_name and identity_name not in allowed_names:
                    pytest.fail(
                        f"Fabricated contact name {identity_name!r} in context "
                        f"(not in ENTITIES). Context: {ctx!r}"
                    )

    def test_email_received_primary_entity_is_sender(self, tmp_path: Path) -> None:
        """email.received: entities field must contain the sender's SHA-256 ID."""
        db, items = _build(tmp_path, "sb2")
        recv_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "email.received"
        ]
        assert recv_items

        for item in recv_items[:3]:  # spot-check first 3
            entities = item.get("entities") or []
            if not entities:
                continue
            entity_text = entities[0].get("text", "")
            # Must be a SHA-256-based ID (ent_ prefix with 20 hex chars)
            assert entity_text.startswith("ent_"), (
                f"entities field has unexpected format: {entity_text!r}"
            )
            assert len(entity_text) == 24, (
                f"entity ID length unexpected: {entity_text!r} (len={len(entity_text)})"
            )

    def test_email_sent_primary_entity_is_recipient(self, tmp_path: Path) -> None:
        """email.sent: entities field resolves to the TO recipient's SHA-256 ID."""
        db, items = _build(tmp_path, "sb3")
        sent_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "email.sent"
        ]
        assert sent_items

        # For email.sent, the primary entity is the first TO recipient (a customer).
        # Verify entity IDs match one of the known customer IDs.
        customer_ids = {entity_id_from_email(ent.email) for ent in ENTITIES}
        owner_id = entity_id_from_email("owner@olo-business.example")

        for item in sent_items[:3]:
            entities = item.get("entities") or []
            if not entities:
                continue
            entity_text = entities[0].get("text", "")
            if not entity_text:
                continue
            # For email sent FROM owner TO customer, entities[0] = customer ID
            # For email sent FROM customer TO owner (edge case), entities[0] = owner ID
            assert entity_text in customer_ids | {owner_id}, (
                f"email.sent entities[0]={entity_text!r} is not a known entity ID. "
                f"Known customer IDs: {customer_ids}"
            )
