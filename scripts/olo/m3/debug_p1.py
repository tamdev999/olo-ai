"""Debug P1: check what reschedule observations exist for Alice Chen."""
import sys
sys.path.insert(0, '/mnt/c/oloai')
import os
os.environ.setdefault('HINDSIGHT_HOST', '127.0.0.1')
os.environ.setdefault('HINDSIGHT_PORT', '18800')
os.environ.setdefault('HINDSIGHT_BANK_ID', 'main-v1-granite')

from olo.m3.hindsight.client import HindsightClient, HindsightConfig
from olo.m3.synthetic.generator import ENTITIES

cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)

# Find the P1 entity
ent = ENTITIES[0]  # Try first entity
print(f"Entity: {ent.entity_id} ({ent.display_name})")

# Recall observations about rescheduling
result = c.recall(
    "delivery rescheduled correction new date April 08",
    tag_groups=[{"tags": [f"ent:{ent.entity_id}"], "match": "all_strict"}],
    max_tokens=4000,
    trace=True,
)
print(f"\nRecall results: {len(result.get('results', []))} items")
for r in result.get('results', [])[:5]:
    doc_id = r.get('document_id', '')
    for sf in r.get('source_facts', []) or r.get('source_fact_ids', []) or []:
        if isinstance(sf, dict) and sf.get('text'):
            print(f"  [{doc_id[:30]}] {sf['text'][:100]}")
    if r.get('content'):
        print(f"  [{doc_id[:30]}] {r['content'][:100]}")

# Try direct recall for reschedule
print("\n--- Rescue search: 'April 08' ---")
result2 = c.recall(
    "April 08 reschedule rescheduled",
    tag_groups=[{"tags": [f"ent:{ent.entity_id}"], "match": "all_strict"}],
    max_tokens=4000,
    trace=True,
)
print(f"Results: {len(result2.get('results', []))}")
for r in result2.get('results', [])[:5]:
    content = r.get('content', '') or ''
    print(f"  {content[:120]}")
