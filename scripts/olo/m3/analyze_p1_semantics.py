"""Analyze P1 signal content to determine confirmed vs. proposed date change."""
import sys, hashlib
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.synthetic.generator import ENTITIES, generate_timeline

timeline = generate_timeline(months=12)
ent = ENTITIES[0]

# IDs used by the P1 test
def sig_id(key: str) -> str:
    return f"sig_{hashlib.sha256(key.encode()).hexdigest()[:24]}"

ids = {
    'confirm_orig':  sig_id(f'email_confirm_orig_{ent.entity_id}'),
    'reschedule':    sig_id(f'email_reschedule_{ent.entity_id}'),
    'transcript':    sig_id(f'transcript_reschedule_{ent.entity_id}'),
}

print(f"Entity: {ent.entity_id} ({ent.display_name} @ {ent.company})\n")

for name, sid in ids.items():
    s = next((x for x in timeline if x.signal_id == sid), None)
    if s:
        print(f"=== {name} ({sid}) ===")
        print(f"  kind:       {s.kind}")
        print(f"  occurred:   {s.occurred_at.date()} (day {s.occurred_at.timetuple().tm_yday})")
        print(f"  source:     {s.source}")
        print(f"  is_prose:   {s.is_llm_eligible}")
        print(f"  body:\n{s.body_text}\n")
    else:
        print(f"=== {name} ({sid}) NOT FOUND in timeline ===\n")

print("\n=== Semantic analysis ===")
confirm_body = next((x.body_text for x in timeline if x.signal_id == ids['confirm_orig']), '')
reschedule_body = next((x.body_text for x in timeline if x.signal_id == ids['reschedule']), '')
transcript_body = next((x.body_text for x in timeline if x.signal_id == ids['transcript']), '')

# Timeline ordering
confirm_sig = next((x for x in timeline if x.signal_id == ids['confirm_orig']), None)
reschedule_sig = next((x for x in timeline if x.signal_id == ids['reschedule']), None)
transcript_sig = next((x for x in timeline if x.signal_id == ids['transcript']), None)

if confirm_sig and reschedule_sig:
    print(f"confirm_orig  date: {confirm_sig.occurred_at.date()}")
    print(f"reschedule    date: {reschedule_sig.occurred_at.date()}")
    if transcript_sig:
        print(f"transcript    date: {transcript_sig.occurred_at.date()}")
    
    if reschedule_sig.occurred_at < confirm_sig.occurred_at:
        print("\nTIMELINE: reschedule request PRECEDES confirmation")
        print("SEMANTICS: The confirm email (later) OVERRIDES the reschedule request")
        print("           → April 04 is the CONFIRMED date")
        print("           → April 08 is a PROPOSED date that was NOT accepted")
    else:
        print("\nTIMELINE: reschedule request FOLLOWS confirmation")
        print("SEMANTICS: The reschedule request (later) may supersede the confirmation")
        print("           → Depends on whether the request was accepted")
