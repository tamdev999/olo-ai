"""S5 live acceptance driver — run ONCE against the server from start.sh.

Real worker (olo.runner.run_source), identity, DeepSeek extraction, ledger,
and the Hindsight projection stage (bank preparation, one-strategy retains,
prose-scoped consolidation, usage guard).  Gmail-shaped messages are built by
the real Gmail full-content builder; no mailbox is read.

Spend controls (client-side; Hindsight has no server-side monetary cap):
  * extraction Budget: 8 calls / 16,000 tokens;
  * projection: 20 documents / 8 prose documents per run, UsageGuard $0.15;
  * this driver stops before any paid step once the combined upper bound
    (extraction + Hindsight tokens at DeepSeek V3 list prices) reaches $0.20.
Failures are recorded, never retried selectively.  No secret is printed.

Usage (WSL, root):  python driver.py <evidence.json>
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
HH = Path("/home/olo_hs/s5accept_hermes")
os.environ["HERMES_HOME"] = str(HH)
EVIDENCE = Path(sys.argv[1])
STOP_USD = 0.20
IN_PER_M, OUT_PER_M = 0.14, 0.28

from olo.activation import ActivationRequest, activate
from olo.adapters import FetchBatch
from olo.adapters.gmail_full import build_email_content_signal
from olo.ledger import get_all_versions, get_current_state
from olo.m3.hindsight.budget import parse_stats
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, _request
from olo.projection import PROSE_SCOPE_TAG, prepare_bank
from olo.runner import run_source
from olo.signals import format_utc
from olo.store import connect, migrate

BANK, BIZ, SOURCE = "olo-s5accept", "s5accept", "gmail_s5accept"
OWNER, ALICE = "owner@s5accept.test", "Alice Customer <alice@customer.test>"
REGISTRY = HH / "hindsight_port_registry.json"
DB = Path("/root/olo_s5accept_db/olo.db")
ev: dict = {"started": format_utc(datetime.now(UTC)), "steps": {}}
state: dict = {}


def save() -> None:
    EVIDENCE.write_text(json.dumps(ev, indent=2, default=str), encoding="utf-8")


def step(name, fn):
    t0 = time.time()
    try:
        ev["steps"][name] = {"ok": True, "result": fn()}
    except Exception as exc:
        ev["steps"][name] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}",
                             "trace": traceback.format_exc()[-1500:]}
    ev["steps"][name]["seconds"] = round(time.time() - t0, 1)
    save()
    return ev["steps"][name]


# ── Gmail-shaped messages ─────────────────────────────────────────────────────

def gmail(msg_id, *, sent, subject, body, day, hour=9, thread="t-ord501"):
    frm, to = (OWNER, ALICE) if sent else (ALICE, OWNER)
    data = base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")
    raw = {"id": msg_id, "threadId": thread, "historyId": "1",
           "labelIds": ["SENT"] if sent else ["INBOX"],
           "internalDate": str(int(datetime(2026, 6, day, hour, tzinfo=UTC).timestamp() * 1000)),
           "sizeEstimate": 500,
           "payload": {"mimeType": "multipart/alternative", "filename": "", "body": {"size": 0},
                       "headers": [{"name": "From", "value": frm}, {"name": "To", "value": to},
                                   {"name": "Subject", "value": subject},
                                   {"name": "Message-ID", "value": f"<{msg_id}@s5accept.test>"}],
                       "parts": [{"partId": "0", "mimeType": "text/plain", "filename": "",
                                  "body": {"size": len(body), "data": data}}]}}
    return build_email_content_signal(SOURCE, raw, "1")


M1 = gmail("m1", sent=True, day=1, subject="Order ORD-501 delivery",
           body="Hi Alice,\n\nThank you for your order ORD-501. Your delivery is confirmed "
                "for 2026-06-10.\n\nBest regards,\nOwner")
M2 = gmail("m2", sent=False, day=2, subject="Re: Order ORD-501 delivery",
           body="Hi,\n\nCould we move the ORD-501 delivery from 2026-06-10 to 2026-06-15 "
                "instead?\n\nThanks,\nAlice\n\nOn Mon, 1 Jun 2026, Owner wrote:\n"
                "> Your delivery is confirmed for 2026-06-10.")
M3 = gmail("m3", sent=True, day=2, hour=11, thread="t-meeting", subject="Planning meeting",
           body="Hi Alice,\n\nConfirming our planning meeting on 2026-06-05 at 10:00.\n\n"
                "Best regards,\nOwner")
M4 = gmail("m4", sent=True, day=3, subject="Re: Order ORD-501 delivery",
           body="Hi Alice,\n\nConfirmed: your ORD-501 delivery has been moved from "
                "2026-06-10 to 2026-06-15.\n\nBest regards,\nOwner")
ev["provenance"] = {s.external_id: {"kind": s.kind, "direction": s.payload.get("direction"),
                                    "thread": s.payload.get("thread_id"),
                                    "blob_sha256": s.blobs[0].sha256} for s in (M1, M2, M3, M4)}


class Adapter:
    source = SOURCE

    def __init__(self):
        self.queue, self.n = [], 0

    def fetch(self, cursor, *, limit):
        batch, self.queue = self.queue, []
        self.n += 1
        return FetchBatch(signals=batch, has_more=False, next_cursor=f"c{self.n}")


hcfg = HindsightConfig.for_business(BIZ, registry_path=str(REGISTRY))
client = HindsightClient(hcfg)


# ── Spend ─────────────────────────────────────────────────────────────────────

def hs_tokens() -> tuple[int, int]:
    snap = parse_stats(client.get_llm_stats(bank_id=BANK))
    return snap.input_tokens, snap.output_tokens


def spend() -> dict:
    i, o = hs_tokens()
    i, o = i - state["base_in"], o - state["base_out"]
    hs = i * IN_PER_M / 1e6 + o * OUT_PER_M / 1e6
    b = state.get("budget")
    ex = b.cost_usd if b else 0.0
    ex_upper = (b.tokens_used * OUT_PER_M / 1e6) if b else 0.0
    return {"hindsight_tokens_in": i, "hindsight_tokens_out": o, "hindsight_usd": round(hs, 5),
            "extraction_calls": b.calls_used if b else 0, "extraction_tokens": b.tokens_used if b else 0,
            "extraction_usd": round(ex, 5), "total_usd_upper": round(hs + max(ex, ex_upper), 5)}


def guard(name: str) -> bool:
    s = spend()
    ev.setdefault("spend_checks", {})[name] = s
    save()
    if s["total_usd_upper"] >= STOP_USD:
        ev["stopped"] = f"spend {s['total_usd_upper']} >= {STOP_USD} before {name}"
        save()
        return False
    return True


# ── Evidence readers ──────────────────────────────────────────────────────────

def ledger_state() -> dict:
    conn = connect(DB)
    out = {}
    for cid, et, key in conn.execute("SELECT id, event_type, business_key FROM commitments ORDER BY created_at"):
        st = get_current_state(conn, cid)
        out[cid] = {"event_type": et, "key": key, "status": st["status"],
                    "scheduled_date": st["scheduled_date"], "pending_action": st["pending_action"],
                    "pending_proposed_date": st["pending_proposed_date"],
                    "versions": [{k: v[k] for k in ("version", "actor_role", "action", "confirmed_date",
                                                    "prior_confirmed_date", "proposed_date",
                                                    "is_confirmed", "source_doc_id")}
                                 for v in get_all_versions(conn, cid)]}
    conn.close()
    return out


def resolution() -> dict:
    conn = connect(DB)
    rows = conn.execute("SELECT signal_id, identity_status, contact_id, commitment_status,"
                        " commitment_outcome, commitment_id, commitment_version, commitment_attempts"
                        " FROM signal_resolution ORDER BY signal_id").fetchall()
    conn.close()
    return {r[0]: dict(r) for r in rows}


def outbox() -> dict:
    conn = connect(DB)
    ops = [dict(r) for r in conn.execute(
        "SELECT kind, status, doc_count, prose_count, detail FROM hindsight_operations ORDER BY rowid")]
    docs = {r[0]: r[1] for r in conn.execute("SELECT signal_id, status FROM hindsight_projection")}
    conn.close()
    return {"operations": ops, "documents": docs}


def memory() -> dict:
    out = {}
    for d in client.list_documents(bank_id=BANK, limit=100).get("items", []):
        full = client.get_document(d["id"], bank_id=BANK)
        out[d["id"]] = {"text": (full.get("original_text") or "")[:300], "tags": full.get("tags"),
                        "facts": full.get("nodes_by_fact_type"), "units": full.get("memory_unit_count")}
    return out


def observations() -> list:
    items = _request(hcfg, "GET", f"/v1/default/banks/{BANK}/memories/list",
                     params={"type": "observation", "limit": "100"}).get("items", [])
    return [{"tags": sorted(o.get("tags") or []), "text": (o.get("text") or "")[:200]} for o in items]


def recall(q: str, types=None) -> list:
    r = client.recall(q, bank_id=BANK, types=types)
    return [{"type": x.get("type") or x.get("fact_type"), "text": str(x.get("text"))[:200],
             "document_id": x.get("document_id")} for x in (r.get("results") or [])[:8]]


# ── 0. Isolation and controls ─────────────────────────────────────────────────

def preflight():
    version = _request(hcfg, "GET", "/version")
    refused = {}
    for label, key in (("no key", ""), ("wrong key", uuid.uuid4().hex * 2)):
        try:
            HindsightClient(HindsightConfig(port=hcfg.port, bank_id=BANK, api_key=key)).list_banks()
            refused[label] = "ALLOWED"
        except Exception as exc:
            refused[label] = f"refused: {getattr(exc, 'status_code', type(exc).__name__)}"
    banks = client.list_banks()
    return {"port": hcfg.port, "llm_trace": (version.get("features") or {}).get("llm_trace"),
            "version": version.get("api_version") or version.get("version"), "auth": refused,
            "banks_before": [b.get("bank_id") or b.get("id") for b in (banks.get("banks") or [])]}


pre = step("0_preflight", preflight)
r = pre.get("result") or {}
if not (pre["ok"] and r.get("llm_trace") is True and not r.get("banks_before")
        and all(str(v).startswith("refused: 401") for v in (r.get("auth") or {"x": ""}).values())):
    ev["stopped"] = "isolation or spend controls could not be established"
    save()
    sys.exit(2)


def provision():
    result = prepare_bank(client, BANK)
    cfg = client.get_bank_config(bank_id=BANK)["config"]
    return {"prepare_bank": result, "enable_auto_consolidation": cfg.get("enable_auto_consolidation"),
            "structured_strategy": (cfg.get("retain_strategies") or {}).get("structured-no-llm")}


prov = step("1_prepare_bank", provision)
pr = prov.get("result") or {}
if not (prov["ok"] and pr.get("enable_auto_consolidation") is False and pr.get("structured_strategy")):
    ev["stopped"] = "bank could not be prepared"
    save()
    sys.exit(3)
state["base_in"], state["base_out"] = hs_tokens()

DB.parent.mkdir(mode=0o700, exist_ok=True)
conn = connect(DB)
migrate(conn)
act_req = ActivationRequest(
    business_id=BIZ, enable_commitments=True, enable_projection=True, bank_id=BANK,
    deepseek_key_file="/root/olo_m3_secrets/deepseek.env", extract_max_calls=8,
    extract_max_tokens=16000, projection_max_docs=20, projection_max_prose_docs=8,
    projection_budget_usd=0.15, own_emails=(OWNER,), hindsight_registry=str(REGISTRY))


def do_activate():
    a = activate(act_req, conn, format_utc(datetime.now(UTC)))
    state["act"], state["budget"] = a, a.commitment.budget
    return {"commitment_budget": {"max_calls": a.commitment.budget.max_calls,
                                  "max_total_tokens": a.commitment.budget.max_total_tokens},
            "projection_limits": a.projection.limits.__dict__,
            "guard": type(a.projection.usage_guard).__name__,
            "model_id": getattr(a.commitment.model_fn, "model_id", None)}


act = step("2_activate", do_activate)
conn.close()
if not act["ok"]:
    ev["stopped"] = "activation refused"
    save()
    sys.exit(4)

adapter = Adapter()


def run(*signals):
    adapter.queue.extend(signals)
    a = state["act"]
    res = run_source(adapter, db_path=DB, commitment=a.commitment, projection=a.projection,
                     own_identifiers=a.own_identifiers)
    return {"status": res.status, "inserted": res.inserted, "duplicates": res.duplicates,
            "identity_resolved": res.identity_resolved, "commitment": res.commitment,
            "projection": {k: v for k, v in res.projection.items() if v}, "error": res.error}


_BUSY = ("consolidation_in_flight", "consolidation_deferred", "consolidation_submitted")


def settle(name: str, max_runs: int = 12, pause_s: int = 10) -> list:
    """Re-run the worker with no new mail until retains and consolidation settle."""
    runs = []
    for _ in range(max_runs):
        if not guard(f"{name}_{len(runs)}"):
            break
        time.sleep(pause_s)
        out = run()
        runs.append(out["projection"])
        p = out["projection"]
        submitted = any(o["status"] == "submitted" for o in outbox()["operations"])
        if not submitted and not any(p.get(k) for k in _BUSY):
            break
    return runs


# ── Scenario ──────────────────────────────────────────────────────────────────
if guard("run1"):
    step("3_run1_commit_request_meeting", lambda: run(M1, M2, M3))
    step("3_settle", lambda: settle("settle1"))
    step("3_state", lambda: {"ledger": ledger_state(), "resolution": resolution(),
                             "outbox": outbox(), "spend": spend()})
if guard("run2"):
    step("4_run2_business_confirms", lambda: run(M4))
    step("4_settle", lambda: settle("settle2"))
    step("4_state", lambda: {"ledger": ledger_state(), "resolution": resolution(), "outbox": outbox(),
                             "memory": memory(), "observations": observations(), "spend": spend()})
if guard("recall"):
    step("5_recall", lambda: {
        "delivery": recall("When is the ORD-501 delivery scheduled?"),
        "delivery_observations": recall("ORD-501 delivery date", types=["observation"]),
        "meeting": recall("When is the planning meeting?")})
if guard("run3"):
    ev["restart_before"] = {"versions": sum(len(c["versions"]) for c in ledger_state().values()),
                            "operations": len(outbox()["operations"]), "documents": len(memory())}
    step("6_run3_restart_replay", lambda: run(M1, M2, M3, M4))
    step("6_state", lambda: {"versions": sum(len(c["versions"]) for c in ledger_state().values()),
                             "operations": len(outbox()["operations"]), "documents": len(memory()),
                             "spend": spend()})

# ── Prose replacement under one document id (same request shapes as the stage)
PROBE, PROBE_ENT = "sig:probe-prose-1", "ent:cid_probe_bob"


def prose_item(text: str, rev: str) -> dict:
    return {"content": text, "timestamp": "2026-06-20T09:00:00Z", "document_id": PROBE,
            "entities": [{"text": "cid_probe_bob", "type": "person"}], "resolve_entities": False,
            "tags": [PROBE_ENT, "ym:2026-06", PROSE_SCOPE_TAG],
            "observation_scopes": [[PROBE_ENT, "ym:*"], [PROBE_ENT]],
            "metadata": {"rev": rev}, "strategy": "prose"}


def retain_and_consolidate(item) -> dict:
    op = str(uuid.uuid4())
    client.retain([item], operation_id=op, async_=True, bank_id=BANK)
    retained = client.wait_for_operation(op, timeout_s=300, poll_interval=2.0, bank_id=BANK)
    cons = client.trigger_consolidation(observation_scopes=[[PROSE_SCOPE_TAG]], bank_id=BANK)
    done = client.wait_for_operation(cons["operation_id"], timeout_s=300, poll_interval=2.0, bank_id=BANK)
    return {"retain": retained.get("status"), "consolidation": done.get("status"),
            "prose_pending_after": client.count_memories(
                consolidation_state="pending", tags=[PROSE_SCOPE_TAG], bank_id=BANK)}


def probe_snapshot():
    doc = client.get_document(PROBE, bank_id=BANK)
    hist = client.get_document_history(PROBE, bank_id=BANK)
    return {"text": doc.get("original_text"), "facts": doc.get("nodes_by_fact_type"),
            "units": doc.get("memory_unit_count"),
            "history": [{k: h.get(k) for k in ("facts_added", "facts_removed", "observations_created",
                                                "observations_updated", "observations_removed")}
                        for h in hist.get("items", [])],
            "recall": recall("When is Bob's ORD-777 delivery?"),
            "observations": [o for o in observations() if PROBE_ENT in o["tags"]], "spend": spend()}


if guard("probe_v1"):
    step("7_probe_v1", lambda: retain_and_consolidate(prose_item(
        "Customer Bob confirmed that his order ORD-777 will be delivered on 2026-07-01.", "1")))
    step("7_probe_v1_snapshot", probe_snapshot)
if guard("probe_v2"):
    step("8_probe_v2_same_id", lambda: retain_and_consolidate(prose_item(
        "Customer Bob's order ORD-777 is scheduled for delivery on 2026-07-09.", "2")))
    step("8_probe_v2_snapshot", probe_snapshot)


def cancel_probe():
    op = str(uuid.uuid4())
    item = dict(prose_item("Customer Carol asked about order ORD-888.", "c"),
                document_id="sig:probe-cancel-1", tags=["ent:cid_probe_carol", PROSE_SCOPE_TAG],
                entities=[{"text": "cid_probe_carol", "type": "person"}],
                observation_scopes=[["ent:cid_probe_carol"]])
    client.retain([item], operation_id=op, async_=True, bank_id=BANK)
    cancel = client.cancel_operation(op, bank_id=BANK)
    time.sleep(5)
    status = client.get_operation_status(op, bank_id=BANK)
    try:
        doc = client.get_document("sig:probe-cancel-1", bank_id=BANK)
        doc_state = {"exists": True, "units": doc.get("memory_unit_count")}
    except Exception as exc:
        doc_state = {"exists": False, "error": str(getattr(exc, "status_code", exc))}
    return {"cancel": {k: cancel.get(k) for k in ("success", "message")} if isinstance(cancel, dict) else cancel,
            "status_after": {k: status.get(k) for k in ("status", "error_message")}, "document": doc_state}


if guard("cancel_probe"):
    step("9_cancellation", cancel_probe)

step("10_final", lambda: {"ledger": ledger_state(), "memory": memory(), "observations": observations(),
                          "outbox": outbox(), "spend": spend(),
                          "llm_stats": client.get_llm_stats(bank_id=BANK),
                          "bank_stats": client.get_bank_stats(bank_id=BANK)})
ev["finished"] = format_utc(datetime.now(UTC))
save()
print("done; evidence:", EVIDENCE)
