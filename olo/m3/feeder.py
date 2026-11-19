"""Olo M3 — Production feeder: olo.db → Hindsight retain items.

Reads stored signals and content blobs from olo.db.
Raises loudly on missing or corrupted evidence — never silently drops.
Adds ym:YYYY-MM tag from occurred_at for month-based recall filtering (V1 fix).

This is the authoritative path from Olo evidence to Hindsight memory.
The synthetic generator is NEVER called here; all data comes from olo.db.

Opus architecture: §B.2 (evidence-first), §B.9 (isolation).
Audit correction D11 (fix 10): production feeder, raises on missing blob.
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

        strategy = payload.get("strategy", "structured")
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
        if row["subject"]:
            item["context"] = row["subject"]

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
        strategy = "structured" if not is_llm else "prose"

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


# ── Production retain pipeline with budget guard ──────────────────────────────

def batch_retain_to_hindsight(
    items: list[dict],
    client,  # HindsightClient
    *,
    bank_id: str = "main-v1",
    batch_size: int = 50,
    budget_guard=None,  # Optional[UsageGuard]
    async_: bool = True,
) -> dict[str, int]:
    """Retain evidence items into Hindsight, checking budget between batches.

    This is the production path: olo.db evidence → Hindsight via the HTTP
    retain API.  The budget guard is checked after each batch; if the spend
    since the initial snapshot exceeds the limit, ``BudgetExceededError`` is
    raised and no further batches are submitted.

    Args:
        items:        Retain-ready items from ``build_hindsight_items()``.
        client:       Connected ``HindsightClient``.
        bank_id:      Target Hindsight bank.
        batch_size:   Items per retain call (default 50).
        budget_guard: Optional ``UsageGuard``; if provided, spending is
                      checked after every batch.  Missing or invalid usage
                      statistics fail closed in ``OLO_GATE_STRICT=1`` mode.
        async_:       Submit retains asynchronously (default True).

    Returns:
        {"total": N, "prose": M, "structured": K, "batches": B}

    Raises:
        FeedError:          if ``items`` is empty.
        BudgetExceededError: if spending crosses the budget threshold.
        StatsUnavailableError: if budget_guard is set and stats are unavailable
                              and OLO_GATE_STRICT=1 is set.
    """
    import os
    import uuid as _uuid

    if not items:
        raise FeedError("batch_retain_to_hindsight: items list is empty")

    # Take a budget baseline snapshot before any work
    baseline = None
    if budget_guard is not None:
        from olo.m3.hindsight.budget import StatsUnavailableError
        try:
            baseline = budget_guard.snapshot()
        except StatsUnavailableError as exc:
            if os.environ.get("OLO_GATE_STRICT"):
                raise FeedError(
                    f"Budget guard: stats unavailable before retain — "
                    f"cannot enforce spending limit (fails closed): {exc}"
                ) from exc
            # Non-strict: proceed without budget enforcement
            baseline = None

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

            op_id = str(_uuid.uuid5(
                _uuid.NAMESPACE_OID,
                f"feeder-{label}-{start}-{bank_id}",
            ))
            client.retain(batch, operation_id=op_id, async_=async_, bank_id=bank_id)

            total_sent += len(batch)
            batch_num += 1
            counters["batches"] += 1
            if label == "prose":
                counters["prose"] += len(batch)
            else:
                counters["structured"] += len(batch)
            counters["total"] += len(batch)

            # Check budget after each batch
            if budget_guard is not None and baseline is not None:
                from olo.m3.hindsight.budget import StatsUnavailableError
                try:
                    budget_guard.check(baseline)
                except StatsUnavailableError as exc:
                    if os.environ.get("OLO_GATE_STRICT"):
                        raise FeedError(
                            f"Budget guard: stats unavailable mid-retain "
                            f"(fails closed): {exc}"
                        ) from exc
                    # Non-strict: continue without check

    return counters
