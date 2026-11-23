"""Olo M3 — Production feeder: olo.db → Hindsight retain items.

Reads stored signals and content blobs from olo.db.
Raises loudly on missing or corrupted evidence — never silently drops.
Adds ym:YYYY-MM tag from occurred_at for month-based recall filtering (V1 fix).

This is the authoritative path from Olo evidence to Hindsight memory.
The synthetic generator is NEVER called here; all data comes from olo.db.

Opus architecture: §B.2 (evidence-first), §B.9 (isolation).
Audit correction D11 (fix 10): production feeder, raises on missing blob.
BBC C7: single-bank architecture — structured signals use the named strategy
        "structured-no-llm" (chunks mode, 0 LLM calls) while prose signals use
        the bank default extraction (concise + DeepSeek).  Automatic bank-wide
        consolidation is disabled; prose consolidation is triggered explicitly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class FeedError(Exception):
    """Raised when evidence is missing or corrupted in olo.db."""


# ── Strategy name constants (C7: single-bank architecture) ───────────────────

#: Named strategy for structured signals (invoice, calendar, Stripe, QBO).
#: Maps to Hindsight extraction_mode="chunks" with enable_observations=False:
#: zero LLM calls during extraction, no background consolidation.
STRUCTURED_STRATEGY = "structured-no-llm"

#: Prose signals (email, voice transcript) use the bank default extraction
#: (concise mode = DeepSeek extraction).  Consolidation is triggered explicitly.
PROSE_STRATEGY = "prose"  # falls back to bank default (concise + LLM)

#: Source tags that identify prose signals (used for targeted consolidation).
PROSE_SOURCES: frozenset = frozenset({"src:gmail_owner", "src:voice_fixture"})

#: Strategies that are explicitly registered with prepare_hindsight_bank.
#: Any other non-empty strategy string must be rejected before retain so it
#: cannot silently fall back to default (DeepSeek) extraction.
_KNOWN_STRATEGIES: frozenset = frozenset({STRUCTURED_STRATEGY, PROSE_STRATEGY})

#: Bank config applied once after bank creation (PATCH /config).
#: Sets up the ``structured-no-llm`` named strategy so structured signals get
#: 0 LLM calls and are excluded from observation consolidation.
#: Auto-consolidation remains enabled so prose items are consolidated normally.
_BANK_CONFIG_OVERRIDES: dict = {
    "retain_strategies": {
        STRUCTURED_STRATEGY: {
            "retain_extraction_mode": "chunks",
            "enable_observations": False,
        }
    },
}


def validate_item_strategy(item: dict, bank_id: str) -> None:
    """Raise FeedError if the item uses an unrecognised named strategy.

    An unknown strategy name silently falls back to the bank default (DeepSeek
    extraction) in Hindsight v1.2.1.  This guard makes the failure explicit
    before any evidence is sent.  Empty strategy (bank default) is allowed.
    """
    strategy = item.get("strategy") or ""
    if strategy and strategy not in _KNOWN_STRATEGIES:
        raise FeedError(
            f"Item {item.get('document_id', '?')!r} uses unrecognised strategy "
            f"{strategy!r} (bank={bank_id!r}). "
            f"Known strategies: {sorted(_KNOWN_STRATEGIES)}. "
            "An unknown strategy falls back to default (DeepSeek) extraction "
            "without warning — fix the item before retaining."
        )


# ── Entity metadata enrichment ────────────────────────────────────────────────
# Loaded fresh from the OLO entity_metadata table per build_hindsight_items call.
# No process-global cache: loading globally caused cross-tenant contamination when
# two tenants were processed sequentially in the same process (the first tenant's
# entities leaked into the second tenant's context, and colliding entity IDs
# returned the wrong tenant's display_name/company).
#
# Instead, _load_entity_meta_from_db returns a plain dict keyed by entity_id,
# which build_hindsight_items binds locally.  Each call to build_hindsight_items
# gets a fresh, tenant-scoped snapshot.  The function never imports from
# olo.m3.synthetic — the production path must not depend on test generators.


def _load_entity_meta_from_db(
    db_conn: "sqlite3.Connection",
) -> "dict[str, tuple[str, str]]":
    """Return entity_id → (display_name, company) from the entity_metadata table.

    Returns a plain dict rather than mutating a module global.  Each call is
    independent and tenant-scoped to the connection supplied.

    If the table does not exist or has no rows, returns an empty dict — context
    enrichment simply omits contact/company fields for unknown entities.  This is
    the correct safe fallback for new businesses and partially populated DBs.

    Never raises; never imports from olo.m3.synthetic.
    """
    import sqlite3
    cache: dict[str, tuple[str, str]] = {}
    try:
        rows = db_conn.execute(
            "SELECT entity_id, display_name, company FROM entity_metadata"
        ).fetchall()
        for row in rows:
            entity_id = row[0] if isinstance(row, tuple) else row["entity_id"]
            display_name = (row[1] if isinstance(row, tuple) else row["display_name"]) or ""
            company = (row[2] if isinstance(row, tuple) else row["company"]) or ""
            cache[entity_id] = (display_name, company)
    except (sqlite3.OperationalError, Exception):
        # Table does not exist (old DB, or DB without entity_metadata yet — will
        # be created by migrate()).  Safe to return empty dict; enrichment is
        # best-effort and callers never depend on it being populated.
        pass
    return cache


def _get_entity_meta(
    entity_id: str,
    cache: "dict[str, tuple[str, str]]",
) -> "tuple[str, str]":
    """Return (display_name, company) from the caller-supplied cache dict.

    Returns ("", "") when the entity_id is not found (new business, missing
    metadata, or entity_metadata table not populated).  Never fabricates names.
    """
    return cache.get(entity_id, ("", ""))


def _ym_tag(occurred_at_iso: str) -> str:
    """Return 'ym:YYYY-MM' from an ISO datetime string."""
    try:
        dt = datetime.fromisoformat(occurred_at_iso.replace("Z", "+00:00"))
        return f"ym:{dt.year:04d}-{dt.month:02d}"
    except (ValueError, AttributeError) as exc:
        raise FeedError(f"Cannot parse occurred_at {occurred_at_iso!r}: {exc}") from exc


def build_hindsight_items(
    db_path: Path,
    source: str = "hindsight_gate",
    *,
    raise_on_missing_blob: bool = True,
) -> list[dict]:
    """Build Hindsight retain items from olo.db signals + blobs.

    Args:
        db_path:               Path to the olo.db SQLite file.
        source:                Signal source name used as the ingestion source key.
        raise_on_missing_blob: If True (default), raise FeedError when a blob is
                               absent or its sha256 doesn't match; set False only
                               for unit-testing the error path.

    Returns:
        A list of Hindsight retain-item dicts, one per signal in olo.db.
        Each item includes a ``ym:YYYY-MM`` tag derived from ``occurred_at``.

    Raises:
        FeedError: if db_path doesn't exist, a signal has no content_sha256,
                   a blob is missing, or a blob's hash doesn't match its record.
    """
    if not db_path.exists():
        raise FeedError(f"olo.db not found at {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # Load entity metadata fresh from this connection — no module globals.
    # Each call gets a tenant-scoped snapshot; two tenants processed in the
    # same process cannot contaminate each other's entity context.
    entity_cache = _load_entity_meta_from_db(conn)

    # Read all signals for this source
    rows = conn.execute(
        "SELECT external_id, kind, occurred_at, subject, payload "
        "FROM signals WHERE source = ? ORDER BY rowid",
        (source,),
    ).fetchall()

    if not rows:
        raise FeedError(
            f"No signals found for source={source!r} in {db_path}. "
            "Run the feeder-population step first."
        )

    items: list[dict] = []
    missing_sha: list[str] = []
    missing_blob: list[str] = []
    corrupted: list[str] = []

    for row in rows:
        payload = json.loads(row["payload"])
        sha256 = payload.get("content_sha256", "")
        if not sha256:
            # Real adapter signals (gmail_full, gcal) store the content hash in
            # content_refs, not at the top level.  The synthetic generator sets
            # both; production adapters set only content_refs.
            refs = payload.get("content_refs") or []
            if refs and isinstance(refs[0], dict):
                sha256 = refs[0].get("sha256", "")
        signal_id = row["external_id"]

        if not sha256:
            missing_sha.append(signal_id)
            continue

        # Read the content blob using the official API (handles decompression)
        try:
            from olo.store import get_blob_raw  # type: ignore[import]
            raw = get_blob_raw(source, sha256, conn)
        except ImportError:
            raw = None

        if raw is None:
            if raise_on_missing_blob:
                missing_blob.append(signal_id)
                continue
            else:
                raise FeedError(
                    f"Blob missing for signal {signal_id!r} "
                    f"(sha256={sha256[:12]}…). Evidence is incomplete."
                )

        actual_sha = hashlib.sha256(raw).hexdigest()
        if actual_sha != sha256:
            if raise_on_missing_blob:
                corrupted.append(signal_id)
                continue
            else:
                raise FeedError(
                    f"Blob corruption for signal {signal_id!r}: "
                    f"stored sha256={sha256[:12]}… but computed={actual_sha[:12]}…"
                )

        content = raw.decode("utf-8")

        # Build the month tag for recall filtering
        try:
            ym = _ym_tag(row["occurred_at"])
        except FeedError as exc:
            # Propagate — a malformed timestamp is a data integrity error
            conn.close()
            raise

        # Merge the month tag into the existing tag list
        existing_tags: list[str] = payload.get("tags", [])
        if ym not in existing_tags:
            tags = existing_tags + [ym]
        else:
            tags = existing_tags

        strategy = payload.get("strategy", STRUCTURED_STRATEGY)
        # Translate legacy Olo strategy names to the Hindsight named strategy
        if strategy == "structured":
            strategy = STRUCTURED_STRATEGY
        is_llm_eligible = str(payload.get("is_llm_eligible", "false")).lower() == "true"

        item: dict = {
            "content": content,
            "timestamp": row["occurred_at"],
            "document_id": f"sig:{signal_id}",
            "entities": [{"text": payload.get("entity_id", ""), "type": "person"}],
            "resolve_entities": False,
            "tags": tags,
            "metadata": {
                "signal_id": signal_id,
                "source": payload.get("source", source),
                "kind": payload.get("kind", row["kind"]),
                "content_sha256": sha256,
                "render_version": "1",
                "is_llm_eligible": str(is_llm_eligible),
            },
            "strategy": strategy,
        }

        # ── Context enrichment (Authorized by Opus audit §6 — Change A) ──────
        # Builds a richer context string from: source kind descriptor, contact
        # name, company name, and document subject.  Uses only facts from
        # signal metadata — never from benchmark labels or expected answers.
        #
        # Authorized change A: uniform source descriptor for EVERY signal type.
        # Format: "<Kind descriptor> · <Contact> (<Company>) · <subject>"
        # This allows the reranker to distinguish signal types without relying
        # on body text that may not contain the type name (e.g., calendar events
        # never mention "Calendar" in their body text).
        #
        # Preserves contact, company, direction, document subject and other
        # grounded attributes when present.  Does NOT fabricate metadata.
        entity_id    = payload.get("entity_id", "")
        subject      = row["subject"] or ""
        doc_kind     = payload.get("kind", row["kind"])
        source_tag   = payload.get("source", "")
        if not entity_id:
            # Real adapters (Gmail, Calendar) do not set entity_id in payload.
            # Resolve the primary counterparty using the same mechanism as ingestion,
            # so the feeder and runner always produce consistent entity_ids.
            from olo.adapters.entity_extractor import resolve_primary_entity_id_from_payload
            entity_id = resolve_primary_entity_id_from_payload(doc_kind, payload)
        display_name, company = _get_entity_meta(entity_id, entity_cache)

        # Map signal kinds to human-readable source descriptors
        _KIND_DESCRIPTORS: dict[str, str] = {
            "email.received":           "Email received",
            "email.sent":               "Email sent",
            "call.transcript":          "Phone call transcript",
            "calendar.event.created":   "Calendar event",
            "calendar.event.changed":   "Calendar event",
            "calendar.event.cancelled": "Cancelled calendar event",
            "invoice.created":          "Invoice record",
        }
        # For email.content, use the authoritative direction field from the payload
        # rather than inferring it from authorship.
        if doc_kind == "email.content":
            direction = payload.get("direction", "received")
            kind_descriptor = "Email sent" if direction == "sent" else "Email received"
        # For invoice.created, prefer the specific payment system name
        elif doc_kind == "invoice.created":
            if "stripe" in source_tag.lower():
                kind_descriptor = "Stripe invoice record"
            elif "qbo" in source_tag.lower():
                kind_descriptor = "QuickBooks invoice record"
            else:
                kind_descriptor = "Invoice record"
        else:
            kind_descriptor = _KIND_DESCRIPTORS.get(doc_kind, doc_kind)

        # Build the context parts: [kind_descriptor, identity, subject]
        ctx_parts: list[str] = []

        # 1. Source kind descriptor (for all signals)
        ctx_parts.append(kind_descriptor)

        # 2. Contact and company identity
        if display_name and company:
            ctx_parts.append(f"{display_name} ({company})")
        elif display_name:
            ctx_parts.append(display_name)
        elif company:
            ctx_parts.append(company)

        # 3. Document subject
        if subject:
            ctx_parts.append(subject)

        if ctx_parts:
            item["context"] = " · ".join(ctx_parts)

        items.append(item)

    conn.close()

    # Fail loudly — never silently drop evidence
    if raise_on_missing_blob:
        errors: list[str] = []
        if missing_sha:
            errors.append(
                f"{len(missing_sha)} signals missing content_sha256: "
                f"{missing_sha[:5]}"
            )
        if missing_blob:
            errors.append(
                f"{len(missing_blob)} blobs absent from olo.db: "
                f"{missing_blob[:5]}"
            )
        if corrupted:
            errors.append(
                f"{len(corrupted)} blobs with sha256 mismatch: "
                f"{corrupted[:5]}"
            )
        if errors:
            raise FeedError(
                "Evidence integrity failure — cannot feed Hindsight:\n"
                + "\n".join(f"  • {e}" for e in errors)
            )

    return items


def populate_olo_db(
    timeline,  # list[SyntheticSignal] — only imported from generator when seeding
    db_path: Path,
    source: str = "hindsight_gate",
) -> int:
    """Write a synthetic timeline into olo.db as signals + blobs.

    Returns the number of signals written.
    This is the ONLY place the synthetic generator is consumed;
    ``build_hindsight_items`` reads from olo.db, never from the generator.

    Raises:
        ImportError: if olo.store is not available.
    """
    from olo.store import (  # type: ignore[import]
        connect, migrate, acquire_run, finish_run, ingest_batch,
    )
    from olo.signals import NormalizedSignal, ContentBlob  # type: ignore[import]

    conn = connect(db_path)
    migrate(conn)

    run_id = acquire_run(source, "gate_seed", conn)
    batch: list[NormalizedSignal] = []
    last_cursor: Optional[str] = None
    total = 0

    def _flush(batch: list, cursor: str) -> None:
        nonlocal last_cursor
        ingest_batch(source, run_id, tuple(batch), cursor, last_cursor, conn)
        last_cursor = cursor

    for sig in timeline:
        body_bytes = sig.body_text.encode("utf-8")
        sha256 = hashlib.sha256(body_bytes).hexdigest()
        blob = ContentBlob(
            sha256=sha256,
            media_type="text/plain;charset=utf-8",
            data=body_bytes,
        )

        try:
            ym = _ym_tag(sig.occurred_at.isoformat())
        except FeedError:
            ym = f"ym:{sig.occurred_at.year:04d}-{sig.occurred_at.month:02d}"

        tags_with_ym = list(sig.tags)
        if ym not in tags_with_ym:
            tags_with_ym.append(ym)

        is_llm = sig.is_llm_eligible
        strategy = PROSE_STRATEGY if is_llm else STRUCTURED_STRATEGY

        ns = NormalizedSignal(
            source=source,
            external_id=sig.signal_id,
            kind=sig.kind,
            occurred_at=sig.occurred_at,
            subject=sig.subject,
            payload={
                "content_refs": [{
                    "sha256": sha256,
                    "media_type": "text/plain;charset=utf-8",
                    "size": len(body_bytes),
                    "role": "body",
                }],
                "signal_id": sig.signal_id,
                "source": sig.source,
                "kind": sig.kind,
                "content_sha256": sha256,
                "entity_id": sig.entity_id,
                "tags": tags_with_ym,  # includes ym: tag
                "is_llm_eligible": is_llm,
                "strategy": strategy,
            },
            blobs=(blob,),
        )
        batch.append(ns)
        total += 1

        if len(batch) >= 100:
            _flush(batch, batch[-1].external_id)
            batch = []

    if batch:
        _flush(batch, batch[-1].external_id)

    finish_run(source, run_id, "succeeded", conn)
    conn.close()
    return total


# ── Bank preparation (C7) ────────────────────────────────────────────────────

def prepare_hindsight_bank(
    client,  # HindsightClient
    bank_id: str,
) -> bool:
    """Configure the Hindsight bank for Olo's single-bank architecture.

    Must be called after the bank has been created (first retain) and before
    any structured retains that need 0 LLM calls.

    Applies :data:`_BANK_CONFIG_OVERRIDES` via PATCH:
      - Registers the ``structured-no-llm`` named strategy (chunks mode,
        observations disabled) so structured signals generate 0 LLM calls.
      - Disables bank-wide automatic consolidation so only prose signals are
        consolidated — triggered explicitly after each prose batch.

    Returns True if the PATCH succeeded, False if the bank config API is not
    supported (HTTP 405) — the caller may choose to proceed without
    zero-LLM guarantee.

    Raises FeedError on other HTTP errors.
    """
    from olo.m3.hindsight.client import HindsightAPIError
    try:
        client.patch_bank_config(_BANK_CONFIG_OVERRIDES, bank_id=bank_id)
        return True
    except HindsightAPIError as exc:
        if exc.status_code == 405:
            # Bank config API not enabled on this Hindsight build — proceed
            # without the named strategy (structured items will use the bank
            # default and may incur LLM calls).
            return False
        raise FeedError(
            f"Failed to prepare Hindsight bank {bank_id!r}: {exc}"
        ) from exc


# ── Budget guard sentinel ────────────────────────────────────────────────────
# Used to distinguish "caller did not pass a guard" from "caller explicitly
# passed None to bypass guard enforcement".  Production callers must always
# pass a UsageGuard instance.  Test-only callers that intentionally skip budget
# enforcement must pass budget_guard=None explicitly — the deliberate act of
# passing None serves as documentation that the caller chose to skip enforcement.
_UNGUARDED = object()


# ── Production retain pipeline with budget guard ──────────────────────────────

def batch_retain_to_hindsight(
    items: list[dict],
    client,  # HindsightClient
    *,
    bank_id: str = "main-v1",
    batch_size: int = 50,
    budget_guard=_UNGUARDED,  # UsageGuard | None; None only for explicit test bypass
    async_: bool = True,
    prepare_bank: bool = True,
) -> dict[str, int]:
    """Retain evidence items into Hindsight, checking budget between batches.

    This is the production path: olo.db evidence → Hindsight via the HTTP
    retain API.  The budget guard is checked after each batch; if the spend
    since the initial snapshot exceeds the limit, ``BudgetExceededError`` is
    raised and no further batches are submitted.

    BBC C7 single-bank architecture:
      - Structured items (``strategy="structured-no-llm"``) are retained with
        the named strategy that uses chunks extraction (0 LLM calls).
      - Prose items (``strategy="prose"``) are retained with the bank default
        (concise = DeepSeek extraction).
      - After all prose batches, targeted consolidation is triggered for prose
        entity scopes so observations are created for prose items only.
      - ``prepare_bank=True`` (default): call :func:`prepare_hindsight_bank`
        before the first batch to register the named strategy configuration.
        Set to False when the bank is already prepared (e.g. incremental feeds).

    Args:
        items:        Retain-ready items from ``build_hindsight_items()``.
        client:       Connected ``HindsightClient``.
        bank_id:      Target Hindsight bank.
        batch_size:   Items per retain call (default 50).
        budget_guard: Optional ``UsageGuard``; spending is checked after every
                      batch.  Missing statistics always abort (fails closed).
        async_:       Submit retains asynchronously (default True).
        prepare_bank: If True (default), configure the bank before retaining.

    Returns:
        {"total": N, "prose": M, "structured": K, "batches": B}

    Raises:
        FeedError:          if ``items`` is empty or bank preparation fails.
        BudgetExceededError: if spending crosses the budget threshold.
        StatsUnavailableError: if budget_guard is set and stats are unavailable.
    """
    import os
    import uuid as _uuid

    if budget_guard is _UNGUARDED:
        raise ValueError(
            "batch_retain_to_hindsight: budget_guard is required. "
            "Pass a UsageGuard instance for production use to enforce spending limits. "
            "For tests that intentionally skip budget enforcement, "
            "pass budget_guard=None explicitly."
        )

    if not items:
        raise FeedError("batch_retain_to_hindsight: items list is empty")

    # Reject unknown strategy names before sending anything — they silently
    # fall back to DeepSeek extraction in Hindsight v1.2.1.
    for item in items:
        validate_item_strategy(item, bank_id)

    # Prepare bank config once (registers named strategy, disables auto-consolidation)
    if prepare_bank:
        prepare_hindsight_bank(client, bank_id)

    # Take a budget baseline snapshot before any work — always fail closed
    # (C6 fix: removed OLO_GATE_STRICT guard; production workloads are
    # protected the same way as gate runs).
    baseline = None
    if budget_guard is not None:
        from olo.m3.hindsight.budget import StatsUnavailableError
        try:
            baseline = budget_guard.snapshot()
        except StatsUnavailableError as exc:
            raise FeedError(
                f"Budget guard: stats unavailable before retain — "
                f"cannot enforce spending limit (fails closed): {exc}"
            ) from exc

    prose_items = [i for i in items if i.get("strategy") == "prose"]
    struct_items = [i for i in items if i.get("strategy") != "prose"]

    total_sent = 0
    batch_num = 0
    counters = {"total": 0, "prose": 0, "structured": 0, "batches": 0}

    for label, item_list in [("prose", prose_items), ("structured", struct_items)]:
        for start in range(0, len(item_list), batch_size):
            batch = item_list[start:start + batch_size]
            if not batch:
                continue

            # Content-addressed operation ID: stable across reordering and
            # batch-boundary changes.  A later feed whose batch 0 holds
            # different signals gets a different ID, so Hindsight does not
            # treat it as an already-completed operation (C1 fix).
            batch_content_key = "|".join(
                sorted(
                    f"{item.get('document_id', '')}:"
                    f"{item.get('metadata', {}).get('content_sha256', '')}"
                    for item in batch
                )
            )
            op_id = str(_uuid.uuid5(
                _uuid.NAMESPACE_OID,
                f"{bank_id}|{label}|{batch_content_key}",
            ))
            retain_result = client.retain(
                batch, operation_id=op_id, async_=async_, bank_id=bank_id
            )

            total_sent += len(batch)
            batch_num += 1
            counters["batches"] += 1
            if label == "prose":
                counters["prose"] += len(batch)
            else:
                counters["structured"] += len(batch)
            counters["total"] += len(batch)

            # P0.4 async fix: wait for the operation to complete before checking
            # the budget.  async=True submissions queue work but do not execute
            # LLM extraction immediately; a budget check immediately after
            # client.retain(async_=True) always sees $0 spent because the
            # background worker has not yet run.
            #
            # Only wait when both conditions apply:
            #   1. A budget guard is active (guard=None → no cost tracking needed)
            #   2. The retain was submitted asynchronously
            # For sync retains (async_=False), the server already completed the
            # extraction by the time retain() returns.
            if budget_guard is not None and async_:
                from olo.m3.hindsight.client import HindsightAPIError, HindsightUnavailable
                returned_op_id = (
                    retain_result.get("operation_id") if isinstance(retain_result, dict)
                    else None
                ) or op_id
                try:
                    client.wait_for_operation(
                        returned_op_id,
                        timeout_s=180.0,
                        bank_id=bank_id,
                    )
                except (TimeoutError, HindsightAPIError, HindsightUnavailable) as _we:
                    # Operation wait failed: budget check will still run but
                    # may undercount due to incomplete trace data.  Log and
                    # continue; the budget guard fails closed on unavailable stats.
                    import logging as _log
                    _log.getLogger(__name__).warning(
                        "batch_retain_to_hindsight: wait_for_operation timed out "
                        "or failed (%s); budget check may undercount costs", _we
                    )

            # Check budget after each completed batch — always fail closed on unavailable
            # statistics (C6 fix: removed the OLO_GATE_STRICT guard so production
            # workloads are protected, not just strict-mode gate runs).
            if budget_guard is not None and baseline is not None:
                from olo.m3.hindsight.budget import StatsUnavailableError
                try:
                    budget_guard.check(baseline)
                except StatsUnavailableError as exc:
                    raise FeedError(
                        f"Budget guard: stats unavailable mid-retain "
                        f"(fails closed — missing statistics must never mean "
                        f"unchecked spending): {exc}"
                    ) from exc

    # With auto-consolidation enabled and structured items using the
    # 'structured-no-llm' strategy (enable_observations=False), prose items
    # are consolidated automatically while structured items are excluded.
    # No explicit consolidation trigger needed here.
    return counters


# ── Projection reconciler (M3.0 §C2) ─────────────────────────────────────────

def reconcile_hindsight_projection(
    db_path: Path,
    client,  # HindsightClient
    *,
    bank_id: str,
    source: str = "hindsight_gate",
    batch_size: int = 50,
    budget_guard=_UNGUARDED,  # UsageGuard | None; None only for explicit test bypass
) -> dict[str, int]:
    """Diff olo.db signals against Hindsight documents and re-submit missing ones.

    ``budget_guard`` is required.  Pass a ``UsageGuard`` for production use.
    Pass ``budget_guard=None`` explicitly only in tests that intentionally skip
    budget enforcement.

    Algorithm:
      1. Load all signals from olo.db.
      2. List all documents in the Hindsight bank.
      3. For each signal missing from Hindsight (or with a different
         content_sha256 in the document metadata), re-submit it via
         ``batch_retain_to_hindsight``.
      4. Confirm projection rows for signals whose documents exist and have
         the correct content_sha256.
      5. Fail loudly if any operation ends in ``failed`` status.

    Returns:
        {
            "total": N,     # signals in olo.db
            "confirmed": C, # already or newly confirmed
            "resubmitted": R, # re-retained this run
            "failed": F,    # operations that ended failed
        }

    Raises:
        FeedError: if any operation ends failed or any evidence is missing.
    """
    if budget_guard is _UNGUARDED:
        raise ValueError(
            "reconcile_hindsight_projection: budget_guard is required. "
            "Pass a UsageGuard instance for production use. "
            "For tests that intentionally skip budget enforcement, "
            "pass budget_guard=None explicitly."
        )

    from olo.store import connect as _connect, migrate, record_projection, \
        confirm_projection, fail_projection, list_unconfirmed_projections
    from olo.m3.hindsight.client import HindsightAPIError

    # 1. Load all items from olo.db
    items = build_hindsight_items(db_path, source=source)
    signal_map = {
        item["document_id"]: item
        for item in items
    }

    # 2. Open projection ledger
    conn = _connect(db_path)
    migrate(conn)

    # 3. List current Hindsight documents for this bank
    try:
        all_docs = client.list_all_documents_with_tags([], bank_id=bank_id)
    except HindsightAPIError as exc:
        if exc.status_code == 404:
            all_docs = []  # bank doesn't exist yet — everything is missing
        else:
            conn.close()
            raise FeedError(f"Cannot list Hindsight documents: {exc}") from exc

    # Build a lookup: document_id → content_sha256 in Hindsight
    hindsight_sha: dict[str, str] = {}
    for doc in all_docs:
        doc_id = doc.get("document_id") or doc.get("id", "")
        meta = doc.get("document_metadata") or doc.get("metadata") or {}
        sha = meta.get("content_sha256", "")
        if doc_id:
            hindsight_sha[doc_id] = sha

    # 4. Identify missing or mismatched signals
    missing_items: list[dict] = []
    for doc_id, item in signal_map.items():
        expected_sha = item.get("metadata", {}).get("content_sha256", "")
        if doc_id not in hindsight_sha or hindsight_sha[doc_id] != expected_sha:
            missing_items.append(item)

    # 5. Re-submit missing items
    resubmitted = 0
    if missing_items:
        # Use content-addressed IDs for all re-submissions
        counters = batch_retain_to_hindsight(
            missing_items,
            client,
            bank_id=bank_id,
            batch_size=batch_size,
            budget_guard=budget_guard,
            async_=False,  # synchronous for deterministic reconciliation
        )
        resubmitted = counters["total"]

    # 6. Verify and confirm all signals
    failed_sigs: list[str] = []
    confirmed_count = 0

    # Refresh document list after re-submissions
    try:
        all_docs_after = client.list_all_documents_with_tags([], bank_id=bank_id)
    except HindsightAPIError:
        all_docs_after = all_docs

    hindsight_sha_after: dict[str, str] = {}
    for doc in all_docs_after:
        doc_id = doc.get("document_id") or doc.get("id", "")
        meta = doc.get("document_metadata") or doc.get("metadata") or {}
        sha = meta.get("content_sha256", "")
        if doc_id:
            hindsight_sha_after[doc_id] = sha

    conn.execute("BEGIN IMMEDIATE")
    try:
        for doc_id, item in signal_map.items():
            sig_id = doc_id[4:] if doc_id.startswith("sig:") else doc_id
            expected_sha = item.get("metadata", {}).get("content_sha256", "")
            operation_id = str(
                __import__("uuid").uuid5(
                    __import__("uuid").NAMESPACE_OID,
                    f"reconcile|{bank_id}|{doc_id}|{expected_sha}",
                )
            )
            if (doc_id in hindsight_sha_after and
                    hindsight_sha_after[doc_id] == expected_sha):
                record_projection(sig_id, bank_id, expected_sha, operation_id, conn)
                confirm_projection(sig_id, bank_id, conn)
                confirmed_count += 1
            else:
                fail_projection(sig_id, bank_id, conn)
                failed_sigs.append(sig_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.close()
        raise

    conn.close()

    if failed_sigs:
        raise FeedError(
            f"Reconciliation: {len(failed_sigs)} signals not confirmed in Hindsight "
            f"after re-submission. Missing: {failed_sigs[:5]}"
        )

    return {
        "total": len(signal_map),
        "confirmed": confirmed_count,
        "resubmitted": resubmitted,
        "failed": len(failed_sigs),
    }
