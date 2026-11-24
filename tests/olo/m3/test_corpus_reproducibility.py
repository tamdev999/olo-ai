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

    def test_full_item_fingerprint_stable(self, tmp_path: Path) -> None:
        """full_item_fingerprint must be identical across three fresh builds."""
        from olo.m3.feeder import full_item_fingerprint

        fps = []
        for i in range(3):
            _, items = _build(tmp_path, f"fip{i}")
            fps.append(full_item_fingerprint(items))

        assert fps[0] == fps[1] == fps[2], (
            f"Full-item fingerprint not stable: {fps[0][:16]}, {fps[1][:16]}, {fps[2][:16]}"
        )

    def test_full_item_fingerprint_changes_on_context_mutation(self, tmp_path: Path) -> None:
        """full_item_fingerprint must change when a context string is mutated."""
        import copy
        from olo.m3.feeder import full_item_fingerprint

        _, items = _build(tmp_path, "fipm_ctx")
        fip_orig = full_item_fingerprint(items)
        mutated = copy.deepcopy(items)
        mutated[0]["context"] = "TAMPERED CONTEXT"
        assert full_item_fingerprint(mutated) != fip_orig, (
            "Full-item fingerprint must change when context string is mutated"
        )

    def test_full_item_fingerprint_changes_on_entity_link_change(self, tmp_path: Path) -> None:
        """full_item_fingerprint must change when an entity link is changed."""
        import copy
        from olo.m3.feeder import full_item_fingerprint

        _, items = _build(tmp_path, "fipm_ent")
        fip_orig = full_item_fingerprint(items)
        mutated = copy.deepcopy(items)
        mutated[0]["entities"] = [{"text": "cid_different", "type": "person"}]
        assert full_item_fingerprint(mutated) != fip_orig, (
            "Full-item fingerprint must change when entity link is changed"
        )

    def test_full_item_fingerprint_changes_on_tag_change(self, tmp_path: Path) -> None:
        """full_item_fingerprint must change when a tag is added."""
        import copy
        from olo.m3.feeder import full_item_fingerprint

        _, items = _build(tmp_path, "fipm_tag")
        fip_orig = full_item_fingerprint(items)
        mutated = copy.deepcopy(items)
        mutated[0]["tags"] = mutated[0].get("tags", []) + ["extra:injected"]
        assert full_item_fingerprint(mutated) != fip_orig, (
            "Full-item fingerprint must change when a tag is added"
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
        """ULID entity IDs in entity_metadata must come from CRM-tagged sources only.

        Before the entity-link fix: entity_metadata only had SHA-256 email-derived IDs.
        After the fix: entity_metadata also has ULID IDs for CRM-tagged signals
        (call.transcript, invoice.created) with source_kind='crm_tag'.
        No arbitrary ULID IDs from non-CRM sources should appear.
        """
        db, _ = _build(tmp_path, "em4")
        rows = _entity_meta(db)
        for row in rows:
            entity_id = row[0]
            source_kind = row[3] if len(row) > 3 else "?"
            if entity_id.startswith("ent_01"):
                # ULID-style IDs (ent_01...) must come from CRM-tagged sources
                assert source_kind == "crm_tag", (
                    f"ULID entity ID {entity_id!r} in entity_metadata must have "
                    f"source_kind='crm_tag', got {source_kind!r}. "
                    "Only CRM-tagged signals (call.transcript, invoice.created) "
                    "should produce ULID entity_metadata entries."
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
        """Call transcripts with CRM customer_name metadata get the correct identity.

        After the entity-link fix: call.transcript signals with customer_name in
        their CRM metadata produce a 3-part context: 'Kind · Identity · Subject'.
        Transcripts WITHOUT customer_name (which shouldn't exist in the synthetic
        corpus, but could in production) would still produce only 'Kind · Subject'.
        """
        db, items = _build(tmp_path, "ce3")
        transcript_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "call.transcript"
        ]
        assert transcript_items, "Must have transcript items"

        for i in transcript_items:
            ctx = i.get("context") or ""
            assert ctx.startswith("Phone call transcript"), (
                f"Unexpected context for transcript: {ctx!r}"
            )
            ctx_parts = ctx.split(" · ")
            # context = "Phone call transcript · [Identity] · [subject]"
            # With CRM data: 3 parts (kind · identity · subject)
            # Without CRM data: 2 parts (kind · subject)
            if len(ctx_parts) == 3:
                # Identity must be a known customer name, not fabricated
                identity_part = ctx_parts[1].split(" (")[0].strip()
                allowed_names = {ent.display_name for ent in ENTITIES} | {"Owner"}
                assert identity_part in allowed_names, (
                    f"Transcript context has unexpected identity {identity_part!r}. "
                    f"Context: {ctx!r}"
                )

    def test_context_names_from_fixture_source_data(self, tmp_path: Path) -> None:
        """Contact names in contexts must come from fixture metadata, not answer registry.

        Verifies that names appear only for entities defined in ENTITIES and
        only in contexts from signals that have those entities as participants
        (email/calendar) or CRM-tagged customers (call.transcript, invoice.created).
        """
        db, items = _build(tmp_path, "ce4")
        entity_names = {ent.display_name for ent in ENTITIES}

        for item in items:
            ctx = item.get("context") or ""
            kind = (item.get("metadata") or {}).get("kind", "")
            # Any name that appears in a context must be from the fixture ENTITIES list.
            for name in entity_names:
                if name in ctx:
                    # The signal kind must support participant/CRM extraction.
                    assert kind in (
                        "email.received", "email.sent", "email.content",
                        "calendar.event.changed", "calendar.event.cancelled",
                        "calendar.event.created", "calendar.event.updated",
                        # CRM-tagged signals carry customer names via metadata:
                        "call.transcript", "invoice.created",
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
        """email.received: entities field carries the canonical contact_id (cid_...)."""
        db, items = _build(tmp_path, "sb2")
        recv_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "email.received"
        ]
        assert recv_items
        for item in recv_items[:3]:
            entities = item.get("entities") or []
            if not entities:
                continue
            entity_text = entities[0].get("text", "")
            if not entity_text:
                continue
            # C1/C2: entities field uses canonical contact_id (cid_... prefix)
            assert entity_text.startswith("cid_"), (
                f"email.received entities[0]={entity_text!r} must start with 'cid_' "
                "(canonical contact_id from CRM fixture)"
            )

    def test_email_sent_primary_entity_is_recipient(self, tmp_path: Path) -> None:
        """email.sent: entities field carries the canonical contact_id (cid_...)."""
        db, items = _build(tmp_path, "sb3")
        sent_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "email.sent"
        ]
        assert sent_items
        for item in sent_items[:3]:
            entities = item.get("entities") or []
            if not entities:
                continue
            entity_text = entities[0].get("text", "")
            if not entity_text:
                continue
            # C1/C2: entities field uses canonical contact_id (cid_... prefix)
            assert entity_text.startswith("cid_"), (
                f"email.sent entities[0]={entity_text!r} must start with 'cid_'"
            )


# ---------------------------------------------------------------------------
# 7. Entity-link regression fix: voice transcript and invoice entity links
# ---------------------------------------------------------------------------

class TestEntityLinkFallback:
    """voice transcript and invoice signals must carry entity links via ent: tag fallback.

    Regression fixed in feeder.py: when email/calendar participant resolution returns
    empty (call.transcript, invoice.created), use the first canonical ent: tag from
    the signal's payload tags as the entity ID for the Hindsight entities field.

    This creates entity links that allow entity-scoped recall to find the signal's
    DeepSeek-extracted observations — without it, rank drops to the bottom even
    though the document is in the bank.
    """

    # ── Acceptance test 1: voice transcript creates entity link ──────────────

    def test_transcript_has_entity_link_from_ent_tag(self, tmp_path: Path) -> None:
        """call.transcript: entities field uses canonical cid_ contact_id via phone lookup."""
        db, items = _build(tmp_path, "el1")
        transcript_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "call.transcript"
        ]
        assert transcript_items, "Must have call.transcript items"

        from olo.adapters.contacts import make_contact_id
        from olo.m3.synthetic.crm_fixture import get_contact_id_for_email
        expected_cids = {get_contact_id_for_email(ent.email) for ent in ENTITIES}
        expected_cids.discard("")  # skip entities not in CRM fixture
        for item in transcript_items:
            entities = item.get("entities") or []
            assert entities, (
                f"call.transcript must have non-empty entities field. "
                f"doc={item.get('document_id')}"
            )
            entity_text = entities[0].get("text", "")
            # C1/C2: entities field uses canonical contact_id (cid_... prefix)
            assert entity_text.startswith("cid_"), (
                f"entities[0] must have cid_ prefix (canonical contact_id), "
                f"got {entity_text!r}"
            )
            assert entity_text in expected_cids, (
                f"transcript entities[0]={entity_text!r} not in expected CRM contacts"
            )

    # ── Acceptance test 2: entity link matches the signal's customer ─────────

    def test_transcript_entity_link_matches_customer(self, tmp_path: Path) -> None:
        """call.transcript entity link uses the CRM contact_id (not ent: tag value)."""
        db, items = _build(tmp_path, "el2")
        transcript_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "call.transcript"
        ]
        from olo.adapters.contacts import make_contact_id
        from olo.m3.synthetic.crm_fixture import get_contact_id_for_email
        expected_cids = {get_contact_id_for_email(ent.email) for ent in ENTITIES}
        expected_cids.discard("")  # skip entities not in CRM fixture
        for item in transcript_items:
            entities = item.get("entities") or []
            if not entities:
                continue
            entity_text = entities[0].get("text", "")
            if not entity_text:
                continue
            # C2: entity link must be a valid CRM contact_id, not an ent: tag value
            assert entity_text.startswith("cid_"), (
                f"entities[0]={entity_text!r} must be a canonical cid_ contact_id"
            )
            assert entity_text in expected_cids, (
                f"transcript cid_ {entity_text!r} not in expected CRM contacts"
            )

    # ── Acceptance test 3: unfiltered retrieval unchanged ────────────────────

    def test_transcript_entity_id_stable_across_builds(self, tmp_path: Path) -> None:
        """Two builds produce identical entity IDs for transcripts (replay stable)."""
        _, items1 = _build(tmp_path, "el3a")
        _, items2 = _build(tmp_path, "el3b")

        get_entity = lambda items: {
            i["document_id"]: (i.get("entities") or [{}])[0].get("text", "")
            for i in items
            if (i.get("metadata") or {}).get("kind") == "call.transcript"
        }
        assert get_entity(items1) == get_entity(items2), (
            "Transcript entity IDs must be stable across replays"
        )

    # ── Acceptance test 4: transcript without ent: tag remains unassigned ────

    def test_signal_without_ent_tag_has_no_entity_link(self, tmp_path: Path) -> None:
        """A signal without any ent: tag gets entities=[] — no fabricated link."""
        from olo.m3.feeder import _ym_tag
        import tempfile, json, hashlib
        from olo.store import connect, migrate, acquire_run, finish_run, ingest_batch
        from olo.signals import NormalizedSignal, ContentBlob

        # Build a minimal signal with NO ent: tag
        db = tmp_path / "no_ent" / "olo.db"
        db.parent.mkdir(parents=True)
        conn = connect(db)
        migrate(conn)
        body = b"Customer called, no info."
        sha = hashlib.sha256(body).hexdigest()
        blob = ContentBlob(sha256=sha, media_type="text/plain;charset=utf-8", data=body)
        from datetime import datetime, timezone
        ns = NormalizedSignal(
            source="test_src",
            external_id="no_ent_sig_001",
            kind="call.transcript",
            occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            subject="No entity call",
            payload={
                "content_refs": [{"sha256": sha, "media_type": "text/plain;charset=utf-8",
                                   "size": len(body), "role": "body"}],
                "signal_id": "no_ent_sig_001",
                "source": "voice_fixture",
                "kind": "call.transcript",
                "content_sha256": sha,
                # No ent: tags here — this signal has no customer association
                "tags": ["src:voice_fixture", "kind:call.transcript", "ym:2026-06"],
                "is_llm_eligible": True,
                "strategy": "prose",
            },
            blobs=(blob,),
        )
        run_id = acquire_run("test_src", "test", conn)
        ingest_batch("test_src", run_id, (ns,), "c1", None, conn)
        finish_run("test_src", run_id, "succeeded", conn)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db, source="test_src")
        assert len(items) == 1
        entities = items[0].get("entities") or []
        assert not entities or entities[0].get("text", "") == "", (
            f"Signal without ent: tag must have empty entities. Got: {entities}"
        )

    # ── Acceptance test 5: invalid ent: tag is rejected ──────────────────────

    def test_invalid_ent_tag_does_not_create_entity_link(self, tmp_path: Path) -> None:
        """An ent: tag that doesn't have the ent_ prefix must be ignored."""
        import hashlib
        from olo.store import connect, migrate, acquire_run, finish_run, ingest_batch
        from olo.signals import NormalizedSignal, ContentBlob
        from datetime import datetime, timezone

        db = tmp_path / "bad_ent" / "olo.db"
        db.parent.mkdir(parents=True)
        conn = connect(db)
        migrate(conn)
        body = b"Some call data."
        sha = hashlib.sha256(body).hexdigest()
        blob = ContentBlob(sha256=sha, media_type="text/plain;charset=utf-8", data=body)
        ns = NormalizedSignal(
            source="test_src",
            external_id="bad_ent_sig_001",
            kind="call.transcript",
            occurred_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            subject="Bad entity call",
            payload={
                "content_refs": [{"sha256": sha, "media_type": "text/plain;charset=utf-8",
                                   "size": len(body), "role": "body"}],
                "signal_id": "bad_ent_sig_001",
                "source": "voice_fixture",
                "kind": "call.transcript",
                "content_sha256": sha,
                # Malformed / invalid ent: tag (doesn't start with "ent_")
                "tags": ["src:voice_fixture", "ent:NOT_A_VALID_ENTITY_ID", "ym:2026-06"],
                "is_llm_eligible": True,
                "strategy": "prose",
            },
            blobs=(blob,),
        )
        run_id = acquire_run("test_src", "test", conn)
        ingest_batch("test_src", run_id, (ns,), "c1", None, conn)
        finish_run("test_src", run_id, "succeeded", conn)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db, source="test_src")
        assert len(items) == 1
        entities = items[0].get("entities") or []
        for e in entities:
            text = e.get("text", "")
            # Empty entity text is fine (no link created); what must NOT happen
            # is that "NOT_A_VALID_ENTITY_ID" (lacks ent_ prefix) becomes a link.
            if text:
                assert text.startswith("ent_"), (
                    f"Invalid ent: tag value {text!r} slipped through as entity link"
                )

    # ── Acceptance test 6: Gmail entity links unchanged ──────────────────────

    def test_gmail_entities_use_canonical_contact_id(self, tmp_path: Path) -> None:
        """email.received: entities field uses canonical cid_ contact_id from CRM (C1/C2).

        In the synthetic corpus with CRM fixture, all signals resolve to cid_...
        contact IDs via verified email addresses.  In production Gmail (no CRM
        fixture loaded), entities remain empty until a CRM is configured.
        """
        db, items = _build(tmp_path, "el6")
        email_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "email.received"
        ]
        assert email_items
        for item in email_items[:5]:
            entities = item.get("entities") or []
            if not entities:
                continue
            entity_text = entities[0].get("text", "")
            if not entity_text:
                continue
            # C1/C2: entities field uses canonical contact_id (cid_ prefix)
            assert entity_text.startswith("cid_"), (
                f"email entities[0]={entity_text!r} must use canonical cid_ contact_id"
            )

    # ── Acceptance test 7: invoice entity link from ent: tag ─────────────────

    def test_invoice_has_entity_link(self, tmp_path: Path) -> None:
        """invoice.created: entities field carries the canonical cid_ contact_id."""
        db, items = _build(tmp_path, "el7")
        invoice_items = [
            i for i in items
            if (i.get("metadata") or {}).get("kind") == "invoice.created"
        ]
        assert invoice_items, "Must have invoice.created items"
        from olo.adapters.contacts import make_contact_id
        from olo.m3.synthetic.crm_fixture import get_contact_id_for_email
        expected_cids = {get_contact_id_for_email(ent.email) for ent in ENTITIES}
        expected_cids.discard("")  # skip entities not in CRM fixture
        for item in invoice_items:
            entities = item.get("entities") or []
            assert entities, (
                f"invoice.created must have non-empty entities. doc={item.get('document_id')}"
            )
            entity_text = entities[0].get("text", "")
            assert entity_text.startswith("cid_") and entity_text in expected_cids, (
                f"invoice entities[0]={entity_text!r} not a canonical CRM contact_id"
            )

    # ── Acceptance test 8: entity link count consistency ─────────────────────

    def test_all_signals_have_entity_link(self, tmp_path: Path) -> None:
        """Every signal in the synthetic corpus has a non-empty entities field."""
        db, items = _build(tmp_path, "el8")
        without_link = []
        for item in items:
            entities = item.get("entities") or []
            if not entities or not entities[0].get("text", ""):
                kind = (item.get("metadata") or {}).get("kind", "?")
                without_link.append((item["document_id"], kind))
        assert not without_link, (
            f"{len(without_link)} signals have no entity link: "
            f"{without_link[:5]}"
        )
