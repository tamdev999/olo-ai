"""Test how max_tokens affects result count."""
import sys, os
sys.path.insert(0, '/mnt/c/oloai')
os.environ['HINDSIGHT_HOST'] = '127.0.0.1'
os.environ['HINDSIGHT_PORT'] = '18800'
os.environ['HINDSIGHT_BANK_ID'] = 'main-v1-granite'
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
c.wait_until_ready(30)

tg = [{"tags": ["ent:ent_01ACME000000000000000000"], "match": "all_strict"}]
target = "sig:sig_d992bc383e81974213d7d74a"

for max_tok in [4000, 8000, 16000, 32000]:
    r = c.recall("Find the voice call transcript about rescheduling with Acme Corp",
                  tag_groups=tg, max_tokens=max_tok, trace=False)
    results = r.get("results", [])
    ids = [h.get("document_id","") for h in results]
    rank = -1
    for i, doc_id in enumerate(ids, 1):
        if doc_id == target:
            rank = i
            break
    print(f"  max_tokens={max_tok:6}: {len(results)} results returned, target rank={rank}")
