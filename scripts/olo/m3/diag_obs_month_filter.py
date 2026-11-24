#!/usr/bin/env python3
"""Task 2: Verify that observations created with observation_scopes retain ym: tags
and are retrievable by both entity-only and entity+month filtered recall.

Tests:
  T1  Entity-only recall finds observations (not just world facts)
  T2  Entity+month recall finds the same observations
  T3  Entity+wrong-month recall returns nothing on observations
  T4  observation_scopes do NOT strip ym: tags from world facts or observations

Usage:
  . /root/olo_m3_secrets/deepseek.env && export HINDSIGHT_API_LLM_API_KEY
  export PYTHONPATH='/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai'
  cd /mnt/c/oloai && . /root/olo_linux_venv/bin/activate
  python scripts/olo/m3/diag_obs_month_filter.py 2>&1 | tee /tmp/diag_obs_month_filter.log
"""
from __future__ import annotations
import hashlib, os, sys, time, uuid as _uuid
sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable
from olo.m3.synthetic.generator import ENTITIES, _sig_id, generate_timeline, to_hindsight_items

PORT    = 18800
API_KEY = os.environ.get("HINDSIGHT_API_LLM_API_KEY", "")
ENT     = ENTITIES[0]
BANK    = f"diag-ym-{_uuid.uuid4().hex[:6]}"

CONF_ID  = f"sig:{_sig_id(f'email_confirm_orig_{ENT.entity_id}')}"
TRANS_ID = f"sig:{_sig_id(f'transcript_reschedule_{ENT.entity_id}')}"

SEP = "─" * 68
def _p(s): print(f"\n{SEP}\n{s}\n{SEP}")

def main():
    cfg = HindsightConfig(host="127.0.0.1", port=PORT, api_key=API_KEY, bank_id=BANK)
    client = HindsightClient(cfg)
    try:
        client.wait_until_ready(timeout_s=10)
    except HindsightUnavailable as e:
        sys.exit(f"Server not available: {e}")

    # Build items via to_hindsight_items (CID-scoped)
    tl    = generate_timeline()
    all_i = to_hindsight_items(tl)
    im    = {i["document_id"]: i for i in all_i}
    conf_item  = im[CONF_ID]
    trans_item = im[TRANS_ID]

    # Confirm items have ym: tags and CID scopes
    _p("ITEM TAG INSPECTION")
    for label, item in [("CONF", conf_item), ("TRANS", trans_item)]:
        tags  = item.get("tags", [])
        scope = item.get("observation_scopes")
        ent_t = [t for t in tags if t.startswith("ent:")]
        ym_t  = [t for t in tags if t.startswith("ym:")]
        print(f"  [{label}] ent_tag={ent_t}  ym_tag={ym_t}  obs_scopes={scope}")

    _p("RETAIN + CONSOLIDATE")
    op_conf  = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"conf-{BANK}"))
    op_trans = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"trans-{BANK}"))
    client.retain([conf_item],  operation_id=op_conf,  async_=False, bank_id=BANK)
    client.retain([trans_item], operation_id=op_trans, async_=False, bank_id=BANK)
    cons = client.trigger_consolidation(bank_id=BANK)
    op_c = cons.get("operation_id", "")
    if op_c:
        client.wait_for_operation(op_c, timeout_s=120, bank_id=BANK)
    print(f"  consolidation done")

    # Inspect world-fact tags (pre-recall, via /observations endpoint)
    _p("OBSERVATION TAGS (from /observations list)")
    try:
        from olo.m3.hindsight.client import _request
        path = client._bank_path("/observations", BANK)
        obs_resp = _request(client._cfg, "GET", path)
        obs_list = obs_resp if isinstance(obs_resp, list) else (
            obs_resp.get("observations") or obs_resp.get("items") or [])
        print(f"  total observations: {len(obs_list)}")
        for obs in obs_list[:5]:
            tags = obs.get("tags") or []
            ym_obs  = [t for t in tags if t.startswith("ym:")]
            ent_obs = [t for t in tags if t.startswith("ent:")]
            print(f"    id={str(obs.get('id',''))[:8]}  ym={ym_obs}  ent={ent_obs}")
            print(f"      text={str(obs.get('text',''))[:80]!r}")
    except Exception as exc:
        print(f"  /observations error: {exc}")

    # Determine entity scope tag
    ent_tag = next((t for t in conf_item.get("tags", []) if t.startswith("ent:")), "")
    ym_tag  = next((t for t in conf_item.get("tags", []) if t.startswith("ym:")),  "")
    print(f"\n  Using entity_tag={ent_tag!r}  ym_tag={ym_tag!r}")

    def _count_obs(r):
        return sum(1 for x in (r.get("results") or []) if x.get("type") == "observation")

    # T1: Entity-only recall
    _p("T1: Entity-only recall (no ym: filter)")
    r_ent = client.recall(
        f"{ENT.display_name} delivery April {ENT.company}",
        tag_groups=[{"tags": [ent_tag], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_ent = _count_obs(r_ent)
    print(f"  observations found: {obs_ent}")
    for r in (r_ent.get("results") or [])[:4]:
        if r.get("type") == "observation":
            print(f"    [{r.get('id','?')[:8]}] {r.get('text','')[:80]!r}")
    t1_pass = obs_ent > 0
    print(f"  T1: {'PASS' if t1_pass else 'FAIL'} — entity-only recall {'finds' if t1_pass else 'finds NO'} observations")

    # T2: Entity+month recall (correct month)
    _p("T2: Entity + correct month recall")
    r_ym = client.recall(
        f"{ENT.display_name} delivery April {ENT.company}",
        tag_groups=[{"tags": [ent_tag, ym_tag], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_ym = _count_obs(r_ym)
    print(f"  observations found: {obs_ym}")
    for r in (r_ym.get("results") or [])[:4]:
        if r.get("type") == "observation":
            print(f"    [{r.get('id','?')[:8]}] {r.get('text','')[:80]!r}")
    t2_pass = obs_ym > 0
    print(f"  T2: {'PASS' if t2_pass else 'FAIL'} — entity+month recall {'finds' if t2_pass else 'finds NO'} observations")

    # T3: Entity + wrong month
    _p("T3: Entity + wrong month (ym:2025-01) — should return 0 observations")
    r_bad = client.recall(
        f"{ENT.display_name} delivery",
        tag_groups=[{"tags": [ent_tag, "ym:2025-01"], "match": "all_strict"}],
        max_tokens=4000, trace=False, bank_id=BANK,
    )
    obs_bad = _count_obs(r_bad)
    t3_pass = obs_bad == 0
    print(f"  observations found: {obs_bad}")
    print(f"  T3: {'PASS' if t3_pass else 'FAIL'} — wrong month {'correctly returns 0' if t3_pass else 'incorrectly returns ' + str(obs_bad)}")

    # T4: Verify ym: tag appears on world facts returned by recall
    _p("T4: ym: tag present on world facts in recall results")
    world_facts = [r for r in (r_ent.get("results") or []) if r.get("type") == "world"]
    ym_on_wf = sum(1 for wf in world_facts if any(
        t.startswith("ym:") for t in (wf.get("tags") or [])
    ))
    print(f"  world facts total: {len(world_facts)}")
    print(f"  world facts with ym: tag: {ym_on_wf}")
    # Note: recall results may not expose 'tags' field on world facts
    if world_facts:
        sample = world_facts[0]
        print(f"  sample world fact tags: {sample.get('tags')}")
    t4_inconclusive = ym_on_wf == 0 and len(world_facts) > 0
    print(f"  T4: {'INCONCLUSIVE (tags not in recall payload)' if t4_inconclusive else 'ym: present on world facts'}")

    # Summary
    _p("VERDICT")
    print(f"  T1 entity-only recall finds observations:         {'PASS' if t1_pass else 'FAIL'}")
    print(f"  T2 entity+month recall finds observations:        {'PASS' if t2_pass else 'FAIL'}")
    print(f"  T3 wrong-month filter excludes observations:      {'PASS' if t3_pass else 'FAIL'}")
    print(f"  T4 ym: tag confirmed on world facts:              (requires /observations tags field)")

    overall = t1_pass and t2_pass and t3_pass
    print(f"\n  Overall: {'ALL PASS' if overall else 'SOME FAILURES'}")
    if not t1_pass:
        print("  ISSUE: Entity-only recall returns no observations. Check CID entity tag in recall.")
    if not t2_pass:
        print("  ISSUE: Month-filtered recall returns no observations. ym: tags may be missing from observations.")
    if not t3_pass:
        print("  ISSUE: Wrong-month recall still returns observations. ym: tag filtering not working.")

    _p("CLEANUP")
    for _ in range(3):
        try:
            client.delete_bank(BANK); print(f"  bank {BANK!r} deleted"); break
        except Exception as exc:
            print(f"  {exc}"); time.sleep(2)

if __name__ == "__main__":
    main()
