#!/usr/bin/env python3
"""Reproduce the P1 semantic defect and produce a correction manifest.

Uses disposable data against a disposable Hindsight bank.
Never touches production banks or frozen benchmark inputs.

Evidence retained:
  1. email_confirm_orig  (body says "our meeting is still on for April 04")
  2. transcript_reschedule (body says "delivery is now rescheduled for April 08")

The defect: step 1 creates a "meeting" observation; step 2 creates a separate
"delivery" observation.  Because DeepSeek treats meeting and delivery as distinct
business objects, no contradiction is detected and the original "meeting April 04"
observation persists alongside the new "delivery April 08" observation.

The P1 gate test expects: April 08 is confirmed AND April 04 is still traceable
(as historical record).  The gate comment explicitly says DeepSeek creates
"separate observations" for the two — meaning the test passes on a technicality
(April 04 IS still traceable, just as a meeting rather than as a delivery record).

This script records:
  - Exact retained items and their content hashes
  - Extracted observations after each consolidation
  - Recall results for key queries
  - Whether the P1 gate assertions would pass

Usage:
  source /root/olo_m3_secrets/deepseek.env
  export HINDSIGHT_API_LLM_API_KEY
  export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
  cd /mnt/c/oloai
  source /root/olo_linux_venv/bin/activate
  python scripts/olo/m3/reproduce_p1_defect.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.hindsight.client import (
    HindsightClient,
    HindsightConfig,
    HindsightUnavailable,
    HindsightAPIError,
)
from olo.m3.synthetic.generator import ENTITIES, _sig_id, generate_timeline
from olo.m3.pg0_lifecycle import safe_pg0_delete_home

PORT = 19905
OLO_HS_HOME = Path("/home/olo_hs/p1_repro_home")
OLO_HS_SOCKET = OLO_HS_HOME / "run"
HF_CACHE = "/var/cache/huggingface"
HINDSIGHT_BIN = "/root/olo_m3_hindsight_venv/bin/hindsight-api"
API_KEY = os.environ.get("HINDSIGHT_API_LLM_API_KEY", "")
P1_BANK = f"p1-repro-{uuid.uuid4().hex[:8]}"

# P1 dates (matching generator: day_90, day_90+7)
UTC = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)
DAY_90 = BASE + timedelta(days=90)
DATE_ORIG = (DAY_90 + timedelta(days=3)).strftime("%A %B %d")   # April 04
DATE_NEW  = (DAY_90 + timedelta(days=7)).strftime("%A %B %d")   # April 08

# Use ENTITIES[0] (Alice/ACME)
ENT = ENTITIES[0]

HS_ENV = {
    "HOME": str(OLO_HS_HOME),
    "HF_HOME": HF_CACHE,
    "HF_HUB_OFFLINE": "1",
    "HINDSIGHT_API_HOST": "127.0.0.1",
    "HINDSIGHT_API_PORT": str(PORT),
    "HINDSIGHT_API_DATABASE_URL": (
        f"pg0://olo-p1-repro?unix_socket_directories={OLO_HS_SOCKET}"
    ),
    "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
    "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID": "intfloat/multilingual-e5-small",
    "HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA": "false",
    "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
    "HINDSIGHT_API_LLM_PROVIDER": "deepseek",
    "HINDSIGHT_API_LLM_MODEL": "deepseek-chat",
    "HINDSIGHT_API_LLM_API_KEY": API_KEY,
    "HINDSIGHT_API_LLM_STRICT_SCHEMA": "false",
    "HINDSIGHT_API_LLM_TRACE_ENABLED": "true",
    "HINDSIGHT_API_DECISION_MODEL_PROVIDER": "none",
    "HINDSIGHT_API_MCP_ENABLED": "false",
    "HINDSIGHT_API_STORE_DOCUMENT_TEXT": "true",
    "OTEL_SDK_DISABLED": "true",
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def start_server():
    try:
        safe_pg0_delete_home(OLO_HS_HOME)
    except Exception:
        pass
    subprocess.run(["chmod", "711", "/root"], check=False, capture_output=True)
    subprocess.run(["chmod", "-R", "a+rX", "/root/olo_m3_hindsight_venv"],
                   check=False, capture_output=True)
    subprocess.run(["sudo", "-u", "olo_hs", "mkdir", "-p", str(OLO_HS_SOCKET)],
                   check=True)
    env_list = [f"{k}={v}" for k, v in HS_ENV.items()]
    log_fh = open("/tmp/p1_repro_hs.log", "w")
    proc = subprocess.Popen(
        ["sudo", "-u", "olo_hs", "env"] + env_list + [HINDSIGHT_BIN],
        stdout=log_fh, stderr=log_fh, start_new_session=True,
    )
    cfg = HindsightConfig(host="127.0.0.1", port=PORT, api_key=API_KEY, bank_id=P1_BANK)
    client = HindsightClient(cfg)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            client.wait_until_ready(timeout_s=5)
            return proc, client
        except (HindsightUnavailable, Exception):
            time.sleep(2)
    raise RuntimeError("Server not ready")


def kill_server(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        pass


def build_p1_items(tl):
    """Build the three P1 evidence items from the generated timeline."""
    sig_map = {s.signal_id: s for s in tl}
    confirm_id  = _sig_id(f"email_confirm_orig_{ENT.entity_id}")
    reschedule_id = _sig_id(f"email_reschedule_{ENT.entity_id}")
    transcript_id = _sig_id(f"transcript_reschedule_{ENT.entity_id}")

    def _item(sig_id, entity_id=ENT.entity_id):
        s = sig_map[sig_id]
        sha = _h(s.body_text or "")
        return {
            "content": s.body_text or "",
            "timestamp": s.occurred_at.isoformat(),
            "document_id": f"sig:{sig_id}",
            "entities": [{"text": entity_id, "type": "person"}],
            "resolve_entities": False,
            "tags": [f"ent:{ENT.entity_id}", f"ym:2026-04", "test:p1-repro"],
            "strategy": "prose",
            "metadata": {
                "signal_id": sig_id, "content_sha256": sha,
                "render_version": "1", "is_llm_eligible": "true",
                "source": s.source, "kind": s.kind,
                "subject": s.subject or "",
            },
        }

    return {
        "confirm": _item(confirm_id),
        "reschedule": _item(reschedule_id),
        "transcript": _item(transcript_id),
        "confirm_body": (sig_map[confirm_id].body_text or ""),
        "reschedule_body": (sig_map[reschedule_id].body_text or ""),
        "transcript_body": (sig_map[transcript_id].body_text or ""),
        "confirm_subject": (sig_map[confirm_id].subject or ""),
        "reschedule_subject": (sig_map[reschedule_id].subject or ""),
        "transcript_subject": (sig_map[transcript_id].subject or ""),
        "confirm_id": f"sig:{confirm_id}",
        "reschedule_id": f"sig:{reschedule_id}",
        "transcript_id": f"sig:{transcript_id}",
    }


def get_all_observations(client, bank_id):
    """Get all observations from recall on a broad entity query."""
    try:
        r = client.recall(
            f"{ENT.display_name} {ENT.company} meeting delivery schedule April",
            tag_groups=[{"tags": [f"ent:{ENT.entity_id}"], "match": "all_strict"}],
            max_tokens=8000, trace=True, bank_id=bank_id,
        )
        return r.get("results", [])
    except Exception as e:
        return [{"error": str(e)}]


def wait_for_consolidation(client, bank_id, label="consolidation", timeout_s=120):
    """Trigger consolidation and wait for completion."""
    try:
        resp = client.trigger_consolidation(bank_id=bank_id)
        op_id = resp.get("operation_id", "")
        if op_id:
            client.wait_for_operation(op_id, timeout_s=timeout_s, bank_id=bank_id)
            return {"triggered": True, "op_id": op_id, "status": "completed"}
        return {"triggered": True, "op_id": None, "status": "no_op_id"}
    except TimeoutError as e:
        return {"triggered": True, "error": f"timeout: {e}"}
    except Exception as e:
        return {"triggered": False, "error": str(e)}


def main():
    if not API_KEY:
        print("FATAL: HINDSIGHT_API_LLM_API_KEY not set")
        return 1

    print(f"\n{'='*60}")
    print("P1 Semantic Defect Reproduction")
    print(f"{'='*60}")
    print(f"Time:    {_now()}")
    print(f"Entity:  {ENT.display_name} / {ENT.company}")
    print(f"Bank:    {P1_BANK}")
    print(f"D1:      {DATE_ORIG}")
    print(f"D2:      {DATE_NEW}")

    tl = generate_timeline()
    items = build_p1_items(tl)

    print(f"\n--- Source Documents ---")
    print(f"confirm_orig  ({items['confirm_id']})")
    print(f"  Subject: {items['confirm_subject']!r}")
    print(f"  Body:    {items['confirm_body']!r}")
    print(f"reschedule_email ({items['reschedule_id']})")
    print(f"  Subject: {items['reschedule_subject']!r}")
    print(f"  Body:    {items['reschedule_body'][:100]!r}...")
    print(f"transcript ({items['transcript_id']})")
    print(f"  Subject: {items['transcript_subject']!r}")
    print(f"  Body:    {items['transcript_body'][:100]!r}...")

    report = {
        "sha": "aa136d69a60534f5cbba50194b5a3053c84ba754",
        "bank": P1_BANK,
        "date_orig": DATE_ORIG,
        "date_new": DATE_NEW,
        "source_documents": {
            "confirm_orig": {
                "id": items["confirm_id"],
                "subject": items["confirm_subject"],
                "body_excerpt": items["confirm_body"],
                "defect": "body says 'meeting' in a delivery thread",
            },
            "reschedule_email": {
                "id": items["reschedule_id"],
                "subject": items["reschedule_subject"],
                "body_excerpt": items["reschedule_body"][:200],
            },
            "transcript": {
                "id": items["transcript_id"],
                "subject": items["transcript_subject"],
                "body_excerpt": items["transcript_body"][:200],
            },
        },
        "steps": {},
        "gate_assertion_outcome": {},
        "correction_manifest": {},
    }

    proc = None
    try:
        print(f"\n--- Starting Hindsight server (DeepSeek) ---")
        proc, client = start_server()
        print("Server ready.")

        # === Step 1: Retain confirm_orig ===
        print(f"\n--- Step 1: Retain email_confirm_orig ---")
        op1 = str(uuid.uuid5(uuid.NAMESPACE_OID, f"p1-confirm-{P1_BANK}"))
        resp1 = client.retain([items["confirm"]], operation_id=op1,
                               async_=False, bank_id=P1_BANK)
        time.sleep(2)
        print(f"  Retained: success={resp1.get('success')}")

        # === Step 2: Consolidate 1 ===
        print(f"\n--- Step 2: Consolidate (creates initial observation) ---")
        cons1 = wait_for_consolidation(client, P1_BANK, label="consolidation_1")
        print(f"  Consolidation 1: {cons1}")
        time.sleep(3)

        obs_after_step2 = get_all_observations(client, P1_BANK)
        print(f"\n  Observations after Step 2 ({len(obs_after_step2)} results):")
        for i, r in enumerate(obs_after_step2[:5]):
            t = (r.get("text") or r.get("content") or r.get("body") or "")[:150]
            did = r.get("document_id", "?")
            rtype = r.get("type", "?")
            print(f"    [{i+1}] type={rtype} doc={did}")
            print(f"         text={t!r}")
        report["steps"]["after_step2_consolidation"] = {
            "operation": cons1,
            "observations_count": len(obs_after_step2),
            "observations": [
                {
                    "type": r.get("type", "?"),
                    "doc_id": r.get("document_id", ""),
                    "text_excerpt": (r.get("text") or r.get("content") or "")[:200],
                }
                for r in obs_after_step2[:5]
            ],
        }

        # === Step 3: Retain reschedule + transcript ===
        print(f"\n--- Step 3: Retain reschedule email + transcript ---")
        op2 = str(uuid.uuid5(uuid.NAMESPACE_OID, f"p1-reschedule-{P1_BANK}"))
        op3 = str(uuid.uuid5(uuid.NAMESPACE_OID, f"p1-transcript-{P1_BANK}"))
        for item, op, label in [
            (items["reschedule"], op2, "reschedule email"),
            (items["transcript"], op3, "transcript"),
        ]:
            resp = client.retain([item], operation_id=op, async_=True, bank_id=P1_BANK)
            print(f"  Retained {label}: success={resp.get('success')}")
        # Wait for async retains
        for op_id, label in [(op2, "reschedule"), (op3, "transcript")]:
            try:
                client.wait_for_operation(op_id, timeout_s=90, bank_id=P1_BANK)
                print(f"  Wait {label}: completed")
            except TimeoutError as e:
                print(f"  Wait {label}: TIMEOUT: {e}")
        time.sleep(3)

        # === Step 4: Consolidate 2 ===
        print(f"\n--- Step 4: Consolidate 2 (should detect rescheduling) ---")
        cons2 = wait_for_consolidation(client, P1_BANK, label="consolidation_2",
                                        timeout_s=120)
        print(f"  Consolidation 2: {cons2}")
        time.sleep(5)

        # === Step 5: Recall and check ===
        print(f"\n--- Step 5: Recall results ---")
        obs_after_step4 = get_all_observations(client, P1_BANK)
        print(f"\n  All observations/results after Step 4 ({len(obs_after_step4)}):")
        for i, r in enumerate(obs_after_step4[:8]):
            t = (r.get("text") or r.get("content") or r.get("body") or "")[:200]
            did = r.get("document_id", "?")
            rtype = r.get("type", "?")
            sf = r.get("source_fact_ids", [])
            print(f"    [{i+1}] type={rtype} doc={did}")
            print(f"         text={t!r}")
            if sf:
                print(f"         sources={sf[:3]}")
        report["steps"]["after_step4_consolidation"] = {
            "operation": cons2,
            "observations_count": len(obs_after_step4),
            "observations": [
                {
                    "type": r.get("type", "?"),
                    "doc_id": r.get("document_id", ""),
                    "source_fact_ids": (r.get("source_fact_ids") or [])[:3],
                    "text_excerpt": (r.get("text") or r.get("content") or "")[:300],
                }
                for r in obs_after_step4[:8]
            ],
        }

        # === P1 Gate Assertion Check ===
        print(f"\n--- P1 Gate Assertion Check ---")
        all_obs_text = " ".join(
            (r.get("text") or r.get("content") or "").lower()
            for r in obs_after_step4[:8]
        )

        # Find the "rescheduling" observation
        reschedule_obs = None
        for r in obs_after_step4[:8]:
            t = (r.get("text") or r.get("content") or "").lower()
            if any(kw in t for kw in ("reschedule", "rescheduled", "moved to",
                                       DATE_NEW.lower(), "april 08", "april 8")):
                reschedule_obs = r
                break

        # Assert 1: April 08 confirmation exists
        obs_text = (reschedule_obs.get("text") or reschedule_obs.get("content") or "") if reschedule_obs else ""
        iso_april = re.findall(r'2026-04-(\d{2})', obs_text.lower())
        extracted_days = {int(d.lstrip('0') or '0') for d in iso_april}
        april_numbers = re.findall(r'april\s+(\d+)|(\d+)\s+april', obs_text.lower())
        extracted_days.update(int(n) for pair in april_numbers for n in pair if n)
        d2_day = int(DATE_NEW.split()[-1].lstrip("0") or "0")
        d1_day = int(DATE_ORIG.split()[-1].lstrip("0") or "0")
        date_ref_found = any(d > d1_day for d in extracted_days)
        rescheduling_confirmed = any(
            kw in obs_text.lower()
            for kw in ("reschedule", "rescheduled", "moved", "changed",
                       DATE_NEW.lower(), "april 08", "april 8")
        )
        assert1 = date_ref_found or rescheduling_confirmed

        # Assert 2: April 04 still traceable
        april04_traceable = any(
            kw in all_obs_text
            for kw in (DATE_ORIG.lower(), "april 04", "april 4", "2026-04-04")
        )
        assert2 = april04_traceable

        # Assert 3: Supersession explicitly stated in the correction obs
        supersession_stated = any(
            kw in obs_text.lower()
            for kw in ("reschedule", "rescheduled", "moved", "changed",
                       "new date", DATE_NEW.lower(), "april 08", "april 8")
        ) if reschedule_obs else False
        assert3 = supersession_stated

        # Assert 4: Source document found
        expected_sources = {items["reschedule_id"], items["transcript_id"]}
        raw_sources: set[str] = set()
        if reschedule_obs:
            for sfid in (reschedule_obs.get("source_fact_ids") or []):
                sid = sfid.get("document_id", "") if isinstance(sfid, dict) else str(sfid)
                raw_sources.add(sid)
        for r in obs_after_step4[:20]:
            did = r.get("document_id", "") or ""
            if did in expected_sources:
                raw_sources.add(did)
        found_sources = raw_sources & expected_sources
        assert4 = bool(found_sources)

        print(f"\n  Gate assertions (from test_new_date_in_observation_old_date_in_history):")
        print(f"  [1] Date D2={DATE_NEW!r} confirmed:  {'PASS' if assert1 else 'FAIL'}")
        print(f"      extracted_days={extracted_days}, reschedule_confirmed={rescheduling_confirmed}")
        print(f"      obs={obs_text[:200]!r}")
        print(f"  [2] Date D1={DATE_ORIG!r} traceable: {'PASS' if assert2 else 'FAIL'}")
        print(f"      april04_in_all_obs={april04_traceable}")
        print(f"  [3] Supersession stated:              {'PASS' if assert3 else 'FAIL'}")
        print(f"  [4] Source document found:            {'PASS' if assert4 else 'FAIL'}")
        print(f"      found={found_sources}, expected={expected_sources}")

        overall = all([assert1, assert2, assert3, assert4])
        print(f"\n  Overall P1 gate result: {'PASS' if overall else 'FAIL'}")

        # === Semantic defect analysis ===
        print(f"\n--- Semantic Defect Analysis ---")
        meeting_obs = [r for r in obs_after_step4[:8]
                       if "meeting" in (r.get("text") or r.get("content") or "").lower()]
        delivery_obs = [r for r in obs_after_step4[:8]
                        if "delivery" in (r.get("text") or r.get("content") or "").lower()]
        print(f"  Observations mentioning 'meeting':  {len(meeting_obs)}")
        for o in meeting_obs[:3]:
            print(f"    {(o.get('text') or o.get('content') or '')[:150]!r}")
        print(f"  Observations mentioning 'delivery': {len(delivery_obs)}")
        for o in delivery_obs[:3]:
            print(f"    {(o.get('text') or o.get('content') or '')[:150]!r}")

        body_says_meeting = "meeting" in items["confirm_body"].lower()
        body_says_delivery = "delivery" in items["confirm_body"].lower()
        print(f"\n  email_confirm_orig body analysis:")
        print(f"    subject says 'delivery': {'delivery' in items['confirm_subject'].lower()}")
        print(f"    body says 'meeting':     {body_says_meeting}")
        print(f"    body says 'delivery':    {body_says_delivery}")
        print(f"    -> DEFECT CONFIRMED: {body_says_meeting and not body_says_delivery}")

        report["gate_assertion_outcome"] = {
            "assert1_d2_confirmed": assert1,
            "assert2_d1_traceable": assert2,
            "assert3_supersession_stated": assert3,
            "assert4_source_found": assert4,
            "overall_pass": overall,
            "reschedule_obs_found": reschedule_obs is not None,
            "meeting_obs_count": len(meeting_obs),
            "delivery_obs_count": len(delivery_obs),
            "defect_body_says_meeting": body_says_meeting,
            "defect_body_says_delivery": body_says_delivery,
        }

    except Exception as e:
        import traceback
        print(f"\nFATAL: {e}")
        traceback.print_exc()
        report["fatal_error"] = str(e)
    finally:
        if proc:
            try:
                client.delete_bank(P1_BANK)
            except Exception:
                pass
            kill_server(proc)
            print("\nServer stopped.")

    # === Correction Manifest ===
    print(f"\n{'='*60}")
    print("PROPOSED CORRECTION MANIFEST")
    print(f"{'='*60}")

    tl_for_manifest = tl  # already built
    manifest = build_correction_manifest(tl_for_manifest)
    report["correction_manifest"] = manifest

    for entry in manifest:
        print(f"\n[{entry['id']}] {entry['title']}")
        print(f"  Current label:  {entry['current_label']}")
        print(f"  Problem:        {entry['problem']}")
        print(f"  Proposed fix:   {entry['proposed_fix']}")
        print(f"  Evidence:       {entry['evidence']}")

    # Save report
    out_path = Path("/tmp/p1_repro_report.json")
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nFull report saved: {out_path}")
    return 0 if report.get("gate_assertion_outcome", {}).get("overall_pass") else 1


def build_correction_manifest(tl):
    from olo.m3.synthetic.generator import _sig_id as _s
    ent = ENT
    manifest = [
        {
            "id": "CM-01",
            "artifact": "olo/m3/synthetic/generator.py",
            "field": "email_confirm_orig.body_text",
            "title": "email_confirm_orig body says 'meeting' in a delivery thread",
            "current_label": f"body: 'just to confirm, our meeting is still on for {DATE_ORIG}'",
            "problem": (
                "The email is a reply in the delivery-reschedule thread "
                "(thread_id=reschedule_thread). Its subject says 'RE: Delivery for {date}'. "
                "But the body says 'our meeting is still on', causing DeepSeek to extract "
                "a 'meeting' observation. The transcript_reschedule then extracts a "
                "'delivery' observation. DeepSeek treats them as separate business objects "
                "and no contradiction is detected, breaking the P1 scenario intent."
            ),
            "proposed_fix": (
                "Change body to: 'just to confirm, your delivery is still scheduled for "
                f"{DATE_ORIG} as originally planned. We haven't made any changes.' "
                "Requires corpus regeneration and fingerprint update."
            ),
            "evidence": f"signal_id={_s(f'email_confirm_orig_{ent.entity_id}')}, "
                        f"subject='RE: Delivery for {DATE_ORIG} — {ent.company}', "
                        f"body contains 'meeting' but not 'delivery'",
        },
        {
            "id": "CM-02",
            "artifact": "olo/m3/benchmark_v2.py",
            "field": "mtg_02.expected_signal_ids",
            "title": "mtg_02 label includes delivery transcript for a meeting question",
            "current_label": (
                f"[meeting_trans, reschedule_trans, p4_meet_orig, p4_meet_corr] "
                f"where reschedule_trans={_s(f'transcript_reschedule_{ent.entity_id}')}"
            ),
            "problem": (
                "transcript_reschedule body says 'delivery is now rescheduled' — it is "
                "a delivery transcript. Including it in mtg_02 ('Did {contact} request "
                "to change a meeting date?') is a semantic mismatch. The v2.2 evaluator "
                "is_meeting_change_request() correctly excludes it via "
                "'delivery not in body'."
            ),
            "proposed_fix": (
                f"Remove {_s(f'transcript_reschedule_{ent.entity_id}')} from mtg_02. "
                "Updated expected: [meeting_trans, p4_meet_orig, p4_meet_corr]"
            ),
            "evidence": f"transcript_reschedule body: 'delivery is now rescheduled for {DATE_NEW}'",
        },
        {
            "id": "CM-03",
            "artifact": "olo/m3/benchmark_v2.py",
            "field": "del_01.expected_signal_ids",
            "title": "del_01 omits meeting transcript for 'meeting or delivery' question",
            "current_label": (
                f"[reschedule_email, reschedule_trans, p4_del_orig, p4_del_corr] "
                f"-- meeting_trans={_s(f'transcript_meeting_reschedule_{ent.entity_id}')} absent"
            ),
            "problem": (
                "Query: 'Has {company} ever requested to reschedule a meeting or delivery?' "
                "explicitly includes meetings. transcript_meeting_reschedule body says "
                "'I need to move [the meeting] to {date_new}' — it IS a meeting reschedule "
                "request. It should be in the label set for del_01."
            ),
            "proposed_fix": (
                f"Add {_s(f'transcript_meeting_reschedule_{ent.entity_id}')} to del_01. "
                "Updated expected: [reschedule_email, reschedule_trans, meeting_trans, "
                "p4_del_orig, p4_del_corr]"
            ),
            "evidence": (
                f"transcript_meeting_reschedule body: 'I need to move [the meeting] "
                f"to {DATE_NEW}'"
            ),
        },
        {
            "id": "CM-04",
            "artifact": "olo/m3/benchmark_v2.py",
            "field": "mtg_05.expected_signal_ids",
            "title": "mtg_05 label points to delivery-context signal for meeting question",
            "current_label": f"[email_confirm_orig] = [{_s(f'email_confirm_orig_{ent.entity_id}')}]",
            "problem": (
                "Query: 'Find the email confirming a meeting was still happening on the "
                "original date for {company}.' email_confirm_orig body currently says "
                "'our meeting is still on' (the CM-01 defect). Once CM-01 is fixed "
                "(body → 'delivery'), this signal no longer satisfies the meeting query. "
                "A better answer is p4_meeting_orig which is a customer email confirming "
                "a meeting on a specific date, without any delivery confusion."
            ),
            "proposed_fix": (
                f"Replace email_confirm_orig with p4_meeting_orig "
                f"({_s(f'p4_meeting_orig_{ent.entity_id}')}). "
                "p4_meeting_orig body: 'just confirming our meeting is scheduled for "
                "{date}. Please add it to your calendar.' — purely a meeting confirmation."
            ),
            "evidence": (
                f"p4_meeting_orig subject='Meeting confirmed for {DATE_ORIG}', "
                "body contains 'meeting is scheduled' with no delivery mention"
            ),
        },
        {
            "id": "CM-05",
            "artifact": "olo/m3/benchmark_v2.py",
            "field": "inv_02 / inv_05 / inv_10 / rel_* .expected_signal_ids",
            "title": "voice_qbo is a phantom signal: not generated but referenced in labels",
            "current_label": (
                f"voice_qbo={_s(f'voice_qbo_{ent.entity_id}')} "
                "in inv_02, inv_05, inv_10, cross-connector and voice questions"
            ),
            "problem": (
                "generator.py never produces a signal with signal_id matching "
                "_sig_id(f'voice_qbo_{ent.entity_id}'). The signal is referenced in "
                "benchmark_v2.py labels for 5+ question categories per entity (3 entities "
                "= 15+ phantom references). inv_05 expected=[dispute_trans, voice_qbo] "
                "but only dispute_trans exists (effective denominator=1 not 2). "
                "Pass@K scores are unaffected (phantom never retrieved), but set-coverage "
                "metrics overcount expected set size."
            ),
            "proposed_fix": (
                "Either: (a) add a voice_qbo signal to generator.py representing a QBO "
                "invoice discussion call, OR (b) remove all voice_qbo references from "
                "benchmark_v2.py label sets. Option (a) requires corpus regeneration; "
                "option (b) is a pure label correction with no corpus change."
            ),
            "evidence": (
                f"grep benchmark_v2.py for voice_qbo: 6 references in label sets. "
                f"grep generator.py for voice_qbo: 0 matches. "
                f"Verified programmatically in test_p1_semantics.py::TestVoiceQboMissingSignal."
            ),
        },
    ]

    # Per-entity expansion note
    manifest.append({
        "id": "CM-05-note",
        "artifact": "olo/m3/benchmark_v2.py",
        "field": "(all three entities)",
        "title": "CM-01 through CM-05 apply identically to all three entities",
        "current_label": "3 entities × affected questions",
        "problem": "Each benchmark question is instantiated for ENTITIES[0] (Alice/ACME), "
                   "ENTITIES[1] (Bob/Beta), and ENTITIES[2] (Carla/Gamma).",
        "proposed_fix": "Apply the same correction for each entity instance.",
        "evidence": f"ENTITIES = {[e.display_name for e in ENTITIES]}",
    })

    return manifest


if __name__ == "__main__":
    sys.exit(main())
