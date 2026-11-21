"""Debug P1 history: run the P1 scenario and inspect all history endpoints."""
import sys, uuid, time, hashlib
sys.path.insert(0, '/mnt/c/oloai')
import os
os.environ['HINDSIGHT_HOST'] = '127.0.0.1'
os.environ['HINDSIGHT_PORT'] = '18800'
os.environ['HINDSIGHT_BANK_ID'] = 'main-v1-granite'

from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError
from olo.m3.synthetic.generator import ENTITIES, generate_timeline, to_hindsight_items
from olo.m3.feeder import STRUCTURED_STRATEGY

cfg = HindsightConfig.from_env()
c = HindsightClient(cfg)
ent = ENTITIES[0]

def sig_id(key): return f"sig_{hashlib.sha256(key.encode()).hexdigest()[:24]}"

timeline = generate_timeline(months=12)
items = to_hindsight_items(timeline)
item_map = {i["document_id"]: i for i in items}

confirm_id    = f"sig:{sig_id(f'email_confirm_orig_{ent.entity_id}')}"
reschedule_id = f"sig:{sig_id(f'email_reschedule_{ent.entity_id}')}"
transcript_id = f"sig:{sig_id(f'transcript_reschedule_{ent.entity_id}')}"

confirm_item    = item_map[confirm_id]
reschedule_item = item_map[reschedule_id]
transcript_item = item_map[transcript_id]

bank = f"p1-debug-{uuid.uuid4().hex[:6]}"
print(f"Bank: {bank}")

try:
    # Step 1: retain confirm (April 02 — April 04 is confirmed)
    c.retain([confirm_item], operation_id=str(uuid.uuid4()), async_=False, bank_id=bank)
    r = c.trigger_consolidation(bank_id=bank)
    if op := r.get("operation_id"):
        c.wait_for_operation(op, timeout_s=90, bank_id=bank)
    print("Round 1 done: April 04 confirmed")

    # Step 2: retain reschedule + transcript
    op_r = str(uuid.uuid5(uuid.NAMESPACE_OID, reschedule_id))
    op_t = str(uuid.uuid5(uuid.NAMESPACE_OID, transcript_id))
    c.retain([reschedule_item], operation_id=op_r, async_=True, bank_id=bank)
    c.retain([transcript_item], operation_id=op_t, async_=True, bank_id=bank)
    c.wait_for_operation(op_r, timeout_s=90, bank_id=bank)
    c.wait_for_operation(op_t, timeout_s=90, bank_id=bank)
    r2 = c.trigger_consolidation(bank_id=bank)
    if op := r2.get("operation_id"):
        c.wait_for_operation(op, timeout_s=120, bank_id=bank)
    print("Round 2 done: reschedule + transcript added")

    # Inspect recall results
    res = c.recall(f"meeting {ent.company} April 2026 reschedule", max_tokens=4000,
                   tag_groups=[{"tags": [f"ent:{ent.entity_id}"], "match": "all_strict"}],
                   trace=True, bank_id=bank)
    obs = [r for r in res.get("results", []) if r.get("type") == "observation"]
    print(f"\nObservations: {len(obs)}")
    for o in obs[:5]:
        print(f"  id={o.get('id','')[:16]} text={o.get('text','')[:80]}")

    # Check DOCUMENT history of confirm_id
    print(f"\n--- Document history: {confirm_id} ---")
    try:
        from olo.m3.hindsight.client import _request
        h = c.get_document_history(confirm_id, bank_id=bank)
        hist = h if isinstance(h, list) else h.get("history", h.get("items", []))
        print(f"  entries: {len(hist)}")
        for entry in hist[:3]:
            print(f"  {entry}")
    except HindsightAPIError as e:
        print(f"  Error {e.status_code}: {e}")

    # Check observation history for each observation
    print("\n--- Observation histories ---")
    for o in obs[:5]:
        oid = o.get("id", "")
        if not oid: continue
        try:
            h = _request(cfg, "GET", c._bank_path(f"/memories/{oid}/history", bank))
            hist = h if isinstance(h, list) else h.get("history", h.get("items", []))
            print(f"  obs {oid[:16]}: {len(hist)} history entries")
        except HindsightAPIError as e:
            print(f"  obs {oid[:16]}: Error {e.status_code}")

finally:
    try: c.delete_bank(bank)
    except: pass
    print("\nBank cleaned up")
