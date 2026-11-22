"""Investigate why benchmark failures happen — missing docs vs ranking."""
import sys, json, os
sys.path.insert(0, '/mnt/c/oloai')
os.environ['HINDSIGHT_HOST'] = '127.0.0.1'
os.environ['HINDSIGHT_PORT'] = '18800'
os.environ['HINDSIGHT_BANK_ID'] = 'main-v1-granite'

from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError
cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
c.wait_until_ready(30)

results = json.load(open("/tmp/benchmark_results/live_benchmark_results.json"))

# Check missing documents (rank=-1)
missing = [r for r in results["per_question"] if r["first_rank"] == -1 and not r.get("error")]
print(f"=== Missing from index: {len(missing)} questions ===\n")

missing_doc_ids = set()
for f in missing:
    for expected in f["expected"]:
        missing_doc_ids.add(expected)

print(f"Expected document IDs for missing questions:")
for did in sorted(missing_doc_ids)[:15]:
    # Check if document exists in bank
    try:
        doc = c.get_document(did)
        tags = (doc.get("document_metadata") or {}).get("tags", [])
        print(f"  FOUND: {did[:40]} tags={tags[:4]}")
    except HindsightAPIError as e:
        print(f"  MISSING (HTTP {e.status_code}): {did[:40]}")

print()
print("=== Documents ranked just outside @10 (rank 11-20) ===")
just_outside = [r for r in results["per_question"]
                if 10 < r["first_rank"] <= 20 and not r.get("error")]
print(f"Count: {len(just_outside)}")
for f in just_outside[:5]:
    print(f"  rank={f['first_rank']} [{f['id']}] {f['query'][:60]}")
