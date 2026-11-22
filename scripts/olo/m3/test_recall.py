import sys
sys.path.insert(0, '/mnt/c/oloai')
import os
os.environ['HINDSIGHT_HOST'] = '127.0.0.1'
os.environ['HINDSIGHT_PORT'] = '18800'
os.environ['HINDSIGHT_BANK_ID'] = 'main-v1-granite'
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
c.wait_until_ready(30)
# Test simple recall first
result = c.recall("invoice dispute billing", max_tokens=4000, trace=False)
print("Simple recall OK:", len(result.get("results", [])), "results")
# Test with tag_groups
result2 = c.recall("invoice dispute",
    tag_groups=[{"tags": ["ent:ent_01ACME000000000000000000"], "match": "all_strict"}],
    max_tokens=4000, trace=True)
print("Tag recall OK:", len(result2.get("results", [])), "results")
# Test with reranking param
try:
    result3 = c.recall("invoice dispute",
        max_tokens=4000, trace=True,
        reranking={"max_candidates": 400})
    print("Reranking recall OK:", len(result3.get("results", [])), "results")
except Exception as e:
    print(f"Reranking param ERROR: {e}")
