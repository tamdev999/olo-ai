"""olo/m3/benchmark_preflight.py — Pre-flight checks before a benchmark bank build.

Two mandatory checks before every development bank deployment:

1. **Scope-equivalence assertion** (Opus P4):
   For every retained item, verify that its legacy generator ``ent:`` ULID tag,
   the CRM-fixture-resolved canonical ``contact_id``, and the item's Hindsight
   ``entities`` field all agree.  Fails closed on any mismatch, unknown ULID,
   missing CRM mapping, or unresolved identifier conflict.

2. **Full-item fingerprint validation**:
   The SHA-256 fingerprint computed over all retained item fields (context,
   entity links, tags, strategy …) must match the committed
   ``FROZEN_FULL_ITEM_FP`` constant.  A mismatch means the corpus being
   submitted to Hindsight differs from the reproduced baseline.

What this proves (and what it doesn't)
----------------------------------------
- PROVES: retained inputs are identical to the baseline (same contexts, links).
- Does NOT prove: DeepSeek extraction is deterministic across bank builds.
- DOES NOT grant permission to use generator ``ent:`` tags as identity
  authority in production.  Signal payloads' ``ent:`` tags are still ignored
  for identity resolution; they appear here only as a scope-labelling mechanism
  that happens to be equivalent to CRM-resolved ``cid_`` links for the
  synthetic corpus.

No side effects at import time.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Committed full-item fingerprint (C4, eb96aefd22)
# ---------------------------------------------------------------------------

#: SHA-256 of the canonical serialisation of all 388 Hindsight retain-item
#: fields under commit ``11535c3880``.  Covers: document_id, body sha,
#: context string, entity links (sorted text+type), recall tags (sorted),
#: retain strategy and signal kind.
#:
#: This fingerprint changes when contexts or entity links change, but NOT
#: when only body text changes (use FROZEN_CORPUS_FP for that).
#:
#: Recompute after intentional corpus changes::
#:
#:   python scripts/olo/m3/build_corpus.py --db-dir /tmp/fip_check
#:   # then read Full-item fingerprint from output
FROZEN_FULL_ITEM_FP: str = (
    "8bc0a791420ef0c5b53397ebd7ded571f4b5f30584d9bf576ebfe4a51af3a1cf"
)

#: Pre-CM01 fingerprint (historical reference — corpus before email_confirm_orig fix).
FROZEN_FULL_ITEM_FP_PRE_CM01: str = (
    "c675903d1cc31d917a4c2d0e9fde5fbb45d4b93aefbea1dec2b16b85cb08e02a"
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ItemScopeResult:
    """Scope-equivalence result for a single retain item."""
    document_id: str
    ent_ulid: str            # generator ent: ULID extracted from document tags
    expected_cid: str        # CRM-fixture-resolved contact_id for that ULID
    actual_cid: str          # item's entities[0].text
    status: str              # "pass" | "mismatch" | "unknown_ulid" |
                             # "missing_crm" | "no_ent_tag" | "unexpected_link"


@dataclass
class ScopeEquivalenceResult:
    """Aggregate scope-equivalence result over all retain items."""
    total: int = 0
    passed: int = 0
    mismatched: int = 0       # ent: → expected_cid ≠ actual entities
    unknown_ulid: int = 0     # ent: ULID not in generator ENTITIES
    missing_crm: int = 0      # ULID is in ENTITIES but no CRM coverage
    no_ent_tag: int = 0       # item has no ent: tag at all
    unexpected_link: int = 0  # has entity link without ent: tag
    details: list[ItemScopeResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when all checks pass and no blocking anomalies exist."""
        return (
            self.mismatched == 0
            and self.unknown_ulid == 0
            and self.missing_crm == 0
            and self.unexpected_link == 0
        )

    def summary_text(self) -> str:
        lines = [
            f"Scope-equivalence: {self.passed}/{self.total} items pass",
        ]
        if self.mismatched:
            lines.append(f"  FAIL — {self.mismatched} items have wrong entity link")
        if self.unknown_ulid:
            lines.append(f"  FAIL — {self.unknown_ulid} items have unknown ULID in ent: tag")
        if self.missing_crm:
            lines.append(f"  FAIL — {self.missing_crm} items have no CRM coverage")
        if self.unexpected_link:
            lines.append(f"  FAIL — {self.unexpected_link} items have entity link without ent: tag")
        if self.no_ent_tag:
            lines.append(f"  INFO — {self.no_ent_tag} items have no ent: tag (expected empty link)")
        return "\n".join(lines)


@dataclass
class FingerprintResult:
    """Full-item fingerprint validation result."""
    actual: str
    expected: str

    @property
    def ok(self) -> bool:
        return self.actual == self.expected

    def summary_text(self) -> str:
        if self.ok:
            return f"Full-item fingerprint OK: {self.actual[:32]}..."
        return (
            f"FAIL — full-item fingerprint mismatch:\n"
            f"  actual:   {self.actual}\n"
            f"  expected: {self.expected}"
        )


@dataclass
class PreflightResult:
    """Combined result from run_preflight()."""
    scope: ScopeEquivalenceResult
    fingerprint: FingerprintResult

    @property
    def ok(self) -> bool:
        return self.scope.ok and self.fingerprint.ok

    def raise_if_failed(self) -> None:
        """Raise PreflightError with a human-readable message if any check failed."""
        if not self.ok:
            raise PreflightError(
                "Benchmark pre-flight FAILED.\n"
                + self.scope.summary_text() + "\n"
                + self.fingerprint.summary_text()
            )


class PreflightError(Exception):
    """Raised when a benchmark pre-flight check fails."""


# ---------------------------------------------------------------------------
# Scope-equivalence helpers
# ---------------------------------------------------------------------------

def _build_ulid_to_expected_cid(
    entities_list: list,
    crm_contacts: list,
) -> tuple[dict[str, str], set[str]]:
    """Build a mapping: generator ULID → expected CRM contact_id.

    Also returns the set of ULIDs for which a CRM entry exists, so the caller
    can distinguish "ULID in ENTITIES but no CRM coverage" from "unknown ULID".

    Args:
        entities_list: the ``ENTITIES`` list from ``olo.m3.synthetic.generator``.
        crm_contacts:  the ``CRM_CONTACTS`` list from ``olo.m3.synthetic.crm_fixture``.

    Returns:
        (ulid_to_cid, ulids_with_crm_coverage)
        where ulid_to_cid maps ULID → cid_ (or "" when ULID is in ENTITIES
        but the entity's email is not in the CRM fixture).
    """
    # Build email → contact_id from CRM fixture
    email_to_cid: dict[str, str] = {}
    for record in crm_contacts:
        cid = record.get("contact_id", "")
        for id_type, id_value in record.get("identifiers", []):
            if id_type == "email":
                email_to_cid[id_value.strip().lower()] = cid

    ulid_to_cid: dict[str, str] = {}
    ulids_with_coverage: set[str] = set()

    for ent in entities_list:
        ulid = ent.entity_id
        email = ent.email.strip().lower()
        cid = email_to_cid.get(email, "")
        ulid_to_cid[ulid] = cid
        if cid:
            ulids_with_coverage.add(ulid)

    return ulid_to_cid, ulids_with_coverage


def _item_entity_text(item: dict) -> str:
    """Return the first entity link text from a retain item, or ''."""
    entities = item.get("entities") or []
    if not entities:
        return ""
    return entities[0].get("text", "")


# ---------------------------------------------------------------------------
# Main checks
# ---------------------------------------------------------------------------

def check_scope_equivalence(
    items: list[dict],
    entities_list: Optional[list] = None,
    crm_contacts: Optional[list] = None,
) -> ScopeEquivalenceResult:
    """Verify that every item's ent: ULID, CRM contact_id and entities field agree.

    For each item:
    1. Extracts the ``ent:ULID`` tag (from the generator).
    2. Looks up the ULID in ``entities_list`` (ENTITIES) to find the email.
    3. Looks up the email in ``crm_contacts`` (CRM_CONTACTS) to get contact_id.
    4. Checks that ``item["entities"][0]["text"]`` == contact_id.

    Fails closed:
    - Unknown ULID (not in ENTITIES) → ``unknown_ulid`` count incremented.
    - ULID in ENTITIES but no CRM mapping → ``missing_crm`` count incremented.
    - Entity link differs from expected contact_id → ``mismatched`` incremented.
    - Item has entity link but no ent: tag → ``unexpected_link`` incremented.

    Items with no ent: tag AND no entity link are recorded as ``no_ent_tag``
    (informational, not a failure).

    Args:
        items:          list of Hindsight retain items from build_hindsight_items().
        entities_list:  ENTITIES from olo.m3.synthetic.generator.  Loaded from
                        the generator if None.
        crm_contacts:   CRM_CONTACTS from olo.m3.synthetic.crm_fixture.  Loaded
                        from the fixture if None.
    """
    if entities_list is None:
        from olo.m3.synthetic.generator import ENTITIES
        entities_list = ENTITIES
    if crm_contacts is None:
        from olo.m3.synthetic.crm_fixture import CRM_CONTACTS
        crm_contacts = CRM_CONTACTS

    # Build the ULID → expected_cid mapping
    ulid_to_cid, ulids_with_coverage = _build_ulid_to_expected_cid(
        entities_list, crm_contacts
    )
    all_known_ulids: set[str] = {ent.entity_id for ent in entities_list}

    result = ScopeEquivalenceResult(total=len(items))

    for item in items:
        doc_id = item.get("document_id", "?")
        actual_cid = _item_entity_text(item)
        tags = item.get("tags") or []

        # Find ent: tags
        ent_tags = [t[4:] for t in tags if t.startswith("ent:")]

        if not ent_tags:
            # No ent: tag on this item
            if actual_cid:
                # Has entity link but no ent: tag → suspicious
                result.unexpected_link += 1
                result.details.append(ItemScopeResult(
                    document_id=doc_id, ent_ulid="", expected_cid="",
                    actual_cid=actual_cid, status="unexpected_link",
                ))
            else:
                # Both empty — correctly unlinked
                result.no_ent_tag += 1
                result.details.append(ItemScopeResult(
                    document_id=doc_id, ent_ulid="", expected_cid="",
                    actual_cid="", status="no_ent_tag",
                ))
            continue

        # Use the first ent: tag (synthetic corpus has exactly one per item)
        ulid = ent_tags[0]

        if ulid not in all_known_ulids:
            # Completely unknown ULID
            result.unknown_ulid += 1
            result.details.append(ItemScopeResult(
                document_id=doc_id, ent_ulid=ulid, expected_cid="",
                actual_cid=actual_cid, status="unknown_ulid",
            ))
            continue

        expected_cid = ulid_to_cid.get(ulid, "")

        if not expected_cid:
            # ULID is in ENTITIES but no CRM coverage
            result.missing_crm += 1
            result.details.append(ItemScopeResult(
                document_id=doc_id, ent_ulid=ulid, expected_cid="",
                actual_cid=actual_cid, status="missing_crm",
            ))
            continue

        # Both have values — check they agree
        if actual_cid != expected_cid:
            result.mismatched += 1
            result.details.append(ItemScopeResult(
                document_id=doc_id, ent_ulid=ulid,
                expected_cid=expected_cid, actual_cid=actual_cid,
                status="mismatch",
            ))
        else:
            result.passed += 1
            result.details.append(ItemScopeResult(
                document_id=doc_id, ent_ulid=ulid,
                expected_cid=expected_cid, actual_cid=actual_cid,
                status="pass",
            ))

    return result


def check_full_item_fingerprint(
    items: list[dict],
    expected_fp: Optional[str] = None,
) -> FingerprintResult:
    """Compute the full-item fingerprint and compare to the committed constant.

    Args:
        items:       list of Hindsight retain items from build_hindsight_items().
        expected_fp: override the expected fingerprint.  Uses FROZEN_FULL_ITEM_FP
                     when None (the default).
    """
    from olo.m3.feeder import full_item_fingerprint
    actual = full_item_fingerprint(items)
    expected = expected_fp if expected_fp is not None else FROZEN_FULL_ITEM_FP
    return FingerprintResult(actual=actual, expected=expected)


def run_preflight(
    items: list[dict],
    entities_list: Optional[list] = None,
    crm_contacts: Optional[list] = None,
    expected_fp: Optional[str] = None,
) -> PreflightResult:
    """Run all pre-flight checks and return a combined result.

    Does NOT raise on failure; call ``result.raise_if_failed()`` for that.

    Args:
        items:          list of Hindsight retain items from build_hindsight_items().
        entities_list:  ENTITIES from the generator.  Loaded automatically if None.
        crm_contacts:   CRM_CONTACTS from the fixture.  Loaded automatically if None.
        expected_fp:    expected full-item fingerprint.  Uses FROZEN_FULL_ITEM_FP if None.
    """
    scope = check_scope_equivalence(items, entities_list, crm_contacts)
    fingerprint = check_full_item_fingerprint(items, expected_fp)
    return PreflightResult(scope=scope, fingerprint=fingerprint)
