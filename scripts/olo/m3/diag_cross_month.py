#!/usr/bin/env python3
"""Cross-month memory linking diagnostic.

Creates a fresh disposable bank, retains a March confirmation email and an
April reschedule transcript for the same customer, and verifies that the
April consolidation correctly finds and updates the March observation.

Checks:
  CM1  After April consolidation, entity-only recall returns updated delivery info.
  CM2  Month-April filtered recall also returns updated delivery info.
  CM3  Month-March filtered recall returns the original March observation.
  CM4  Source provenance: the updated observation (or its history) links to the
       March document.

Usage:
  . /root/olo_m3_secrets/deepseek.env && export HINDSIGHT_API_LLM_API_KEY
  export PYTHONPATH='/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai'
  cd /mnt/c/oloai && . /root/olo_linux_venv/bin/activate
  python scripts/olo/m3/diag_cross_month.py 2>&1 | tee /tmp/diag_cross_month.log
"""
from __future__ import annotations
import hashlib, os, sys, time, uuid as _uuid
from datetime import datetime, timezone
sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable, _request
from olo.m3.synthetic.generator import ENTITIES, _sig_id

PORT    = 18800
API_KEY = os.environ.get("HINDSIGHT_API_LLM_API_KEY", "")
ENT     = ENTITIES[0]  # Alice Chen / Acme Corp
BANK    = f"diag-xmonth-{_uuid.uuid4().hex[:6]}"

# Canonical CID for Alice Chen
ALICE_CID = "cid_64c1dd94041b33ab9245"
CID_TAG   = f"ent:{ALICE_CID}"

SEP = "─" * 68
def _p(s): print(f"\n{SEP}\n{s}\n{SEP}")

def _item(doc_id, body, occurred_at, subject, kind, source, extra_tags=None):
    sha = hashlib.sha256(body.encode()).hexdigest()
    ym  = f"ym:{occurred_at[:7]}"  # ym:YYYY-MM
    tags = [CID_TAG, ym, f"kind:{kind}", f"src:{source}", f"dc:{'voice' if 'transcript' in kind else 'gmail_restricted'}"]
    if extra_tags:
        tags += extra_tags
    return {
        "content":   body,
        "timestamp": occurred_at,
        "document_id": doc_id,
        "entities": [{"text": ALICE_CID, "type": "person"}],
        "resolve_entities": False,
        "tags": tags,
        "strategy": "prose",
        "metadata": {"content_sha256": sha, "source": source, "kind": kind},
        "observation_scopes": [[CID_TAG, "ym:*"], [CID_TAG]],
    }

def _consolidate(client, label):
    r = client.trigger_consolidation(bank_id=BANK)
    op = r.get("operation_id", "")
    if op:
        client.wait_for_operation(op, timeout_s=120, bank_id=BANK)
    print(f"  consolidation {label}: done")

def _count_obs(r):
    return [(x.get("id","")[:8], x.get("text","")[:100])
            for x in (r.get("results") or []) if x.get("type") == "observation"]

def main():
    cfg = HindsightConfig(host="127.0.0.1", port=PORT, api_key=API_KEY, bank_id=BANK)
    client = HindsightClient(cfg)
    try:
        client.wait_until_ready(timeout_s=10)
    except HindsightUnavailable as e:
        sys.exit(f"Server not available: {e}")

    # Document IDs for our two cross-month items
    MARCH_CONF_ID  = "sig:march_delivery_conf"
    APRIL_TRANS_ID = "sig:april_delivery_trans"

    # March item: email confirming delivery for Saturday March 15, 2026
    march_body = (
        "Hi Alice Chen, confirming your delivery is scheduled for Saturday March 15, 2026. "
        "Your order ORD-ACME-MARCH-01 will be delivered between 9am and noon. "
        "Let us know if you need to make any changes. — Owner"
    )
    march_item = _item(
        doc_id=MARCH_CONF_ID,
        body=march_body,
        occurred_at="2026-03-10T10:00:00+00:00",
        subject="Delivery confirmation for March 15",
        kind="email.sent",
        source="gmail_owner",
    )

    # April item: transcript confirming the delivery was rescheduled from March 15 to April 20
    april_body = (
        "Agent: Hi, this is Owner at Olo Business. How can I help you? "
        "Customer: Hi, I'm calling about my delivery scheduled for Saturday March 15. "
        "I need to change it to Monday April 20 if possible. "
        "Agent: Absolutely, I see your order ORD-ACME-MARCH-01. "
        "I can confirm the delivery is now rescheduled for Monday April 20, 2026. "
        "Customer: Perfect, thank you. "
        "Agent: Got it. All confirmed. Have a great day."
    )
    april_item = _item(
        doc_id=APRIL_TRANS_ID,
        body=april_body,
        occurred_at="2026-04-05T14:00:00+00:00",
        subject="Call: delivery reschedule",
        kind="call.transcript",
        source="voice_fixture",
    )

    _p("ITEM DETAILS")
    for label, item in [("MARCH", march_item), ("APRIL", april_item)]:
        print(f"  [{label}] ym={[t for t in item['tags'] if t.startswith('ym:')]}")
        print(f"           ent={[t for t in item['tags'] if t.startswith('ent:')]}")
        print(f"           obs_scopes={item['observation_scopes']}")
        print(f"           content={item['content'][:80]!r}")

    _p("PHASE 1: retain March confirmation → consolidate")
    op1 = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"march-{BANK}"))
    client.retain([march_item], operation_id=op1, async_=False, bank_id=BANK)
    _consolidate(client, "march")

    _p("PHASE 1 RECALL: entity-only")
    r_p1 = client.recall(
        "Alice Chen delivery scheduled",
        tag_groups=[{"tags": [CID_TAG], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_p1 = _count_obs(r_p1)
    print(f"  observations found: {len(obs_p1)}")
    for oid, otext in obs_p1:
        print(f"    [{oid}] {otext!r}")

    _p("PHASE 2: retain April transcript → consolidate")
    op2 = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"april-{BANK}"))
    client.retain([april_item], operation_id=op2, async_=False, bank_id=BANK)
    _consolidate(client, "april-cross-month")

    _p("CM1: entity-only recall after cross-month update")
    r_ent = client.recall(
        "Alice Chen delivery rescheduled",
        tag_groups=[{"tags": [CID_TAG], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_ent = _count_obs(r_ent)
    print(f"  observations found: {len(obs_ent)}")
    april20_in_ent = False
    for oid, otext in obs_ent:
        print(f"    [{oid}] {otext!r}")
        if "april 20" in otext.lower() or "april 20" in otext.lower() or "2026-04-20" in otext:
            april20_in_ent = True
    cm1_pass = april20_in_ent
    print(f"  CM1: {'PASS' if cm1_pass else 'FAIL'} — April 20 {'found' if cm1_pass else 'NOT found'} in entity-only recall")

    _p("CM2: month-April filtered recall")
    r_apr = client.recall(
        "Alice Chen delivery",
        tag_groups=[{"tags": [CID_TAG, "ym:2026-04"], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_apr = _count_obs(r_apr)
    print(f"  observations found: {len(obs_apr)}")
    april20_in_apr = False
    for oid, otext in obs_apr:
        print(f"    [{oid}] {otext!r}")
        if "april 20" in otext.lower() or "2026-04-20" in otext:
            april20_in_apr = True
    cm2_pass = april20_in_apr
    print(f"  CM2: {'PASS' if cm2_pass else 'FAIL'} — April 20 {'found' if cm2_pass else 'NOT found'} in April-filtered recall")

    _p("CM3: month-March filtered recall")
    r_mar = client.recall(
        "Alice Chen delivery",
        tag_groups=[{"tags": [CID_TAG, "ym:2026-03"], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_mar = _count_obs(r_mar)
    print(f"  observations found: {len(obs_mar)}")
    march_obs_found = len(obs_mar) > 0
    for oid, otext in obs_mar:
        print(f"    [{oid}] {otext!r}")
    cm3_note = "march obs present" if march_obs_found else "no march-scoped obs (expected if single-pass cid used)"
    print(f"  CM3: INFO — {cm3_note}")

    _p("CM4: source provenance — does updated obs link back to March doc?")
    sf = r_ent.get("source_facts", {}) or {}
    for r in (r_ent.get("results") or []):
        if r.get("type") == "world":
            fid = r.get("id",""); did = r.get("document_id","")
            if fid and did and fid not in sf:
                sf[fid] = {"document_id": did}

    march_in_sources = False
    for r in (r_ent.get("results") or []):
        if r.get("type") != "observation":
            continue
        docs = set()
        for sfid in (r.get("source_fact_ids") or []):
            direct   = sfid.get("document_id","") if isinstance(sfid,dict) else str(sfid)
            fact_key = sfid.get("id","") if isinstance(sfid,dict) else str(sfid)
            if direct and not direct.startswith("{"): docs.add(direct)
            if fact_key in sf and isinstance(sf[fact_key],dict):
                d = sf[fact_key].get("document_id","")
                if d: docs.add(d)
        if MARCH_CONF_ID in docs:
            march_in_sources = True
            print(f"  March confirmation ({MARCH_CONF_ID!r}) IS in observation sources — provenance linked ✓")
        if APRIL_TRANS_ID in docs:
            print(f"  April transcript ({APRIL_TRANS_ID!r}) IS in observation sources ✓")
    if not march_in_sources:
        print("  March confirmation NOT in observation sources (obs created from April pass only)")
    cm4_note = "PASS — sources linked" if march_in_sources else "INFO — March not in obs sources; check ob history"

    _p("VERDICT")
    print(f"  CM1 entity-only finds April info:         {'PASS' if cm1_pass else 'FAIL'}")
    print(f"  CM2 month-April finds April info:         {'PASS' if cm2_pass else 'FAIL'}")
    print(f"  CM3 month-March recall:                   {cm3_note}")
    print(f"  CM4 source provenance:                    {cm4_note}")
    overall = cm1_pass and cm2_pass
    print(f"\n  Cross-month: {'PASS' if overall else 'FAIL — April info not visible after cross-month update'}")
    if not cm1_pass:
        print("  FIX NEEDED: entity-only recall does not show April rescheduling.")
        print("  Check observation_scopes pass 2 is [[cid]] and all_strict finds cross-month obs.")
    if not cm2_pass:
        print("  FIX NEEDED: month-April recall does not show updated obs.")
        print("  Check observation_scopes pass 1 is [[cid, ym:*]] — ym: not in obs tags.")

    _p("CLEANUP")
    for _ in range(3):
        try:
            client.delete_bank(BANK); print(f"  bank {BANK!r} deleted"); break
        except Exception as exc:
            print(f"  {exc}"); time.sleep(2)

if __name__ == "__main__":
    main()
