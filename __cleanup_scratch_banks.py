"""Delete all scratch/test banks except main-v1-granite."""
import sys, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError, HindsightUnavailable

cfg = HindsightConfig(host='127.0.0.1', port=18800, bank_id='main-v1-granite')
client = HindsightClient(cfg)

KEEP = {'main-v1-granite', 'main-v1'}

for _attempt in range(10):
    try:
        result = client.list_banks()
        banks = result if isinstance(result, list) else result.get('banks', [])
        break
    except (HindsightUnavailable, Exception):
        print(f"  Waiting for server (attempt {_attempt+1}/10)")
        time.sleep(5)
else:
    print("FAIL: server not responding")
    sys.exit(1)

deleted = []
kept = []

for b in banks:
    bid = b.get('bank_id') or b.get('id') if isinstance(b, dict) else str(b)
    if bid in KEEP:
        kept.append(bid)
        continue
    for _r in range(5):
        try:
            client.delete_bank(bid)
            deleted.append(bid)
            print(f"  Deleted: {bid}")
            time.sleep(2)  # let server settle after each delete
            break
        except HindsightAPIError as e:
            print(f"  Could not delete {bid}: HTTP {e.status_code}")
            break
        except (HindsightUnavailable, Exception) as e:
            if _r < 4:
                print(f"  Retry {_r+1}/5 for {bid}: {e}")
                time.sleep(10)
            else:
                print(f"  GAVE UP on {bid}: {e}")

print(f"\nDeleted {len(deleted)} scratch banks, kept {kept}")
