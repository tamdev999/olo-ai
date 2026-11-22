"""Live 201-question Granite benchmark against main-v1-granite bank.

Runs the frozen benchmark_v2 questions against the live Hindsight server
using the actual Granite R2 int8 + hs-rerank-1 + DeepSeek-consolidated bank.
Reports Recall@1/5/10/20/50/100 and per-question results.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, '/mnt/c/oloai')

from olo.m3.benchmark_v2 import build_benchmark_v2, get_benchmark_v2_hash, _compute_v2_hash
from olo.m3.hindsight.client import (
    HindsightClient, HindsightConfig, HindsightAPIError, HindsightUnavailable
)

# ── Config ───────────────────────────────────────────────────────────────────
BANK_ID    = os.environ.get("HINDSIGHT_BANK_ID", "main-v1-granite")
TOP_K_MAX  = 100
K_TARGETS  = [1, 5, 10, 20, 50, 100]
# Historical hash (pre-fix): generator RNG bug caused bank/hash/scorer to use 3 different
# timelines, invalidating 33 question labels (Opus audit §4.1). Label truncations ([:N])
# also reduced label coverage. Both defects are fixed; the hash is now stable.
# OLD_FROZEN_HASH = "e0cbbd985af2ca5a070751b1921f3c6c77d52b2013a3f0d52a40cf6ad98c7592"
# PRE_P1_FIX_HASH = "e9ac10c347caff427e28126bfc336be67d1316145a7f408a2702af7fb6b47ef5"
FROZEN_HASH = "fbede725aeb89d3557e048803e0891d134c10734ac1ff2c4a7e80f139d28a3be"
OUT_DIR     = Path("/tmp/benchmark_results")
OUT_DIR.mkdir(exist_ok=True)

def _connect() -> HindsightClient:
    cfg = HindsightConfig.from_env()
    c = HindsightClient(cfg)
    for attempt in range(5):
        try:
            c.wait_until_ready(timeout_s=30.0)
            return c
        except HindsightUnavailable:
            time.sleep(10)
    raise RuntimeError("Server not ready after 5 attempts")

def _recall_question(c: HindsightClient, q, retries: int = 2) -> dict:
    """Run a single benchmark question against the live bank."""
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
                max_tokens=8000,   # 8000 returns all 100 reranked candidates
                trace=True,
            )
            latency_ms = (time.monotonic() - t0) * 1000
            return {"result": result, "latency_ms": latency_ms, "error": None}
        except HindsightUnavailable as e:
            if attempt < retries and "Connection reset" in str(e):
                time.sleep(5)
            else:
                return {"result": {}, "latency_ms": -1, "error": str(e)}
        except Exception as e:
            return {"result": {}, "latency_ms": -1, "error": str(e)}

def _score_result(result: dict, expected_ids: set[str], k: int) -> bool:
    """True if any expected signal appears in the top-k hits."""
    hits = result.get("results", [])[:k]
    for hit in hits:
        # Direct document ID match
        doc_id = hit.get("document_id", "") or ""
        if doc_id in expected_ids:
            return True
        # Match via source_fact_ids (observations referencing source docs)
        for sfid in hit.get("source_fact_ids", []) or []:
            sid = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
            if sid in expected_ids:
                return True
    return False

def _first_rank(result: dict, expected_ids: set[str]) -> int:
    """Return 1-indexed rank of first correct hit, or -1 if not found in top-100."""
    for rank, hit in enumerate(result.get("results", [])[:100], 1):
        doc_id = hit.get("document_id", "") or ""
        if doc_id in expected_ids:
            return rank
        for sfid in hit.get("source_fact_ids", []) or []:
            sid = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
            if sid in expected_ids:
                return rank
    return -1

def run_benchmark() -> dict:
    # Build questions first so _RNG state is consistent; verify hash after.
    questions = build_benchmark_v2()
    benchmark_hash = _compute_v2_hash()   # always recomputes deterministically
    if benchmark_hash != FROZEN_HASH:
        print(f"WARNING: benchmark hash mismatch — computed {benchmark_hash}")
        print(f"  expected: {FROZEN_HASH}")
        print("  Proceeding, but results may differ from the frozen benchmark.")
    else:
        print(f"Benchmark hash verified: {benchmark_hash[:20]}...")

    c = _connect()
    print(f"Server ready. Bank: {BANK_ID}")

    # Verify model
    pid = None
    try:
        import subprocess
        p = subprocess.run(
            ["pgrep", "-u", "olo_hs", "-a", "hindsight-api"],
            capture_output=True, text=True,
        )
        if "granite" in p.stdout.lower():
            print("Model: Granite confirmed in server process")
        else:
            print("Model: check server env manually")
    except Exception:
        pass

    questions = build_benchmark_v2()
    print(f"Running {len(questions)} questions against {BANK_ID}...")

    per_question = []
    latencies_ms = []
    errors = 0

    for i, q in enumerate(questions, 1):
        expected = {f"sig:{sid}" for sid in q.expected_signal_ids}
        resp = _recall_question(c, q)

        if resp["error"]:
            errors += 1
            per_question.append({
                "id": q.id, "category": q.category,
                "query": q.query, "entity_id": q.entity_id,
                "expected": list(expected),
                "first_rank": -1,
                "pass_at": {str(k): False for k in K_TARGETS},
                "error": resp["error"],
                "latency_ms": -1,
            })
            if i % 20 == 0:
                print(f"  [{i}/{len(questions)}] errors so far: {errors}")
            continue

        result = resp["result"]
        latency = resp["latency_ms"]
        latencies_ms.append(latency)

        first_r = _first_rank(result, expected)
        pass_at = {str(k): _score_result(result, expected, k) for k in K_TARGETS}

        # Retrieve actual result IDs for per-question record
        retrieved = []
        for rank, hit in enumerate(result.get("results", [])[:100], 1):
            doc_id = hit.get("document_id", "") or ""
            retrieved.append({
                "rank": rank,
                "document_id": doc_id,
                "is_expected": doc_id in expected,
                "source_fact_ids": [
                    (sfid.get("document_id","") if isinstance(sfid,dict) else str(sfid))
                    for sfid in (hit.get("source_fact_ids",[]) or [])
                ][:3],
            })

        per_question.append({
            "id": q.id, "category": q.category,
            "query": q.query, "entity_id": q.entity_id,
            "expected": list(expected),
            "first_rank": first_r,
            "pass_at": pass_at,
            "latency_ms": round(latency, 1),
            "retrieved_top100": retrieved,
            "error": None,
        })

        if i % 20 == 0 or i == len(questions):
            so_far_at10 = sum(1 for r in per_question if r["pass_at"].get("10"))
            print(f"  [{i}/{len(questions)}] recall@10 so far: {so_far_at10}/{i} = {so_far_at10/i:.1%}")
            time.sleep(0.5)  # brief pause to keep server healthy
        time.sleep(0.05)  # 50ms between questions

    # Aggregate metrics
    n = len(questions)
    recall = {}
    for k in K_TARGETS:
        hits = sum(1 for r in per_question if r["pass_at"].get(str(k)))
        recall[f"recall_at_{k}"] = {"hits": hits, "total": n, "pct": hits / n}

    lat_ok = sorted(latencies_ms)
    lat_p50 = lat_ok[len(lat_ok)//2] if lat_ok else 0
    lat_p95 = lat_ok[int(len(lat_ok)*0.95)] if lat_ok else 0
    lat_max = max(lat_ok) if lat_ok else 0

    # Category breakdown for recall@10
    cat_results = {}
    for r in per_question:
        cat = r["category"]
        if cat not in cat_results:
            cat_results[cat] = {"hits": 0, "total": 0}
        cat_results[cat]["total"] += 1
        if r["pass_at"].get("10"):
            cat_results[cat]["hits"] += 1
    for cat in cat_results:
        d = cat_results[cat]
        d["pct"] = d["hits"] / d["total"]

    # Failures: questions that miss at recall@10
    failures = [r for r in per_question if not r["pass_at"].get("10")]
    failure_cats = {}
    for f in failures:
        fc = f["category"]
        failure_cats[fc] = failure_cats.get(fc, 0) + 1

    summary = {
        "benchmark_hash": benchmark_hash,
        "bank_id": BANK_ID,
        "n_questions": n,
        "errors": errors,
        "recall": recall,
        "latency_ms": {"p50": round(lat_p50,1), "p95": round(lat_p95,1), "max": round(lat_max,1)},
        "category_recall_at_10": {k: f"{v['hits']}/{v['total']} ({v['pct']:.1%})" for k,v in cat_results.items()},
        "failure_categories": failure_cats,
    }

    # Print summary
    print("\n" + "="*70)
    print("LIVE GRANITE BENCHMARK RESULTS")
    print("="*70)
    print(f"Bank: {BANK_ID}  |  Questions: {n}  |  Errors: {errors}")
    print(f"Hash: {benchmark_hash}")
    print()
    for k in K_TARGETS:
        d = recall[f"recall_at_{k}"]
        bar = "█" * int(d["pct"]*40)
        print(f"  Recall@{k:<4} {d['hits']:3}/{d['total']} = {d['pct']:6.2%}  {bar}")
    print()
    print(f"Latency: p50={lat_p50:.0f}ms  p95={lat_p95:.0f}ms  max={lat_max:.0f}ms")
    print()
    print("Category breakdown (recall@10):")
    for cat, v in sorted(cat_results.items(), key=lambda x: x[1]['pct']):
        bar = "█" * int(v['pct']*20)
        print(f"  {cat:<40} {v['hits']:3}/{v['total']} = {v['pct']:5.1%}  {bar}")
    print()
    print(f"Failures at @10: {len(failures)}")
    print("Top failure categories:", dict(sorted(failure_cats.items(), key=lambda x:-x[1])[:8]))

    # Save results
    out_file = OUT_DIR / "live_benchmark_results.json"
    with open(out_file, "w") as f:
        json.dump({"summary": summary, "per_question": per_question}, f, indent=2)
    print(f"\nFull results saved to: {out_file}")
    return summary

if __name__ == "__main__":
    run_benchmark()
