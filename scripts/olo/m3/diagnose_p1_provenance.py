#!/usr/bin/env python3
"""P1 failure diagnostic: trace document→world-fact→observation provenance.

Disposable bank only. Preserves main-v1-granite.

Steps:
  1. Retain the 4 P1 items in correct chronological order.
  2. Dump raw API responses: retain, world facts, observations, source_facts.
  3. Query Hindsight pg0 / PostgreSQL schema for world-fact and observation tables.
  4. Run targeted recall variants to determine which observation cites which sources.
  5. Reproduce the meeting/delivery contamination and characterise it.
  6. Determine whether provenance is traversable, server-only, or genuinely lost.

Usage (WSL2 as root):
  source /root/olo_m3_secrets/deepseek.env
  export HINDSIGHT_API_LLM_API_KEY
  export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
  cd /mnt/c/oloai
  source /root/olo_linux_venv/bin/activate
  python scripts/olo/m3/diagnose_p1_provenance.py 2>&1 | tee /tmp/p1_diag.log
"""
from __future__ import annotations

import hashlib, json, os, signal, subprocess, sys, time, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.hindsight.client import (
    HindsightClient, HindsightConfig, HindsightUnavailable, HindsightAPIError,
    _request,
)
from olo.m3.synthetic.generator import ENTITIES, _sig_id, generate_timeline
from olo.m3.pg0_lifecycle import safe_pg0_delete_home

PORT      = 19907
HOME      = Path("/home/olo_hs/p1_diag_home")
SOCKET    = HOME / "run"
HF_CACHE  = "/var/cache/huggingface"
BIN       = "/root/olo_m3_hindsight_venv/bin/hindsight-api"
API_KEY   = os.environ.get("HINDSIGHT_API_LLM_API_KEY", "")
BANK      = f"p1-diag-{uuid.uuid4().hex[:8]}"
ENT       = ENTITIES[0]   # Alice Chen / Acme Corp

UTC  = timezone.utc
BASE = datetime(2026, 1, 1, tzinfo=UTC)
D90  = BASE + timedelta(days=90)
D1   = (D90 + timedelta(days=3)).strftime("%B %d")   # April 04
D2   = (D90 + timedelta(days=7)).strftime("%B %d")   # April 08

CONF_ID  = f"sig:{_sig_id(f'email_confirm_orig_{ENT.entity_id}')}"
RESC_ID  = f"sig:{_sig_id(f'email_reschedule_{ENT.entity_id}')}"
TRANS_ID = f"sig:{_sig_id(f'transcript_reschedule_{ENT.entity_id}')}"
MTG_ID   = f"sig:{_sig_id(f'transcript_meeting_reschedule_{ENT.entity_id}')}"

HS_ENV = {
    "HOME": str(HOME),
    "HF_HOME": HF_CACHE,
    "HF_HUB_OFFLINE": "1",
    "HINDSIGHT_API_HOST": "127.0.0.1",
    "HINDSIGHT_API_PORT": str(PORT),
    "HINDSIGHT_API_DATABASE_URL": f"pg0://olo-p1-diag?unix_socket_directories={SOCKET}",
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


# ── helpers ───────────────────────────────────────────────────────────────────

def _p(label): print(f"\n{'─'*60}\n{label}\n{'─'*60}")
def _j(obj):   print(json.dumps(obj, indent=2, default=str))

def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        pass

def _sudo(*cmd):
    return ["sudo", "-u", "olo_hs", "env"] + [f"{k}={v}" for k,v in HS_ENV.items()] + list(cmd)

def _api(client, method, path, body=None):
    return _request(client._cfg, method, path, body)

def _recall(client, query, **kw):
    kw.setdefault("max_tokens", 12000)
    kw.setdefault("trace", True)
    kw.setdefault("include_source_facts", True)
    kw.setdefault("bank_id", BANK)
    return client.recall(query, **kw)

def _consolidate(client, label=""):
    resp = client.trigger_consolidation(bank_id=BANK)
    op   = resp.get("operation_id", "")
    if op:
        client.wait_for_operation(op, timeout_s=120, bank_id=BANK)
    return op

def _start_server():
    try:
        safe_pg0_delete_home(HOME)
    except Exception:
        pass
    subprocess.run(["chmod", "711", "/root"], check=False, capture_output=True)
    subprocess.run(["chmod", "-R", "a+rX", "/root/olo_m3_hindsight_venv"],
                   check=False, capture_output=True)
    subprocess.run(["sudo", "-u", "olo_hs", "mkdir", "-p", str(SOCKET)], check=True)

    log = open("/tmp/p1_diag_hs.log", "w")
    proc = subprocess.Popen(_sudo(BIN), stdout=log, stderr=log, start_new_session=True)
    cfg = HindsightConfig(host="127.0.0.1", port=PORT, api_key=API_KEY, bank_id=BANK)
    client = HindsightClient(cfg)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            client.wait_until_ready(timeout_s=5)
            print(f"Server ready. PID={proc.pid} bank={BANK}")
            return proc, client
        except (HindsightUnavailable, Exception):
            time.sleep(2)
    _kill(proc)
    raise RuntimeError("Server not ready")

def _build_items():
    tl = generate_timeline()
    im = {f"sig:{s.signal_id}": s for s in tl}
    items = {}
    for key, doc_id in [("confirm", CONF_ID), ("reschedule", RESC_ID),
                         ("transcript", TRANS_ID), ("meeting", MTG_ID)]:
        s = im[doc_id]
        sha = hashlib.sha256((s.body_text or "").encode()).hexdigest()
        items[key] = {
            "content": s.body_text or "",
            "timestamp": s.occurred_at.isoformat(),
            "document_id": doc_id,
            "entities": [{"text": ENT.entity_id, "type": "person"}],
            "resolve_entities": False,
            "tags": [f"ent:{ENT.entity_id}", "ym:2026-04", "test:diag"],
            "strategy": "prose",
            "metadata": {"content_sha256": sha, "source": s.source, "kind": s.kind},
        }
    return items

def _sources_set(obs, sf_map, world_results):
    """Collect all source document IDs for an observation (3 lookup levels)."""
    docs = set()
    for sfid in (obs.get("source_fact_ids") or []):
        # Level 1: direct document_id on sfid dict
        if isinstance(sfid, dict):
            d = sfid.get("document_id", "")
            if d: docs.add(d)
            fkey = sfid.get("id", "")
        else:
            fkey = str(sfid)
        # Level 2: source_facts map (from recall response)
        if fkey in sf_map:
            sf = sf_map[fkey]
            if isinstance(sf, dict):
                d = sf.get("document_id", "")
                if d: docs.add(d)
        # Level 3: world-type results (id → document_id)
        if fkey in world_results:
            docs.add(world_results[fkey])
    return docs

def _world_index(recall_resp):
    """Build {fact_id: document_id} from world-type results."""
    idx = {}
    for r in (recall_resp.get("results") or []):
        if r.get("type") == "world":
            fid = r.get("id", "")
            did = r.get("document_id", "")
            if fid and did:
                idx[fid] = did
    return idx

def _get_obs_history(client, obs_id):
    """Try /memories/{id}/history and /documents/{id}/history."""
    mem_hist, doc_hist = None, None
    try:
        path = client._bank_path(
            f"/memories/{obs_id}/history", BANK
        )
        mem_hist = _api(client, "GET", path)
    except Exception as e:
        mem_hist = {"error": str(e)}
    try:
        doc_hist = client.get_document_history(obs_id, bank_id=BANK)
    except Exception as e:
        doc_hist = {"error": str(e)}
    return mem_hist, doc_hist

def _query_pg0(query):
    """Run SQL against the pg0 instance for this bank."""
    try:
        result = subprocess.run(
            ["sudo", "-u", "olo_hs", "env",
             f"HOME={HOME}", "PATH=/usr/local/bin:/usr/bin:/bin",
             "psql", f"host={SOCKET} user=hindsight dbname=hindsight",
             "-t", "-c", query],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return f"pg0 query failed: {e}"

def _list_pg0_tables():
    return _query_pg0(r"\dt")

def _all_docs_in_bank(client):
    """Paginate list_documents and return all doc IDs + entity links."""
    all_docs = []
    offset = 0
    while True:
        try:
            resp = client.list_documents(limit=100, offset=offset, bank_id=BANK)
        except Exception:
            break
        docs = resp.get("items") or []
        all_docs.extend(docs)
        if len(docs) < 100:
            break
        offset += 100
    return all_docs


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if not API_KEY:
        print("FATAL: HINDSIGHT_API_LLM_API_KEY not set"); return 1

    _p(f"P1 PROVENANCE DIAGNOSTIC — {datetime.now(UTC).isoformat()}")
    print(f"SHA: ce55cd71ede6eb6b0cc180042680088df5a365c9")
    print(f"Bank: {BANK}  Port: {PORT}")
    print(f"Entity: {ENT.display_name} / {ENT.company}")
    print(f"D1={D1}  D2={D2}")
    print(f"confirm_id:   {CONF_ID}")
    print(f"reschedule_id:{RESC_ID}")
    print(f"transcript_id:{TRANS_ID}")
    print(f"meeting_id:   {MTG_ID}")

    items = _build_items()
    print(f"\nDocument body excerpts:")
    for k, item in items.items():
        print(f"  {k}: {item['content'][:100]!r}")

    _p("STARTING SERVER")
    proc, client = _start_server()

    report = {
        "bank": BANK, "d1": D1, "d2": D2,
        "confirm_id": CONF_ID, "transcript_id": TRANS_ID,
        "meeting_id": MTG_ID,
    }

    try:
        # ── Phase 1 retain ────────────────────────────────────────────────────
        _p("PHASE 1: retain reschedule (Apr 1) + confirm (Apr 2)")
        for key, op_sfx in [("reschedule","resc"), ("confirm","conf")]:
            op = str(uuid.uuid5(uuid.NAMESPACE_OID, f"p1d-{op_sfx}-{BANK}"))
            r = client.retain([items[key]], operation_id=op, async_=False, bank_id=BANK)
            print(f"  retain {key}: success={r.get('success')}  op_id={r.get('operation_id')!r}")

        _p("PHASE 1 CONSOLIDATE")
        cons1_op = _consolidate(client, "phase-1")
        time.sleep(3)

        # Raw world facts + observations after phase 1
        _p("RAW RECALL (phase 1) — delivery + D1 query")
        r1 = _recall(client,
            f"{ENT.display_name} delivery scheduled April {ENT.company}",
            tag_groups=[{"tags": [f"ent:{ENT.entity_id}"], "match": "all_strict"}])
        sf1    = r1.get("source_facts", {}) or {}
        world1 = _world_index(r1)
        results1 = r1.get("results") or []

        print(f"  Total results: {len(results1)}")
        print(f"  source_facts keys: {len(sf1)}  world_index entries: {len(world1)}")
        print(f"  source_facts keys sample: {list(sf1.keys())[:4]}")

        obs1_all = [r for r in results1 if r.get("type") == "observation"]
        wld1_all = [r for r in results1 if r.get("type") == "world"]
        print(f"  observations: {len(obs1_all)}  world_facts: {len(wld1_all)}")

        O = None
        for obs in obs1_all:
            srcs = _sources_set(obs, sf1, world1)
            obs["_diag_sources"] = list(srcs)
            t = (obs.get("text") or "").lower()
            has_delivery = "deliver" in t
            has_d1 = "april 04" in t or "april 4" in t or "2026-04-04" in t
            has_meeting = "meeting" in t
            if CONF_ID in srcs or any("conf" in str(s) for s in srcs):
                O = obs
            print(f"\n  OBS id={obs.get('id','?')!r}")
            print(f"    text={t[:180]!r}")
            print(f"    source_fact_ids={obs.get('source_fact_ids')!r}")
            print(f"    _diag_sources={srcs!r}")
            print(f"    has_delivery={has_delivery} has_d1={has_d1} has_meeting={has_meeting}")

        print(f"\n  World facts:")
        for w in wld1_all:
            wt = (w.get("text") or "").lower()
            print(f"    id={w.get('id','?')!r}  doc={w.get('document_id','?')!r}")
            print(f"    text={wt[:100]!r}")

        if O is None and obs1_all:
            O = obs1_all[0]
        O_id = O.get("id","") if O else ""
        print(f"\n  Selected O.id={O_id!r}")

        # Try to find O in source_facts or by explicit ID lookup
        if O_id:
            # Try GET /documents/{obs_id}
            try:
                od = client.get_document(O_id, bank_id=BANK)
                print(f"  GET /documents/{O_id[:12]}...: {list(od.keys())!r}")
                rp = od.get("retain_params") or {}
                print(f"    retain_params.entities: {rp.get('entities')!r}")
            except Exception as e:
                print(f"  GET /documents/{O_id[:12]}...: {e}")

        # ── Phase 2 retain ────────────────────────────────────────────────────
        _p("PHASE 2: retain transcript (Apr 3) + meeting distractor (Apr 4)")
        for key, op_sfx in [("transcript","trans"), ("meeting","mtg")]:
            op = str(uuid.uuid5(uuid.NAMESPACE_OID, f"p1d-{op_sfx}-{BANK}"))
            r = client.retain([items[key]], operation_id=op, async_=True, bank_id=BANK)
            print(f"  retain {key}: success={r.get('success')}  op_id={r.get('operation_id')!r}")
        for op_sfx in ["trans", "mtg"]:
            op = str(uuid.uuid5(uuid.NAMESPACE_OID, f"p1d-{op_sfx}-{BANK}"))
            try:
                client.wait_for_operation(op, timeout_s=90, bank_id=BANK)
                print(f"  wait {op_sfx}: done")
            except TimeoutError as e:
                print(f"  wait {op_sfx}: TIMEOUT: {e}")
        time.sleep(3)

        _p("PHASE 2 CONSOLIDATE")
        cons2_op = _consolidate(client, "phase-2")
        time.sleep(5)

        # ── Post-phase-2 deep trace ────────────────────────────────────────────
        _p("RAW RECALL (phase 2) — delivery rescheduled query")
        r2 = _recall(client,
            f"{ENT.display_name} delivery rescheduled April {ENT.company}",
            tag_groups=[{"tags": [f"ent:{ENT.entity_id}"], "match": "all_strict"}])
        sf2    = r2.get("source_facts", {}) or {}
        world2 = _world_index(r2)
        results2 = r2.get("results") or []

        print(f"  Total results: {len(results2)}")
        print(f"  source_facts keys: {len(sf2)}  world_index entries: {len(world2)}")

        obs2_all = [r for r in results2 if r.get("type") == "observation"]
        wld2_all = [r for r in results2 if r.get("type") == "world"]
        print(f"  observations: {len(obs2_all)}  world_facts: {len(wld2_all)}")

        # Print ALL world facts with their document IDs
        print(f"\n  World facts in phase-2 recall:")
        for w in wld2_all:
            wt = (w.get("text") or "").lower()
            print(f"    id={w.get('id','?')!r}  doc={w.get('document_id','?')!r}")
            print(f"    text={wt[:120]!r}")

        # Print source_facts map content
        print(f"\n  source_facts map:")
        for k, v in list(sf2.items())[:20]:
            print(f"    {k!r}: {v!r}")

        O2 = None
        for obs in obs2_all:
            srcs = _sources_set(obs, sf2, world2)
            obs["_diag_sources"] = list(srcs)
            obs["_diag_sf_ids"]  = obs.get("source_fact_ids", [])
            t = (obs.get("text") or "").lower()
            has_delivery  = "deliver" in t
            has_d2        = "april 08" in t or "april 8" in t or "2026-04-08" in t
            has_meeting   = "meeting" in t
            has_conf      = CONF_ID in srcs
            has_trans     = TRANS_ID in srcs
            has_mtg       = MTG_ID in srcs
            print(f"\n  OBS id={obs.get('id','?')!r}")
            print(f"    text={t[:200]!r}")
            print(f"    source_fact_ids (raw): {obs.get('source_fact_ids')!r}")
            print(f"    _diag_sources (resolved): {srcs!r}")
            print(f"    has_delivery={has_delivery} has_d2={has_d2} has_meeting={has_meeting}")
            print(f"    has_conf={has_conf} has_trans={has_trans} has_mtg={has_mtg}")
            if has_d2 and has_delivery:
                O2 = obs

        report["phase2_obs_count"]   = len(obs2_all)
        report["phase2_world_count"] = len(wld2_all)

        # ── History trace ─────────────────────────────────────────────────────
        _p("OBSERVATION HISTORY TRACE")
        if O2:
            O2_id = O2.get("id", "")
            print(f"  Tracing O2.id={O2_id!r}")
            mem_hist, doc_hist = _get_obs_history(client, O2_id)
            print(f"\n  /memories/{O2_id[:12]}..../history response:")
            _j(mem_hist)
            print(f"\n  /documents/{O2_id[:12]}..../history response:")
            _j(doc_hist)

            # Also trace the original O (phase-1 observation)
            if O_id and O_id != O2_id:
                print(f"\n  Tracing original O.id={O_id!r}")
                omh, odh = _get_obs_history(client, O_id)
                print(f"  /memories/{O_id[:12]}..../history:"); _j(omh)
        else:
            print("  O2 not found — skipping history trace")
            report["O2_not_found"] = True

        # ── World-fact → document deep lookup ─────────────────────────────────
        _p("WORLD FACT → DOCUMENT DEEP LOOKUP")
        # Collect all UUID source_fact_ids from O2 and try to resolve them
        if O2:
            sfids = O2.get("source_fact_ids") or []
            print(f"  O2 source_fact_ids: {sfids!r}")
            for sfid in sfids:
                fid = sfid.get("id","") if isinstance(sfid, dict) else str(sfid)
                if not fid.startswith("sig:"):
                    # This is a world-fact UUID; try GET /memories/{id}
                    try:
                        path = client._bank_path(f"/memories/{fid}", BANK)
                        wf = _api(client, "GET", path)
                        print(f"\n  GET /memories/{fid[:16]}...:")
                        _j(wf)
                    except Exception as e:
                        print(f"  GET /memories/{fid[:16]}...: {e}")

        # ── pg0 schema inspection ─────────────────────────────────────────────
        _p("pg0 / PostgreSQL SCHEMA INSPECTION")
        print("  Tables in Hindsight DB:")
        print(_list_pg0_tables())

        # Try to find world-fact and observation tables
        for tbl in ["world_facts", "facts", "observations", "memory_facts",
                    "document_facts", "knowledge_facts"]:
            q = f"SELECT COUNT(*) FROM {tbl} LIMIT 1"
            res = _query_pg0(q)
            if "error" not in res.lower() and "does not exist" not in res.lower():
                print(f"  {tbl}: {res}")
                # Sample rows
                sample = _query_pg0(f"SELECT * FROM {tbl} LIMIT 3")
                print(f"    sample: {sample[:300]}")

        # ── ALL documents in bank ─────────────────────────────────────────────
        _p("ALL DOCUMENTS IN BANK (list_documents)")
        all_docs = _all_docs_in_bank(client)
        print(f"  Total documents: {len(all_docs)}")
        for d in all_docs:
            did = d.get("document_id") or d.get("id", "?")
            rp = d.get("retain_params") or {}
            ents = rp.get("entities") or d.get("entities") or []
            el = ents[0].get("text","?") if ents else "?"
            print(f"    doc={did!r}  entity_link={el!r}")

        # ── CONTAMINATION INVESTIGATION ───────────────────────────────────────
        _p("MEETING/DELIVERY CONTAMINATION INVESTIGATION")
        # 1. Query specifically for delivery observation (not meeting)
        print("\n1. Targeted delivery-only recall:")
        rd = _recall(client,
            f"delivery rescheduled {ENT.company} April Wednesday",
            tag_groups=[{"tags": [f"ent:{ENT.entity_id}"], "match": "all_strict"}])
        for obs in [r for r in (rd.get("results") or []) if r.get("type") == "observation"][:3]:
            srcs = _sources_set(obs, rd.get("source_facts",{}) or {}, _world_index(rd))
            t = (obs.get("text") or "").lower()
            print(f"  id={obs.get('id','?')!r}  meeting_in_text={'meeting' in t}"
                  f"  meeting_in_srcs={MTG_ID in srcs}")
            print(f"  text={t[:150]!r}")
            print(f"  srcs={srcs!r}")

        # 2. Query specifically for MEETING observation
        print("\n2. Meeting-only recall:")
        rm = _recall(client,
            f"meeting rescheduled {ENT.company} April",
            tag_groups=[{"tags": [f"ent:{ENT.entity_id}"], "match": "all_strict"}])
        for obs in [r for r in (rm.get("results") or []) if r.get("type") == "observation"][:3]:
            srcs = _sources_set(obs, rm.get("source_facts",{}) or {}, _world_index(rm))
            t = (obs.get("text") or "").lower()
            print(f"  id={obs.get('id','?')!r}  delivery_in_text={'deliver' in t}"
                  f"  trans_in_srcs={TRANS_ID in srcs}")
            print(f"  text={t[:150]!r}")
            print(f"  srcs={srcs!r}")

        # 3. Count all observations
        print("\n3. All observations (broad recall):")
        rall = _recall(client,
            f"{ENT.display_name} {ENT.company} April schedule",
            tag_groups=[{"tags": [f"ent:{ENT.entity_id}"], "match": "all_strict"}])
        all_obs = [r for r in (rall.get("results") or []) if r.get("type") == "observation"]
        wld_all = _world_index(rall)
        sf_all = rall.get("source_facts", {}) or {}
        print(f"  Total observations: {len(all_obs)}")
        for obs in all_obs:
            srcs = _sources_set(obs, sf_all, wld_all)
            t = (obs.get("text") or "").lower()
            has_del  = "deliver" in t
            has_mtg  = "meeting" in t
            has_d1   = "april 04" in t or "april 4" in t
            has_d2   = "april 08" in t or "april 8" in t
            mtg_src  = MTG_ID in srcs
            trans_src = TRANS_ID in srcs
            conf_src  = CONF_ID in srcs
            print(f"  id={obs.get('id','?')[:16]}..."
                  f"  del={has_del} mtg={has_mtg}"
                  f"  d1={has_d1} d2={has_d2}"
                  f"  conf_src={conf_src} trans_src={trans_src} mtg_src={mtg_src}")
            print(f"    text={t[:180]!r}")
            print(f"    srcs={srcs!r}")

        # ── FINDINGS SUMMARY ─────────────────────────────────────────────────
        _p("FINDINGS SUMMARY")
        print("""
Issue 1 — A4 source provenance:
  When observations are retrieved, their source_fact_ids contain opaque world-fact
  UUIDs. The source_facts dict in the recall response may omit some entries.
  World-type results carry (id, document_id) so we can build an extended map.
  Whether confirm_orig's world fact appears in the top-K results determines
  if the extended map works.

  To determine: does the world fact from confirm_orig appear in world_index?
  Check above: look for doc_id == CONF_ID in world2 entries.

Issue 2 — Meeting/delivery contamination:
  Inspect whether a single observation cites both TRANS_ID and MTG_ID.
  If yes: consolidation merged them. If no: recall selected wrong observation.
""")

        # Check world2 for confirm_orig presence
        conf_in_world2 = {fid: did for fid, did in world2.items() if did == CONF_ID}
        print(f"  world_index entries pointing to confirm_orig: {conf_in_world2}")
        trans_in_world2 = {fid: did for fid, did in world2.items() if did == TRANS_ID}
        print(f"  world_index entries pointing to transcript:   {trans_in_world2}")

    except Exception as e:
        import traceback
        print(f"\nFATAL: {e}")
        traceback.print_exc()
    finally:
        try:
            client.delete_bank(BANK)
        except Exception:
            pass
        _kill(proc)
        print("\nServer stopped.")

    out = Path("/tmp/p1_diag_report.json")
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"Report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
