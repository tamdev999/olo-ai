"""Final analysis: LLM stats per bank, missed questions deep-dive, regression analysis."""
import json, sys
from pathlib import Path
sys.path.insert(0, '/mnt/c/oloai')

import urllib.request

REPO = Path("/mnt/c/oloai")

# --- LLM stats for each bank
print("=== LLM Stats per bank ===")
banks = ["dev-bench-01", "dev-bench-02", "dev-bench-03", "main-v1-granite"]
for bid in banks:
    try:
        url = f"http://127.0.0.1:18800/v1/default/banks/{bid}/llm-requests/stats"
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
        total_calls = sum(b.get("total", 0) for b in d.get("buckets", []))
        total_in = sum(b.get("tokens", {}).get("input", 0) for b in d.get("buckets", []))
        total_out = sum(b.get("tokens", {}).get("output", 0) for b in d.get("buckets", []))
        cost_approx = (total_in * 0.14 + total_out * 0.28) / 1_000_000
        url_stats = f"http://127.0.0.1:18800/v1/default/banks/{bid}/stats"
        with urllib.request.urlopen(url_stats, timeout=5) as r2:
            stats = json.loads(r2.read())
        print(f"  {bid}: facts={stats.get('total_nodes')} docs={stats.get('total_documents')} "
              f"obs={stats.get('nodes_by_fact_type', {}).get('observation', 0)} "
              f"calls={total_calls} in_tok={total_in:,} out_tok={total_out:,} cost≈${cost_approx:.3f}")
    except Exception as e:
        print(f"  {bid}: error: {e}")

# --- Per-question miss analysis across all three builds
print("\n=== Consistently failing questions (miss in all 3 builds) ===")
from olo.m3.synthetic.generator import generate_timeline
from olo.m3.benchmark_v2 import build_benchmark_v2
from olo.m3.benchmark_eval_v2_2 import build_v22_labels, score_at_k, INVALID_SPECIFIC, _template

tl = generate_timeline()
qs = build_benchmark_v2(tl)
q_map = {q.id: q for q in qs}
v2_labels = {q.id: {f"sig:{sid}" for sid in q.expected_signal_ids} for q in qs}
v22_labels = {qid: build_v22_labels(qid, v2_labels[qid], tl, q_map) for qid in v2_labels}

result_files = [
    REPO / "docs/olo/m3/dev_bench_01_dev-bench-01.json",
    REPO / "docs/olo/m3/dev_bench_02_dev-bench-02.json",
    REPO / "docs/olo/m3/dev_bench_03_dev-bench-03.json",
]

runs = []
for f in result_files:
    if f.exists():
        runs.append(json.loads(f.read_text()))

# Find questions missing in all 3 runs (v2)
miss_all = {}
for r in runs:
    per_q = r.get("per_question", [])
    for item in per_q:
        qid = item["id"]
        lab = v2_labels.get(qid, set())
        rank = next((h["rank"] for h in item.get("retrieved_top100", []) if h.get("document_id") in lab), -1)
        miss = rank < 0 or rank > 10
        if qid not in miss_all:
            miss_all[qid] = []
        miss_all[qid].append((miss, rank))

# Questions missing in all 3 builds
consistent_misses = {qid: ranks for qid, ranks in miss_all.items()
                     if len(ranks) == 3 and all(m for m, r in ranks)}

print(f"Questions missing in all 3 builds: {len(consistent_misses)}")
by_cat = {}
for qid in consistent_misses:
    q = q_map.get(qid)
    cat = q.category if q else "?"
    by_cat[cat] = by_cat.get(cat, 0) + 1
for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
    print(f"  {cat}: {count}")

# Focus on voice/delivery transcript misses
print("\n=== Voice/delivery transcript miss details ===")
voice_cats = ["delivery_voice", "meeting_voice", "xent_voice", "xent_reschedule",
              "cross_connector_reschedule", "delivery_reschedule", "meeting_reschedule"]
for cat in voice_cats:
    qs_in_cat = [q for q in qs if q.category == cat]
    for q in qs_in_cat:
        ranks = miss_all.get(q.id, [])
        miss_count = sum(1 for m, r in ranks if m)
        rank_str = str([r for m, r in ranks])
        # Check expected signal types
        exp_sigs = [sid for sid in q.expected_signal_ids]
        exp_kinds = []
        for sig in tl:
            if sig.signal_id in exp_sigs:
                exp_kinds.append(f"{sig.kind[:20]}(exists)")
        print(f"  [{cat}] {q.id}: miss={miss_count}/3 ranks={rank_str}")
        print(f"    query: {q.query[:70]}")
        print(f"    expected: {exp_kinds[:3]}")

# --- Comparison with previous bank
print("\n=== Score comparison: previous vs new banks ===")
prev_file = REPO / "docs/olo/m3/benchmark_src_desc_400cands.json"
if prev_file.exists():
    prev = json.loads(prev_file.read_text())
    prev_v2, _ = score_at_k(prev, v2_labels, 10)
    prev_v22, _ = score_at_k(prev, v22_labels, 10)
    n = len(prev.get("per_question", []))
    print(f"  Previous bank (main-v1-granite, pre-fix): v2={prev_v2}/{n}={prev_v2/n:.1%} v2.2={prev_v22}/{n}={prev_v22/n:.1%}")

for i, (run, label) in enumerate(zip(runs, ["dev-bench-01", "dev-bench-02", "dev-bench-03"]), 1):
    n = len(run.get("per_question", []))
    h2, _ = score_at_k(run, v2_labels, 10)
    h22, _ = score_at_k(run, v22_labels, 10)
    print(f"  New bank {i} ({label}): v2={h2}/{n}={h2/n:.1%} v2.2={h22}/{n}={h22/n:.1%}")

# Categories that regressed vs previous bank
print("\n=== Category regressions (previous pass → new fail) ===")
if prev_file.exists():
    prev_cats = {}
    per_q_prev = prev.get("per_question", [])
    for item in per_q_prev:
        qid = item["id"]
        lab = v2_labels.get(qid, set())
        rank = next((h["rank"] for h in item.get("retrieved_top100", []) if h.get("document_id") in lab), -1)
        q = q_map.get(qid)
        cat = q.category if q else "?"
        prev_cats[cat] = prev_cats.get(cat, {"hits": 0, "total": 0})
        prev_cats[cat]["total"] += 1
        if 0 < rank <= 10:
            prev_cats[cat]["hits"] += 1

    # Average new bank per-category
    new_cats = {}
    for run in runs:
        for item in run.get("per_question", []):
            qid = item["id"]
            lab = v2_labels.get(qid, set())
            rank = next((h["rank"] for h in item.get("retrieved_top100", []) if h.get("document_id") in lab), -1)
            q = q_map.get(qid)
            cat = q.category if q else "?"
            if cat not in new_cats:
                new_cats[cat] = {"hits": 0, "total": 0}
            new_cats[cat]["total"] += 1
            if 0 < rank <= 10:
                new_cats[cat]["hits"] += 1

    regressions = []
    for cat in prev_cats:
        old_pct = prev_cats[cat]["hits"] / prev_cats[cat]["total"] if prev_cats[cat]["total"] > 0 else 0
        if cat in new_cats and new_cats[cat]["total"] > 0:
            new_pct = (new_cats[cat]["hits"] / 3) / (new_cats[cat]["total"] / 3)
            if new_pct < old_pct - 0.1:  # >10% regression
                regressions.append((cat, old_pct, new_pct))
    for cat, old_pct, new_pct in sorted(regressions, key=lambda x: x[1] - x[2], reverse=True):
        print(f"  {cat}: {old_pct:.0%} → {new_pct:.0%} (delta={old_pct-new_pct:.0%})")
