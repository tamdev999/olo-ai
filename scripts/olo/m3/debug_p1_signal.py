"""Find the exact P1 reschedule and confirm signals in the synthetic timeline."""
import sys, hashlib
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.synthetic.generator import ENTITIES, generate_timeline

timeline = generate_timeline(months=12)
ent = ENTITIES[0]
print(f"Entity: {ent.entity_id} {ent.display_name}")

reschedule_id = f"sig_{hashlib.sha256(f'email_reschedule_{ent.entity_id}'.encode()).hexdigest()[:24]}"
confirm_id    = f"sig_{hashlib.sha256(f'email_confirm_orig_{ent.entity_id}'.encode()).hexdigest()[:24]}"

print(f"\nLooking for reschedule: {reschedule_id}")
print(f"Looking for confirm:    {confirm_id}")

r = next((s for s in timeline if s.signal_id == reschedule_id), None)
c = next((s for s in timeline if s.signal_id == confirm_id), None)

if r:
    print(f"\nReschedule occurred: {r.occurred_at.date()}")
    print(f"Body:\n{r.body_text}")
else:
    # Show signals with 'reschedule' in their ID
    similar = [s for s in timeline if 'reschedule' in s.signal_id.lower() and ent.entity_id[:8] in s.tags[0] if s.tags]
    print(f"RESCHEDULE NOT FOUND. Similar signals: {[s.signal_id for s in similar[:3]]}")

if c:
    print(f"\nConfirm occurred: {c.occurred_at.date()}")
    print(f"Body:\n{c.body_text}")
else:
    print("CONFIRM NOT FOUND")

# Also show P1-related signals from the test
print("\nAll entity signals with 'reschedule' or 'confirm' tags:")
ent_sigs = [s for s in timeline if any(ent.entity_id in t for t in s.tags)]
rsch = [s for s in ent_sigs if 'reschedule' in s.signal_id.lower() or 'reschedule' in s.body_text.lower()[:100]]
print(f"Found {len(rsch)} reschedule-related signals")
for s in rsch[:3]:
    print(f"  {s.signal_id}: {s.body_text[:120]}")
