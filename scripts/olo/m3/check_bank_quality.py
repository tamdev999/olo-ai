"""Check bank quality for V1 recall and P4 missing document."""
import sys
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError
from olo.m3.synthetic.generator import generate_timeline, build_labelled_questions

cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)

# Bank stats
try:
    stats = c.get_bank_stats()
    print("Bank:", stats)
except Exception as e:
    print("Stats error:", e)

# Total documents
all_docs = c.list_documents(limit=10000)
docs = all_docs.get("items", [])
print(f"Total documents: {len(docs)}")

# Check missing P4 doc
p4_missing = "sig:sig_0a06499cd6677a99ccbfbd04"
try:
    doc = c.get_document(p4_missing)
    print(f"P4 doc present: {p4_missing}")
except HindsightAPIError as e:
    print(f"P4 doc MISSING (HTTP {e.status_code}): {p4_missing}")

# Check how many benchmark signal IDs are in the bank
doc_ids = {d.get("document_id", "") or d.get("id", "") for d in docs}
questions = build_labelled_questions()
in_bank = 0
out_bank = 0
missing_ids = []
for q in questions:
    for sig_id in q.get("expected_signal_ids", []):
        full_id = f"sig:{sig_id}"
        if full_id in doc_ids:
            in_bank += 1
        else:
            out_bank += 1
            missing_ids.append(sig_id)

print(f"\nV1 expected signals in bank: {in_bank}")
print(f"V1 expected signals MISSING from bank: {out_bank}")
if missing_ids:
    print("Sample missing:", missing_ids[:5])
