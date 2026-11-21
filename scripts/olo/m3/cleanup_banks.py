"""Delete Stage B scratch banks and any other non-gate banks."""
import sys, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError, HindsightUnavailable

KEEP = {'main-v1-granite', 'main-v1'}

cfg = HindsightConfig(host='127.0.0.1', port=18800, bank_id='main-v1-granite')
client = HindsightClient(cfg)

for _attempt in range(5):
    try:
        result = client.list_banks()
        banks = result if isinstance(result, list) else result.get('banks', [])
        break
    except (HindsightUnavailable, Exception):
        time.sleep(5)
else:
    print("FAIL: server not responding")
    sys.exit(1)

deleted = []
for b in banks:
    bid = b.get('bank_id') or b.get('id') if isinstance(b, dict) else str(b)
    if bid in KEEP:
        continue
    for _r in range(3):
        try:
            client.delete_bank(bid)
            deleted.append(bid)
            print(f"  Deleted: {bid}")
            time.sleep(2)
            break
        except (HindsightAPIError, HindsightUnavailable) as e:
            if _r < 2:
                time.sleep(10)
            else:
                print(f"  Could not delete {bid}: {e}")

print(f"\nDeleted {len(deleted)} scratch banks")
print(f"Remaining: {[b.get('bank_id') for b in banks if b.get('bank_id') in KEEP]}")
