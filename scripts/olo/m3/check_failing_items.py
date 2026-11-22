"""Check the failing items — what do they look like, why do they rank low?"""
import sys, json, os, hashlib
sys.path.insert(0, '/mnt/c/oloai')
os.environ['HINDSIGHT_HOST'] = '127.0.0.1'
os.environ['HINDSIGHT_PORT'] = '18800'
os.environ['HINDSIGHT_BANK_ID'] = 'main-v1-granite'
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
from olo.m3.synthetic.generator import ENTITIES, generate_timeline

cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
c.wait_until_ready(30)

results = json.load(open("/tmp/benchmark_results/live_benchmark_results.json"))
timeline = generate_timeline(months=12)

# Check calendar event failures
print("=== meeting_calendar failures ===")
cal_fails = [r for r in results["per_question"] if r["category"] == "meeting_calendar"]
for f in cal_fails:
    print(f"  rank={f['first_rank']} [{f['id']}] {f['query']}")
    print(f"  expected: {f['expected'][:2]}")
    # Check what these signals look like
    for sig_full_id in f['expected'][:2]:
        sig_id = sig_full_id[4:]  # strip "sig:"
        try:
            doc = c.get_document(sig_full_id)
            print(f"  doc kind: {doc.get('document_metadata',{}).get('kind','?')}")
            print(f"  tags: {doc.get('tags', [])[:5]}")
            print(f"  retain_context: {doc.get('retain_params',{}).get('context','')}")
            orig = doc.get("original_text", "")[:100]
            print(f"  content: {orig}")
        except Exception as e:
            print(f"  lookup error: {e}")
    print()

# Check invoice dispute failures
print("=== invoice_dispute failures ===")
inv_fails = [r for r in results["per_question"] if r["category"] == "invoice_dispute" and not r["pass_at"]["10"]]
for f in inv_fails[:2]:
    print(f"  rank={f['first_rank']} {f['query'][:70]}")
    for sig_id in f['expected'][:1]:
        try:
            doc = c.get_document(sig_id)
            print(f"  kind: {doc.get('document_metadata',{}).get('kind','?')}")
            print(f"  source: {doc.get('document_metadata',{}).get('source','?')}")
            print(f"  retain_context: {doc.get('retain_params',{}).get('context','')}")
            orig = doc.get("original_text", "")[:150]
            print(f"  content: {orig}")
        except Exception as e:
            print(f"  error: {e}")
    print()
