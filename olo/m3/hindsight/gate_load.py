"""Olo M3 — M3.0 GATE: load synthetic timeline into Hindsight and verify.

Usage::

    # With server already running (port from env or --port):
    python gate_load.py --port 8765 --verify

    # Dry run (print items, don't send):
    python gate_load.py --dry-run

Environment:
    HINDSIGHT_PORT        Server port (default 8765)
    HINDSIGHT_API_KEY     Bearer token for the server (optional for local dev)
    HINDSIGHT_BANK_ID     Bank to target (default main-v1)

Opus architecture: §9 (PoC scenarios P1–P10); §B.2 (validation V1–V10).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable
from olo.m3.synthetic.generator import (
    ENTITIES,
    build_labelled_questions,
    generate_timeline,
    to_hindsight_items,
)


def load_timeline(client: HindsightClient, dry_run: bool = False) -> list[dict]:
    """Generate and retain the full 12-month synthetic timeline.

    Returns the list of Hindsight items for further verification.
    """
    print("[load] Generating 12-month synthetic timeline…")
    timeline = generate_timeline(months=12)
    items = to_hindsight_items(timeline)
    print(f"[load] {len(items)} items ({sum(1 for i in items if i['strategy'] == 'prose')} prose, "
          f"{sum(1 for i in items if i['strategy'] == 'structured')} structured)")

    if dry_run:
        print("[load] DRY RUN — not sending to server")
        return items

    # P0.4: Enforce budget guard before the first retain.
    # gate_load.py is used to build production banks with LLM extraction
    # (DeepSeek consolidation).  Budget enforcement and server-side tracing
    # are mandatory.  Tracing records arrive asynchronously; a batch that
    # starts at the limit may overshoot by up to one batch worth of tokens
    # (bounded lag).
    from olo.m3.hindsight.budget import UsageGuard, StatsUnavailableError as _GateSUA
    _gate_guard = UsageGuard(client, bank_id=client._cfg.bank_id, budget_usd=8.0)
    try:
        _gate_baseline = _gate_guard.snapshot()
        print("[load] Budget guard active (tracing confirmed)")
    except _GateSUA as exc:
        raise RuntimeError(
            f"[load] Budget guard: tracing not active on server — cannot enforce "
            f"spending limits.  Set HINDSIGHT_API_LLM_TRACE_ENABLED=true and "
            f"restart before loading.  Error: {exc}"
        ) from exc

    # v1.2.1 constraint: operation_id requires all items in a batch to have
    # the same strategy. Split into prose and structured batches.
    prose_items = [i for i in items if i.get("strategy") == "prose"]
    structured_items = [i for i in items if i.get("strategy") != "prose"]

    BATCH = 50
    total_sent = 0
    batch_num = 0

    for label, item_list in [("prose", prose_items), ("structured", structured_items)]:
        for start in range(0, len(item_list), BATCH):
            batch = item_list[start:start + BATCH]
            op_id = f"olo-gate-{label}-{batch_num:03d}"
            batch_num += 1
            try:
                result = client.retain(batch, operation_id=op_id, async_=True)
            except Exception as e:
                from olo.m3.hindsight.client import HindsightAPIError
                if isinstance(e, HindsightAPIError) and e.response_body:
                    print(f"[load] error detail: {e.response_body.decode()[:300]}")
                raise
            total_sent += len(batch)
            print(f"[load] {label} batch {batch_num:3d}: {len(batch)} items queued")
            # Check budget after each batch (trace records arrive asynchronously)
            try:
                _gate_guard.check(_gate_baseline)
            except _GateSUA as exc:
                raise RuntimeError(
                    f"[load] Budget guard: stats unavailable mid-load "
                    f"(fails closed): {exc}"
                ) from exc

    print(f"[load] Done. {total_sent} items queued ({len(prose_items)} prose, {len(structured_items)} structured).")
    return items


def verify_provenance(client: HindsightClient, items: list[dict]) -> bool:
    """V8: 100% of recall hits carry document_id → signal_id + content_sha256."""
    print("\n[V8] Verifying provenance…")
    sample_queries = [
        ("reschedule meeting Acme", f"ent:{ENTITIES[0].entity_id}"),
        ("invoice dispute Beta Tech", f"ent:{ENTITIES[1].entity_id}"),
        ("delivery change Gamma Retail", f"ent:{ENTITIES[2].entity_id}"),
    ]
    # Build a lookup: document_id → {signal_id, content_sha256}
    provenance_map = {
        item["document_id"]: {
            "signal_id": item["metadata"]["signal_id"],
            "content_sha256": item["metadata"]["content_sha256"],
        }
        for item in items
    }

    total_hits = 0
    provenance_ok = 0
    for query, tag in sample_queries:
        result = client.recall(query, tags=[tag], max_tokens=2000, trace=True)
        for hit in result.get("results", []):
            doc_id = hit.get("document_id", "")
            if not doc_id:
                continue
            total_hits += 1
            if doc_id in provenance_map:
                provenance_ok += 1

    if total_hits == 0:
        print("[V8] No recall hits — server may still be processing. Try again after 30s.")
        return False

    pct = provenance_ok / total_hits * 100
    status = "PASS" if pct == 100 else "FAIL"
    print(f"[V8] {status}: {provenance_ok}/{total_hits} hits have provenance ({pct:.0f}%)")
    return pct == 100


def verify_cross_connector(client: HindsightClient) -> bool:
    """V2: entity-tag query returns documents from every connector."""
    print("\n[V2] Verifying cross-connector identity…")
    expected = {"gmail_owner", "stripe_fixture", "qbo_fixture", "voice_fixture"}
    all_pass = True

    for ent in ENTITIES:
        result = client.recall(
            ent.display_name,
            tags=[f"ent:{ent.entity_id}"],
            max_tokens=8000,
            trace=True,
        )
        found: set[str] = set()
        for hit in result.get("results", []):
            meta = hit.get("metadata") or {}
            src = meta.get("source", "")
            if src:
                found.add(src)
        missing = expected - found
        if missing:
            print(f"[V2] FAIL {ent.company}: missing connectors {missing}")
            all_pass = False
        else:
            print(f"[V2] PASS {ent.company}: all {len(expected)} connectors found")
    return all_pass


def spot_check_llm_extraction(client: HindsightClient) -> None:
    """P1/P2 spot check: query for the PoC scenarios and print results."""
    print("\n[P1] Spot check: delivery reschedule (Acme Corp)…")
    result = client.recall(
        f"reschedule meeting {ENTITIES[0].company}",
        tags=[f"ent:{ENTITIES[0].entity_id}"],
        max_tokens=4000,
        trace=False,
    )
    hits = result.get("results", [])
    print(f"  {len(hits)} result(s) for reschedule query")
    for hit in hits[:3]:
        text = hit.get("text", "")
        typ = hit.get("type", "?")
        doc = hit.get("document_id", "")
        print(f"  [{typ}] {text[:100]} (doc={doc})")

    print("\n[P2] Spot check: invoice dispute (Beta Tech)…")
    result2 = client.recall(
        "invoice dispute billing discrepancy",
        tags=[f"ent:{ENTITIES[1].entity_id}"],
        max_tokens=4000,
        trace=False,
    )
    hits2 = result2.get("results", [])
    print(f"  {len(hits2)} result(s) for invoice dispute query")
    for hit in hits2[:3]:
        text = hit.get("text", "")
        typ = hit.get("type", "?")
        print(f"  [{typ}] {text[:100]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="M3.0 GATE: load + verify")
    parser.add_argument("--port", type=int, default=int(os.environ.get("HINDSIGHT_PORT", "8765")))
    parser.add_argument("--bank-id", default=os.environ.get("HINDSIGHT_BANK_ID", "main-v1"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="Run V2+V8 verification after load")
    parser.add_argument("--spot-check", action="store_true", help="Spot-check P1/P2 LLM extraction")
    parser.add_argument("--wait", type=int, default=0, help="Wait N seconds for processing before verify")
    args = parser.parse_args()

    cfg = HindsightConfig(
        host="127.0.0.1",
        port=args.port,
        api_key=os.environ.get("HINDSIGHT_API_KEY", ""),
        bank_id=args.bank_id,
    )
    client = HindsightClient(cfg)

    if not args.dry_run:
        try:
            client.wait_until_ready(timeout_s=10.0)
            health = client.health_check()
            print(f"[gate] Server healthy: {health.get('status', 'ok')}")
        except HindsightUnavailable as e:
            print(f"[gate] ERROR: {e}", file=sys.stderr)
            print("[gate] Start the server first: hindsight-api (in the Hindsight venv)")
            return 1

    items = load_timeline(client, dry_run=args.dry_run)

    if args.dry_run:
        print(f"[gate] Dry run complete. Would load {len(items)} items.")
        return 0

    if args.wait:
        print(f"[gate] Waiting {args.wait}s for async processing…")
        time.sleep(args.wait)

    failures = 0
    if args.verify:
        if not verify_provenance(client, items):
            failures += 1
        if not verify_cross_connector(client):
            failures += 1

    if args.spot_check:
        spot_check_llm_extraction(client)

    if failures:
        print(f"\n[gate] {failures} verification(s) FAILED")
        return 1
    elif args.verify:
        print("\n[gate] All verifications PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
