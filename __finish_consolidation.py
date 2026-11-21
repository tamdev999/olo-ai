"""Run consolidation until quiescent after deploy."""
import sys, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable, HindsightAPIError

BANK = 'main-v1-granite'
cfg = HindsightConfig(host='127.0.0.1', port=18800, bank_id=BANK)
client = HindsightClient(cfg)

for round_num in range(1, 4):
    print(f"Consolidation check round {round_num}…", end='', flush=True)
    for attempt in range(4):
        try:
            result = client.trigger_consolidation(bank_id=BANK)
            break
        except (HindsightUnavailable, HindsightAPIError) as e:
            if attempt < 3:
                print(f" (retry after {e})")
                time.sleep(20)
            else:
                print(f" FAILED: {e}")
                sys.exit(0)
    op_id = result.get('operation_id', '')
    if not op_id:
        print(" nothing to process — done.")
        break
    print(f" op={op_id[:8]}…", end='', flush=True)
    for attempt in range(5):
        try:
            status = client.wait_for_operation(op_id, timeout_s=180.0, bank_id=BANK)
            print(f" {status.get('status', '?')}")
            break
        except (HindsightUnavailable, HindsightAPIError):
            if attempt < 4:
                print(f" (conn reset, retry {attempt+1})", end='', flush=True)
                time.sleep(30)
            else:
                print(" timeout/reset — giving up")
        except TimeoutError:
            print(" timed out — may still be running")
            break

# Final fact count
try:
    banks = client.list_banks().get('banks', [])
    for b in banks:
        if b.get('bank_id') == BANK:
            print(f"\nFinal: {BANK} has {b.get('fact_count', '?')} facts")
except Exception as e:
    print(f"\nCould not get final count: {e}")
