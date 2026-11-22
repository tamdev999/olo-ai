"""Analyze benchmark failures and classify improvement opportunities."""
import sys, json
sys.path.insert(0, '/mnt/c/oloai')

results = json.load(open("/tmp/benchmark_results/live_benchmark_results.json"))
failures = [r for r in results["per_question"] if not r["pass_at"]["10"]]

print(f"Total failures at @10: {len(failures)}")
print()

# Classify failures
classes = {
    "1_missing_from_index": [],   # expected IDs not retrieved even at @100
    "2_ranked_11_to_100": [],     # retrieved but outside top-10
    "3_errors": [],               # recall call errored
}

for f in failures:
    if f.get("error"):
        classes["3_errors"].append(f)
    elif f["first_rank"] > 0:
        classes["2_ranked_11_to_100"].append(f)
    else:
        classes["1_missing_from_index"].append(f)

print("Classification:")
for cls, items in sorted(classes.items()):
    if items:
        print(f"\n  {cls}: {len(items)} questions")
        for q in items[:5]:
            print(f"    [{q['id']}] {q['query'][:70]}")
            print(f"      first_rank={q['first_rank']}  category={q['category']}")

# Categories that consistently fail
print("\nFailing categories detail:")
cat_fails = {}
for f in failures:
    cat = f["category"]
    if cat not in cat_fails:
        cat_fails[cat] = []
    cat_fails[cat].append(f)

for cat, items in sorted(cat_fails.items(), key=lambda x: -len(x[1])):
    if len(items) >= 2:
        print(f"\n  {cat} ({len(items)}/3 fail):")
        for q in items[:3]:
            first_r = q.get("first_rank", -1)
            print(f"    [{q['id']}] rank={first_r} {q['query'][:60]}")
            # Show what was retrieved vs what was expected
            retrieved_ids = set(r["document_id"] for r in q.get("retrieved_top20", [])[:10])
            expected = set(q["expected"])
            overlap = retrieved_ids & expected
            print(f"    expected: {list(expected)[:2]}")
            print(f"    retrieved@10: {list(retrieved_ids)[:3]}")
            print(f"    overlap: {list(overlap)}")
