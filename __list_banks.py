import os, sys
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
result = c.list_banks()
banks = result if isinstance(result, list) else result.get('banks', [])
for b in banks:
    bid = b.get('bank_id') or b.get('id') if isinstance(b, dict) else str(b)
    obs = b.get('observation_count', '?') if isinstance(b, dict) else '?'
    print(f"  {bid}: {obs} observations")
