"""Benchmark pre-flight tests: scope-equivalence and full-item fingerprint.

Covers Opus P4 (scope-equivalence assertion) and C4 (full-item fingerprint
persistence) preflight requirements.

Positive tests:
  - All 388 synthetic corpus items pass scope-equivalence (ent: ULID ↔ CRM ↔ entities).
  - Full-item fingerprint matches FROZEN_FULL_ITEM_FP across three independent builds.
  - PreflightResult.ok is True for the committed corpus.

Negative tests:
  - Altering a generator ent: tag causes preflight failure.
  - Deleting a CRM identifier (Alice's email) causes preflight failure.
  - Completely removing a customer from CRM causes preflight failure.
  - Unknown ULID in ent: tag is detected and reported.
  - Item with entity link but no ent: tag is flagged.
  - Wrong entity link (cid_ for the wrong customer) is detected.
  - Tampered context causes full-item fingerprint mismatch.
  - Tampered entity link causes full-item fingerprint mismatch.
  - Tampered tag causes full-item fingerprint mismatch.
"""
from __future__ import annotations

import copy
import hashlib
import tempfile
from pathlib import Path

import pytest

from olo.m3.synthetic.generator import ENTITIES, generate_timeline
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS, get_contact_id_for_email
from olo.m3.feeder import populate_olo_db, build_hindsight_items
from olo.m3.benchmark_preflight import (
    FROZEN_FULL_ITEM_FP,
    ScopeEquivalenceResult,
    FingerprintResult,
    PreflightResult,
    PreflightError,
    check_scope_equivalence,
    check_full_item_fingerprint,
    run_preflight,
)
from olo.adapters.contacts import make_fixture_contact_id


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_items(tmp_path: Path, subdir: str = "corpus",
                 crm_contacts=None) -> list[dict]:
    """Build fresh olo.db and return Hindsight retain items."""
    tl = generate_timeline()
    db = tmp_path / subdir / "olo.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    populate_olo_db(tl, db, crm_contacts=crm_contacts)
    return build_hindsight_items(db)


# ---------------------------------------------------------------------------
# Positive tests
# ---------------------------------------------------------------------------

class TestScopeEquivalencePositive:
    """All 388 corpus items must pass scope-equivalence."""

    def test_all_388_items_pass(self, tmp_path: Path) -> None:
        """Positive: all items in the committed corpus pass scope-equivalence."""
        items = _build_items(tmp_path)
        result = check_scope_equivalence(items)

        assert result.ok, (
            f"Pre-flight scope-equivalence failed:\n{result.summary_text()}"
        )
        assert result.passed == len(items), (
            f"Expected all {len(items)} items to pass, "
            f"got {result.passed} passed, "
            f"{result.mismatched} mismatched, "
            f"{result.unknown_ulid} unknown_ulid, "
            f"{result.missing_crm} missing_crm"
        )

    def test_no_unassigned_items_in_corpus(self, tmp_path: Path) -> None:
        """Every item in the synthetic corpus has a CRM-resolved entity link."""
        items = _build_items(tmp_path, "no_unassigned")
        result = check_scope_equivalence(items)
        assert result.missing_crm == 0, (
            f"{result.missing_crm} items have no CRM coverage. "
            "All synthetic customers must be in the CRM fixture."
        )

    def test_no_items_without_ent_tag(self, tmp_path: Path) -> None:
        """Every synthetic corpus signal has exactly one ent: ULID tag."""
        items = _build_items(tmp_path, "has_tags")
        result = check_scope_equivalence(items)
        assert result.no_ent_tag == 0, (
            f"{result.no_ent_tag} items have no ent: tag — expected 0 for synthetic corpus"
        )

    def test_preflight_passes_on_committed_corpus(self, tmp_path: Path) -> None:
        """run_preflight() returns ok=True for the committed corpus."""
        items = _build_items(tmp_path, "full_pf")
        result = run_preflight(items)
        assert result.ok, str(result.scope.summary_text()) + "\n" + result.fingerprint.summary_text()
        # Must not raise
        result.raise_if_failed()


# ---------------------------------------------------------------------------
# Negative tests: scope-equivalence
# ---------------------------------------------------------------------------

class TestScopeEquivalenceNegative:
    """Preflight must fail closed on any identity mismatch."""

    def test_altered_ent_tag_detected(self, tmp_path: Path) -> None:
        """Negative: changing an item's ent: tag to wrong ULID causes failure."""
        items = _build_items(tmp_path, "alt_tag")
        mutated = copy.deepcopy(items)

        alice_ulid = ENTITIES[0].entity_id
        bob_ulid   = ENTITIES[1].entity_id

        # Find an Alice item and change its ent: tag to Bob's ULID
        changed = 0
        for item in mutated:
            if f"ent:{alice_ulid}" in (item.get("tags") or []):
                item["tags"] = [
                    t.replace(f"ent:{alice_ulid}", f"ent:{bob_ulid}")
                    for t in item["tags"]
                ]
                changed += 1
                break

        assert changed > 0, "Must find at least one Alice item to mutate"

        result = check_scope_equivalence(mutated)
        assert not result.ok, (
            "Pre-flight must FAIL when an ent: tag is altered to the wrong ULID. "
            "An Alice item with Bob's ULID should mismatch (expected Bob's cid_, "
            "actual Alice's cid_)."
        )
        assert result.mismatched > 0, (
            f"Expected mismatched > 0, got {result.mismatched}"
        )

    def test_deleted_crm_email_identifier_causes_failure(self, tmp_path: Path) -> None:
        """Negative: removing Alice's email from CRM causes her signals to fail preflight.

        Without the email identifier, email/calendar signals for Alice resolve to ""
        instead of Alice's cid_ → mismatch.
        """
        # Remove Alice's primary email from the CRM fixture
        crm_no_alice_email = []
        alice_email = "alice.chen@acme-corp.example"
        for entry in CRM_CONTACTS:
            if entry["contact_id"] == get_contact_id_for_email(alice_email):
                # Keep Alice but remove her email identifiers
                new_ids = [
                    (t, v) for t, v in entry["identifiers"] if t != "email"
                ]
                crm_no_alice_email.append({**entry, "identifiers": new_ids})
            else:
                crm_no_alice_email.append(entry)

        items = _build_items(tmp_path, "no_alice_email", crm_contacts=crm_no_alice_email)
        # Check against the FULL CRM fixture (which expects Alice to have a cid_)
        result = check_scope_equivalence(items, crm_contacts=CRM_CONTACTS)
        assert not result.ok, (
            "Pre-flight must FAIL when Alice's email identifier is removed from CRM. "
            "Her email/calendar signals resolve to '' instead of her cid_."
        )
        assert result.mismatched > 0, (
            f"Expected mismatched > 0 when Alice's email removed, got {result.mismatched}"
        )

    def test_deleted_crm_contact_causes_failure(self, tmp_path: Path) -> None:
        """Negative: removing a customer's CRM entry entirely causes preflight failure."""
        # Remove Alice entirely from CRM
        crm_no_alice = [c for c in CRM_CONTACTS
                        if c["contact_id"] != get_contact_id_for_email("alice.chen@acme-corp.example")]
        items = _build_items(tmp_path, "no_alice_crm", crm_contacts=crm_no_alice)
        # Check against full CRM — Alice should have been there
        result = check_scope_equivalence(items, crm_contacts=CRM_CONTACTS)
        assert not result.ok, (
            "Pre-flight must FAIL when a customer's CRM entry is completely missing."
        )

    def test_unknown_ulid_detected(self, tmp_path: Path) -> None:
        """Negative: an item with an unknown (non-ENTITIES) ULID in ent: tag is flagged."""
        items = _build_items(tmp_path, "unknown_ulid")
        mutated = copy.deepcopy(items[:1])

        # Put a totally made-up ULID in the ent: tag
        fake_ulid = "ent_XXXXXXXXXXXXXXXXXXXXXXXX"
        mutated[0]["tags"] = [
            t if not t.startswith("ent:") else f"ent:{fake_ulid}"
            for t in (mutated[0].get("tags") or [])
        ]

        result = check_scope_equivalence(mutated)
        assert result.unknown_ulid > 0, (
            f"Unknown ULID must be detected. Got unknown_ulid={result.unknown_ulid}"
        )
        assert not result.ok

    def test_entity_link_without_ent_tag_flagged(self, tmp_path: Path) -> None:
        """Negative: an item that has a cid_ entity link but no ent: tag is flagged."""
        items = _build_items(tmp_path, "no_tag_link")
        mutated = copy.deepcopy(items[:1])

        # Remove ent: tags but keep the entity link
        mutated[0]["tags"] = [t for t in (mutated[0].get("tags") or [])
                               if not t.startswith("ent:")]
        # Ensure entity link is non-empty
        if not (mutated[0].get("entities") or [{}])[0].get("text"):
            mutated[0]["entities"] = [{"text": "cid_some_link", "type": "person"}]

        result = check_scope_equivalence(mutated)
        assert result.unexpected_link > 0, (
            "An item with a cid_ entity link but no ent: tag must be flagged."
        )
        assert not result.ok

    def test_wrong_entity_link_detected(self, tmp_path: Path) -> None:
        """Negative: an Alice item with Bob's cid_ in the entities field is a mismatch."""
        items = _build_items(tmp_path, "wrong_link")
        mutated = copy.deepcopy(items)

        alice_ulid = ENTITIES[0].entity_id
        bob_cid    = get_contact_id_for_email(ENTITIES[1].email)

        # Find an Alice item and replace its entity link with Bob's cid_
        changed = 0
        for item in mutated:
            if f"ent:{alice_ulid}" in (item.get("tags") or []):
                item["entities"] = [{"text": bob_cid, "type": "person"}]
                changed += 1
                break

        assert changed > 0
        result = check_scope_equivalence(mutated)
        assert result.mismatched > 0, (
            "An item with the wrong cid_ (another customer's) must be a mismatch."
        )
        assert not result.ok

    def test_run_preflight_raises_on_mismatch(self, tmp_path: Path) -> None:
        """run_preflight().raise_if_failed() raises PreflightError on scope failure."""
        items = _build_items(tmp_path, "raise_test")
        mutated = copy.deepcopy(items[:5])
        # Put a wrong entity link on one item
        alice_cid = get_contact_id_for_email(ENTITIES[0].email)
        bob_cid   = get_contact_id_for_email(ENTITIES[1].email)
        for item in mutated:
            if (item.get("entities") or [{}])[0].get("text") == alice_cid:
                item["entities"] = [{"text": bob_cid, "type": "person"}]
                break
        result = run_preflight(mutated)
        assert not result.ok
        with pytest.raises(PreflightError):
            result.raise_if_failed()


# ---------------------------------------------------------------------------
# Negative tests: full-item fingerprint
# ---------------------------------------------------------------------------

class TestFingerprintNegative:
    """Full-item fingerprint must change when any field changes."""

    def _base_items(self, tmp_path):
        return _build_items(tmp_path, "fp_base")

    def test_tampered_context_changes_fingerprint(self, tmp_path: Path) -> None:
        """Context string change → fingerprint mismatch."""
        items = self._base_items(tmp_path)
        fp_orig = check_full_item_fingerprint(items)
        assert fp_orig.ok

        mutated = copy.deepcopy(items)
        mutated[0]["context"] = "TAMPERED CONTEXT"
        fp_mut = check_full_item_fingerprint(mutated)
        assert not fp_mut.ok, "Tampered context must change the fingerprint"

    def test_tampered_entity_link_changes_fingerprint(self, tmp_path: Path) -> None:
        """Entity link change → fingerprint mismatch."""
        items = self._base_items(tmp_path)
        mutated = copy.deepcopy(items)
        mutated[0]["entities"] = [{"text": "cid_different", "type": "person"}]
        fp = check_full_item_fingerprint(mutated)
        assert not fp.ok, "Tampered entity link must change the fingerprint"

    def test_tampered_tag_changes_fingerprint(self, tmp_path: Path) -> None:
        """Tag addition → fingerprint mismatch."""
        items = self._base_items(tmp_path)
        mutated = copy.deepcopy(items)
        mutated[0]["tags"] = mutated[0].get("tags", []) + ["extra:injected"]
        fp = check_full_item_fingerprint(mutated)
        assert not fp.ok, "Added tag must change the fingerprint"


# ---------------------------------------------------------------------------
# Three-build fingerprint determinism
# ---------------------------------------------------------------------------

class TestFingerprintDeterminism:
    """Three independent corpus builds must produce the same full-item fingerprint."""

    def test_three_builds_identical_fingerprint(self, tmp_path: Path) -> None:
        """Full-item fingerprint is identical across three fresh builds."""
        fps = []
        for i in range(3):
            items = _build_items(tmp_path, f"det_{i}")
            fps.append(check_full_item_fingerprint(items))

        # All must match the frozen constant
        for i, fp in enumerate(fps, 1):
            assert fp.ok, (
                f"Build {i} fingerprint mismatch: {fp.actual[:32]}... ≠ "
                f"{fp.expected[:32]}..."
            )
        # All must be identical to each other
        assert fps[0].actual == fps[1].actual == fps[2].actual, (
            "Full-item fingerprint must be identical across three builds"
        )

    def test_frozen_fingerprint_matches_constant(self, tmp_path: Path) -> None:
        """The committed FROZEN_FULL_ITEM_FP constant matches actual corpus output."""
        items = _build_items(tmp_path, "frozen_check")
        fp = check_full_item_fingerprint(items)
        assert fp.actual == FROZEN_FULL_ITEM_FP, (
            f"Actual full-item fingerprint {fp.actual[:32]}... "
            f"≠ FROZEN_FULL_ITEM_FP {FROZEN_FULL_ITEM_FP[:32]}..."
        )
