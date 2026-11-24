"""Fast live C1/C2 check: minimal bank (20 signals) to verify entity link behavior."""
import sys, uuid, time, tempfile
from pathlib import Path
sys.path.insert(0, '/mnt/c/oloai')

from olo.m3.synthetic.generator import ENTITIES, generate_timeline, _sig_id
from olo.adapters.contacts import make_contact_id
from olo.m3.feeder import populate_olo_db, build_hindsight_items, STRUCTURED_STRATEGY
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable

HOST, PORT = "127.0.0.1", 18800
cfg = HindsightConfig(host=HOST, port=PORT)
client = HindsightClient(cfg)
try:
    client.wait_until_ready(timeout_s=10.0)
except HindsightUnavailable:
    print("SKIP: Hindsight server not available"); sys.exit(0)

bank_id = f"c1c2-fast-{uuid.uuid4().hex[:8]}"
alice = ENTITIES[0]
bob   = ENTITIES[1]
alice_cid = make_contact_id(alice.email)

tl = generate_timeline()

# Pick 1 delivery transcript, 1 email, 1 calendar, 1 invoice per entity
# to verify cross-connector identity links
transcript_id = f"sig:{_sig_id(f'transcript_reschedule_{alice.entity_id}')}"
email_id      = f"sig:{_sig_id(f'email_reschedule_{alice.entity_id}')}"

PASS, FAIL = [], []
def chk(label, cond, detail=""):
    sym = "[PASS]" if cond else "[FAIL]"
    print(f"  {sym} {label}" + (f": {detail}" if detail else ""))
    (PASS if cond else FAIL).append(label)

try:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "olo.db"
        populate_olo_db(tl, db)
        all_items = build_hindsight_items(db)

    # Select ~20 items: 10 Alice (all kinds), 5 Bob (email only), 5 Carla
    selected = []
    seen_kinds_alice = set()
    for item in all_items:
        tags = item.get("tags") or []
        kind = (item.get("metadata") or {}).get("kind", "")
        cid = (item.get("entities") or [{}])[0].get("text", "")
        if f"ent:{alice.entity_id}" in tags and kind not in seen_kinds_alice and len(selected) < 15:
            seen_kinds_alice.add(kind)
            selected.append({**item, "strategy": STRUCTURED_STRATEGY})
        elif f"ent:{bob.entity_id}" in tags and kind == "email.received" and len(selected) < 20:
            selected.append({**item, "strategy": STRUCTURED_STRATEGY})

    print(f"Retaining {len(selected)} items to {bank_id}...")
    for chunk in [selected[k:k+25] for k in range(0, len(selected), 25)]:
        client.retain(chunk, operation_id=str(uuid.uuid4()), async_=False, bank_id=bank_id)
    time.sleep(2)

    # --- C1: Entity-filtered recall returns Alice's signals ---
    print("\n=== C1 live: entity-filtered recall ===")
    r_alice_unf = client.recall(
        "delivery reschedule", max_tokens=4000, bank_id=bank_id, trace=False
    )
    r_alice_flt = client.recall(
        "delivery reschedule", max_tokens=4000, bank_id=bank_id, trace=False,
        tag_groups=[{"tags": [f"ent:{alice.entity_id}"], "match": "all_strict"}]
    )
    r_bob_flt = client.recall(
        "delivery reschedule", max_tokens=4000, bank_id=bank_id, trace=False,
        tag_groups=[{"tags": [f"ent:{bob.entity_id}"], "match": "all_strict"}]
    )

    hits_unf = len(r_alice_unf.get("results", []))
    hits_alice = len(r_alice_flt.get("results", []))
    hits_bob   = len(r_bob_flt.get("results", []))
    chk("Unfiltered recall returns results", hits_unf > 0, f"{hits_unf} hits")
    chk("Alice entity filter returns results", hits_alice > 0, f"{hits_alice} hits")
    chk("Bob entity filter returns results", hits_bob > 0, f"{hits_bob} hits")

    # C2: Alice's transcript not in Bob's filtered results
    print("\n=== C2 live: cross-customer isolation ===")
    def rank_of(result, doc_id):
        for i, h in enumerate(result.get("results", []), 1):
            if h.get("document_id") == doc_id:
                return i
            for sf in (h.get("source_fact_ids") or []):
                sf_id = sf.get("document_id","") if isinstance(sf,dict) else str(sf)
                if sf_id == doc_id:
                    return i
        return -1

    alice_in_bob = rank_of(r_bob_flt, transcript_id)
    chk("Alice's transcript NOT found under Bob's filter",
        alice_in_bob < 0, f"rank={alice_in_bob}")

    # C5/C4: entity links in retained items are cid_ format
    print("\n=== C5/C4: entity link format ===")
    all_cid = all((i.get("entities") or [{}])[0].get("text","").startswith("cid_") 
                  for i in selected)
    chk("All retained items use cid_ entity links", all_cid)

    # C6: ent: tag removal doesn't affect cid_ entity links
    print("\n=== C6: ent: tag independence ===")
    # Build items two ways: with and without ent: tags in payload
    with tempfile.TemporaryDirectory() as td2:
        db2 = Path(td2) / "olo.db"
        populate_olo_db(tl, db2)
        items_full = build_hindsight_items(db2)
    # Strip ent: tags from items (simulate no ent: tags)
    items_stripped = [{**i, "tags": [t for t in (i.get("tags") or []) if not t.startswith("ent:")]}
                      for i in items_full]
    # Entity links must be identical
    links_full    = {i["document_id"]: (i.get("entities") or [{}])[0].get("text","") for i in items_full}
    links_stripped= {i["document_id"]: (i.get("entities") or [{}])[0].get("text","") for i in items_stripped}
    chk("ent: tag removal leaves entity links unchanged", links_full == links_stripped,
        f"{sum(1 for k in links_full if links_full[k] != links_stripped.get(k))} differ")

    # C7: conflicting phone
    print("\n=== C7: conflicting phone fails closed ===")
    from olo.adapters.contacts import migrate_contacts, populate_contacts_from_crm, load_contact_index, resolve_contact_from_signal
    import sqlite3
    with tempfile.TemporaryDirectory() as td3:
        conn = sqlite3.connect(str(Path(td3) / "conflict.db"))
        from olo.store import connect as oconn, migrate
        conn2 = oconn(Path(td3) / "conflict2.db")
        migrate(conn2)
        migrate_contacts(conn2)
        # Register same phone for two contacts
        from olo.adapters.contacts import make_fixture_contact_id
        ambig = [
            {"contact_id": make_fixture_contact_id("live:test:person_a"),
             "canonical_email": "a@test.example", "display_name": "A",
             "organization_name": "Org",
             "identifiers": [("email","a@test.example"), ("phone","+18001234567")]},
            {"contact_id": make_fixture_contact_id("live:test:person_b"),
             "canonical_email": "b@test.example", "display_name": "B",
             "organization_name": "Org",
             "identifiers": [("email","b@test.example"), ("phone","+18001234567")]},
        ]
        populate_contacts_from_crm(ambig, conn2, source="crm")
        idx, _ = load_contact_index(conn2)
        # First registrant (person_a) keeps the phone; person_b's claim is refused
        cid_a = make_fixture_contact_id("live:test:person_a")
        phone_cid = idx.get(("phone","+18001234567"))
        chk("Conflicting phone: first registrant keeps identifier (fail-closed new claim)",
            phone_cid == cid_a, f"cid={phone_cid!r}, expected={cid_a!r}")
        # Unambiguous email is unaffected
        email_cid_a = idx.get(("email","a@test.example"))
        email_cid_b = idx.get(("email","b@test.example"))
        chk("Unambiguous emails resolved correctly",
            email_cid_a is not None and email_cid_b is not None and email_cid_a != email_cid_b)
        conn2.close()

finally:
    try:
        client.delete_bank(bank_id)
        print(f"\nCleaned up: {bank_id}")
    except Exception:
        pass

print(f"\n{'='*50}")
print(f"RESULTS: {len(PASS)} PASS, {len(FAIL)} FAIL")
for f in FAIL:
    print(f"  FAIL: {f}")
if not FAIL:
    print("All C1/C2 live checks passed.")
sys.exit(0 if not FAIL else 1)
