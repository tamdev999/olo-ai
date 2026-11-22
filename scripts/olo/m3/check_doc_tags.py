import sys, os
sys.path.insert(0, '/mnt/c/oloai')
os.environ['HINDSIGHT_HOST'] = '127.0.0.1'
os.environ['HINDSIGHT_PORT'] = '18800'
os.environ['HINDSIGHT_BANK_ID'] = 'main-v1-granite'
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
c.wait_until_ready(30)

# Check a "missing" document
doc_id = "sig:sig_d992bc383e81974213d7d74a"  # voice transcript Apr 03
doc = c.get_document(doc_id)
print("Full document structure:")
import json
print(json.dumps(doc, indent=2)[:2000])

# Also check list_documents result
docs = c.list_all_documents_with_tags(["ent:ent_01ACME000000000000000000"])
print(f"\nDocuments with ACME tag: {len(docs)}")
# Check if our target doc is in there
found = any(d.get("document_id") == doc_id or d.get("id") == doc_id for d in docs)
print(f"Target doc in entity list: {found}")
