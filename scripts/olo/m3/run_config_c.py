"""Config C benchmark scoring: 400 candidates + include_source_facts.

This runs the same 201-question benchmark as run_live_benchmark.py but
additionally requests source_facts from the server and applies the
_score_hit() scoring from model_benchmark.py:
  1. Deduplication by document_id before ranking
  2. Direct doc_id match
  3. Inline source_fact_ids match
  4. Full source_facts map lookup (the third pass)

This is the scoring methodology used in the original arm_b benchmark
that produced 182/201. The gap versus standard scoring is documented in
docs/olo/m3/m3_diagnostic_report.md.

Usage:
    source /root/olo_linux_venv/bin/activate
    source /root/olo_m3_secrets/deepseek.env
    export PYTHONPATH=/mnt/c/oloai
    export HINDSIGHT_HOST=127.0.0.1
    export HINDSIGHT_PORT=18800
    export HINDSIGHT_BANK_ID=main-v1-granite
    export HINDSIGHT_API_LLM_API_KEY
    python3 scripts/olo/m3/run_config_c.py
"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.synthetic.generator import generate_timeline
from olo.m3.benchmark_v2 import build_benchmark_v2, _compute_v2_hash
from olo.m3.hindsight.client import (
    HindsightClient, HindsightConfig, HindsightAPIError, HindsightUnavailable
)

BANK_ID = os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite")
K_TARGETS = [1, 5, 10, 20, 50, 100]
OUT_DIR = Path("/tmp/benchmark_results")
OUT_DIR.mkdir(exist_ok=True)

FROZEN_HASH = "fbede725aeb89d3557e048803e0891d134c10734ac1ff2c4a7e80f139d28a3be"


def _recall_with_sf(c: HindsightClient, q, retries: int = 2) -> dict:
    tg = []
    if q.entity_id:
        tg.append({"tags": [f"ent:{q.entity_id}"], "match": "all_strict"})
    if q.temporal_ym:
        tg.append({"tags": [q.temporal_ym], "match": "any_strict"})
    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            result = c.recall(
                q.query,
                tag_groups=tg if tg else None,
                max_tokens=16000,  # ensure all 100 results returned
                trace=True,
                include_source_facts=True,  # the Config C key difference
            )
            return {"result": result, "latency_ms": (time.monotonic() - t0) * 1000, "error": None}
        except (HindsightUnavailable, HindsightAPIError) as e:
            if attempt < retries:
                time.sleep(5)
            else:
                return {"result": {}, "latency_ms": -1, "error": str(e)}


def _score_hit_with_sf(recall_result: dict, expected: set[str], top_n: int) -> tuple[bool, int]:
    """Score using source_facts map — matches model_benchmark.py _score_hit."""
    sf_map = recall_result.get("source_facts", {}) or {}
    seen_docs: set[str] = set()
    deduplicated: list = []
    for r in recall_result.get("results", []):
        doc_id = r.get("document_id", "") or ""
        if doc_id and doc_id not in seen_docs:
            seen_docs.add(doc_id)
            deduplicated.append((doc_id, r))
        elif not doc_id:
            deduplicated.append((doc_id, r))

    for rank, (doc_id, r) in enumerate(deduplicated[:top_n], start=1):
        # Pass 1: direct document_id match
        if doc_id in expected:
            return True, rank
        # Pass 2: inline source_fact_ids match
        for sfid in r.get("source_fact_ids", []) or []:
            sd = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
            if sd in expected:
                return True, rank
        # Pass 3: full source_facts map lookup
        for sfid in r.get("source_fact_ids", []) or []:
            fact_key = sfid.get("id", "") if isinstance(sfid, dict) else str(sfid)
            if fact_key in sf_map:
                sf = sf_map[fact_key]
                if isinstance(sf, dict):
                    if sf.get("document_id", "") in expected:
                        return True, rank
    return False, -1


def run_config_c() -> dict:
    tl = generate_timeline()
    questions = build_benchmark_v2(tl)
    actual_hash = _compute_v2_hash()
    if actual_hash != FROZEN_HASH:
        print(f"WARNING: hash mismatch. Expected {FROZEN_HASH[:16]}... got {actual_hash[:16]}...")

    cfg = HindsightConfig.from_env()
    c = HindsightClient(cfg)
    for _ in range(5):
        try:
            c.wait_until_ready(timeout_s=30)
            break
        except HindsightUnavailable:
            time.sleep(10)

    print(f"Running Config C: 400 candidates + include_source_facts on {BANK_ID}...")
    per_q = []
    latencies = []
    errors = 0

    for i, q in enumerate(questions, 1):
        expected = {f"sig:{sid}" for sid in q.expected_signal_ids}
        resp = _recall_with_sf(c, q)
        if resp["error"]:
            errors += 1
            per_q.append({"id": q.id, "category": q.category, "error": resp["error"],
                          "pass_at": {str(k): False for k in K_TARGETS},
                          "pass_at_sf": {str(k): False for k in K_TARGETS}})
            continue

        lat = resp["latency_ms"]
        latencies.append(lat)
        result = resp["result"]

        # Standard scoring (no source_facts)
        def score_std(k):
            hits = result.get("results", [])[:k]
            for h in hits:
                did = h.get("document_id", "") or ""
                if did in expected: return True
                for sf in h.get("source_fact_ids", []) or []:
                    sd = sf.get("document_id", "") if isinstance(sf, dict) else str(sf)
                    if sd in expected: return True
            return False

        # Source-facts scoring
        def score_sf(k):
            hit, _ = _score_hit_with_sf(result, expected, k)
            return hit

        per_q.append({
            "id": q.id, "category": q.category,
            "pass_at": {str(k): score_std(k) for k in K_TARGETS},
            "pass_at_sf": {str(k): score_sf(k) for k in K_TARGETS},
            "latency_ms": round(lat, 1),
            "error": None,
        })
        if i % 20 == 0 or i == len(questions):
            h_std = sum(1 for r in per_q if r["pass_at"].get("10"))
            h_sf = sum(1 for r in per_q if r["pass_at_sf"].get("10"))
            print(f"  [{i}/{len(questions)}] std@10={h_std}/{i}  sf@10={h_sf}/{i}")
        time.sleep(0.05)

    n = len(questions)
    summary = {
        "config": "Config C: 400 candidates + include_source_facts",
        "bank_id": BANK_ID,
        "benchmark_hash": actual_hash,
        "n_questions": n,
        "errors": errors,
        "recall_std": {f"@{k}": sum(1 for r in per_q if r["pass_at"].get(str(k))) for k in K_TARGETS},
        "recall_sf": {f"@{k}": sum(1 for r in per_q if r["pass_at_sf"].get(str(k))) for k in K_TARGETS},
        "latency_ms": {"p50": sorted(latencies)[len(latencies)//2] if latencies else 0,
                       "p95": sorted(latencies)[int(len(latencies)*0.95)] if latencies else 0},
    }

    print("
=== Config C Results ===")
    print("Standard scoring (@k using retrieved results only):")
    for k in K_TARGETS:
        h = summary["recall_std"][f"@{k}"]
        print(f"  @{k}: {h}/{n} = {h/n:.1%}")
    print("Source-facts scoring (@k with source_facts map, dedup):")
    for k in K_TARGETS:
        h = summary["recall_sf"][f"@{k}"]
        print(f"  @{k}: {h}/{n} = {h/n:.1%}")

    out = OUT_DIR / "config_c_results.json"
    json.dump({"summary": summary, "per_question": per_q}, out.open("w"), indent=1)
    print(f"\nSaved: {out}")
    return summary


if __name__ == "__main__":
    run_config_c()
