"""One real Hermes agent turn over Olo's customer context, judged by the ledger.

1. **Business.**  A disposable HERMES_HOME with the Olo plugin and one
   business built by the real worker (identity, extraction with a scripted
   extractor, ledger, projection to a loopback Hindsight).  The customer has
   four orders: ORD-1 confirmed after a confirmed change (its first date is
   superseded), ORD-2 confirmed with a pending customer request, ORD-3
   cancelled, ORD-4 simply confirmed.  Hindsight also holds deliberately stale
   observations about ORD-1/2/3 and one about another customer's order.
2. **Turn.**  The ordinary Hermes ``AIAgent`` with its default tool surface
   (the Olo tool is discovered through Hermes's plugin loading and tool-search
   bridge), its built-in ``deepseek`` provider and model ``deepseek-flash``,
   every request through ``metered.MeteredUpstream`` (hard call and spend
   caps).  No other model, no other agent loop.
3. **Judgement** against SQLite (``olo.ledger``), never against the tool's
   own output: the tool must have been called; its memory must hold nothing
   of another customer; the answer's JSON summary must match the ledger for
   every order; and no sentence may present a superseded date, or a pending
   request's date, as confirmed.

Offline (``--upstream`` a scripted loopback model) this runs in the tests; the
live run (``--live --key-file F``) sends real requests and needs its own
approval.  Evidence: ``--out evidence.json`` (no key, no message bodies).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

REPO = Path(__file__).resolve().parents[3]
BIZ, BANK, KEY = "agentturn", "olo-agentturn", "tenant-key-for-agentturn"
ALICE, BOB = "alice@x.com", "bob@y.com"
MODEL = "deepseek-flash"
QUESTION = (
    "I am Alice (alice@x.com). For each of my orders, tell me its current status and its "
    "confirmed delivery date, and mention any change I have asked for that is not confirmed yet. "
    "Look it up with the Olo customer context tool first. End your answer with a ```json block: "
    '{"orders": [{"order": "ORD-n", "status": "scheduled" or "cancelled", '
    '"confirmed_date": "YYYY-MM-DD" or null, "pending_request": "YYYY-MM-DD" or null}]}')
#: Hard ceilings of one turn (the live run may lower, never raise, them).
MAX_CALLS, MAX_USD, MAX_OUTPUT_TOKENS = 5, 0.03, 800
#: Measured offline (2026-11-28): Hermes sends 58-67 KB per request (17 tools and
#: its system prompt), ~15-22k input tokens; a call costs ~$0.005-0.008 at
#: deepseek-flash list prices and a turn needs 2-3 calls.
_HISTORY = ("was", "previously", "originally", "earlier", "before", "no longer", "moved from",
            "changed from", "superseded", "instead of", "from ", "cancel", "old")
_REQUEST = ("request", "asked", "proposed", "pending", "not confirmed", "not yet", "awaiting",
            "unconfirmed", "would like", "wants")
_DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")

ORD1_COMMIT = "Your order ORD-1 is confirmed for delivery on 2024-04-10."
ORD1_REQUEST = "Could we move order ORD-1 from 2024-04-10 to 2024-04-15?"
ORD1_CONFIRM = "Confirmed: order ORD-1 moved from 2024-04-10 to 2024-04-15."
ORD2_COMMIT = "Your order ORD-2 is confirmed for delivery on 2024-04-15."
ORD2_REQUEST = "Could we move order ORD-2 from 2024-04-15 to 2024-04-20?"
ORD3_COMMIT = "Your order ORD-3 is confirmed for delivery on 2024-04-18."
ORD3_CANCEL = "We have cancelled your order ORD-3 that was due on 2024-04-18."
ORD4_COMMIT = "Your order ORD-4 is confirmed for delivery on 2024-04-25."
BOB_ORDER = "Your order ORD-9 is confirmed for delivery on 2024-04-10."
STALE = ("Order ORD-1 is scheduled for delivery on 2024-04-10.",
         "Order ORD-2 has been moved to 2024-04-20.",
         "Order ORD-3 is scheduled for delivery on 2024-04-18.")
OTHER_SCOPE = "Order ORD-9 is scheduled for delivery on 2024-04-10."


def _routes() -> dict[str, str]:
    from tests.olo.test_s5_pipeline import answer
    return {
        ORD1_COMMIT: answer("commit", ORD1_COMMIT.rstrip("."), key="ORD-1", confirmed="2024-04-10"),
        ORD1_REQUEST: answer("request_change", ORD1_REQUEST.rstrip("?"), key="ORD-1",
                             proposed="2024-04-15", prior="2024-04-10"),
        ORD1_CONFIRM: answer("confirm_change", ORD1_CONFIRM.rstrip("."), key="ORD-1",
                             confirmed="2024-04-15", prior="2024-04-10"),
        ORD2_COMMIT: answer("commit", ORD2_COMMIT.rstrip("."), key="ORD-2", confirmed="2024-04-15"),
        ORD2_REQUEST: answer("request_change", ORD2_REQUEST.rstrip("?"), key="ORD-2",
                             proposed="2024-04-20", prior="2024-04-15"),
        ORD3_COMMIT: answer("commit", ORD3_COMMIT.rstrip("."), key="ORD-3", confirmed="2024-04-18"),
        ORD3_CANCEL: answer("cancel", ORD3_CANCEL.rstrip("."), key="ORD-3", prior="2024-04-18"),
        ORD4_COMMIT: answer("commit", ORD4_COMMIT.rstrip("."), key="ORD-4", confirmed="2024-04-25"),
        BOB_ORDER: answer("commit", BOB_ORDER.rstrip("."), key="ORD-9", confirmed="2024-04-10"),
    }


# ── 1. The business ───────────────────────────────────────────────────────────

@contextmanager
def business(home: Path) -> Iterator[Any]:
    """Build the business in *home* (HERMES_HOME must already point there);
    yields the loopback Hindsight, which must outlive the turn."""
    import hermes_yaml as yaml
    from olo.activation import bind_business
    from olo.commitments import CommitmentStageConfig
    from olo.m3.hindsight.client import HindsightClient, HindsightConfig
    from olo.projection import ProjectionConfig, ProjectionLimits
    from olo.runner import run_source
    from olo.store import connect
    from tests.olo.fake_s5_services import FakeHindsight
    from tests.olo.test_s5_pipeline import Adapter, Guard, Model, content

    if os.environ.get("HERMES_HOME") != str(home):
        raise SystemExit("set HERMES_HOME to the disposable home first")
    shutil.copytree(REPO / "olo" / "hermes_plugin", home / "plugins" / "olo",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["olo"]}}), encoding="utf-8")
    (home / "businesses" / BIZ).mkdir(parents=True)
    (home / "businesses" / BIZ / ".env").write_text(f"HINDSIGHT_API_TENANT_API_KEY={KEY}\n", encoding="utf-8")
    (home / "olo").mkdir()
    with FakeHindsight(KEY) as hs:
        registry = home / "hindsight_port_registry.json"
        registry.write_text(json.dumps({BIZ: {"port": hs.port, "bank_id": BANK}}), encoding="utf-8")
        cfg = HindsightConfig.for_business(BIZ, registry_path=str(registry))
        adapter = Adapter()
        adapter.queue.extend([
            content("m1", sent=True, body=ORD1_COMMIT, day=1),
            content("m2", sent=False, body=ORD1_REQUEST, day=2),
            content("m3", sent=True, body=ORD1_CONFIRM, day=3),
            content("m4", sent=True, body=ORD2_COMMIT, day=4, thread="t2"),
            content("m5", sent=False, body=ORD2_REQUEST, day=5, thread="t2"),
            content("m6", sent=True, body=ORD3_COMMIT, day=6, thread="t3"),
            content("m7", sent=True, body=ORD3_CANCEL, day=7, thread="t3"),
            content("m8", sent=True, body=ORD4_COMMIT, day=8, thread="t4"),
            content("m9", sent=True, body=BOB_ORDER, day=9, thread="t9", to=f"Bob <{BOB}>"),
        ])
        db = home / "olo" / "olo.db"
        r = run_source(adapter, db_path=db,
                       commitment=CommitmentStageConfig(model_fn=Model(_routes())),
                       projection=ProjectionConfig(client=HindsightClient(cfg), bank_id=BANK,
                                                   usage_guard=Guard(), limits=ProjectionLimits()))
        if r.status != "succeeded":
            raise RuntimeError(f"building the business failed: {r.error}")
        conn = connect(db)
        bind_business(conn, BIZ, "2026-11-28T00:00:00Z")
        conn.commit()
        alice = _contact(conn, ALICE)
        bob = _contact(conn, BOB)
        conn.close()
        hs.recall_extra = ([{"type": "observation", "tags": [f"ent:{alice}"], "text": t} for t in STALE]
                           + [{"type": "observation", "tags": [f"ent:{bob}"], "text": OTHER_SCOPE}])
        yield hs


def _contact(conn, email: str) -> str:
    return conn.execute("SELECT contact_id FROM contact_identifiers WHERE identifier_value=?",
                        (email,)).fetchone()[0]


def ledger_truth(db: Path, email: str = ALICE) -> dict[str, dict]:
    """Per order, from SQLite: status, confirmed date, pending request, superseded dates."""
    from olo.ledger import get_all_versions, get_current_state
    from olo.store import connect
    conn = connect(db)
    try:
        contact = _contact(conn, email)
        out = {}
        for cid, key in conn.execute("SELECT id, business_key FROM commitments WHERE contact_id=?",
                                     (contact,)):
            st = get_current_state(conn, cid) or {}
            confirmed = [v["confirmed_date"] for v in get_all_versions(conn, cid)
                         if v["is_confirmed"] and v["confirmed_date"]]
            current = st.get("scheduled_date")
            out[key] = {"status": st.get("status"), "confirmed_date": current,
                        "pending_request": st.get("pending_proposed_date"),
                        "superseded": sorted({d for d in confirmed if d != current})}
        return out
    finally:
        conn.close()


# ── 2. The turn ───────────────────────────────────────────────────────────────

def _write_agent_config(home: Path, base_url: str) -> None:
    from tests.e2e.core.history._helpers import NO_BACKGROUND_REVIEW, OFFLINE_CONFIG
    from tests.fakes.fake_llm_provider import OFFER_VERSION
    (home / "config.yaml").write_text(
        "model:\n  provider: deepseek\n"
        f"  base_url: {base_url}\n  default: {MODEL}\n  context_length: 128000\n"
        "agent:\n  api_max_retries: 0\n"
        "telemetry:\n  shared_metrics:\n    enabled: false\n    send: false\n"
        f"    offer_version: {OFFER_VERSION}\n"
        "plugins:\n  enabled:\n  - olo\n" + OFFLINE_CONFIG + NO_BACKGROUND_REVIEW, encoding="utf-8")


def agent_turn(home: Path, meter, question: str = QUESTION, *, max_iterations: int = 4) -> dict:
    """One ordinary Hermes turn; returns the final response and the error, if any."""
    from hermes_cli import plugins
    from run_agent import AIAgent
    from tests.olo.live_agent.metered import PLACEHOLDER_KEY
    _write_agent_config(home, meter.base_url)
    plugins.discover_plugins()
    agent = AIAgent(provider="deepseek", base_url=meter.base_url, api_key=PLACEHOLDER_KEY, model=MODEL,
                    session_id="olo-live-agent-turn", quiet_mode=True, platform="cli", skip_memory=True,
                    max_iterations=max_iterations, max_tokens=meter.max_output_tokens)
    try:
        result = agent.run_conversation(question, conversation_history=[], task_id="olo-live-agent-turn")
    finally:
        agent.close()
    return {"final_response": result.get("final_response") or "",
            "error": result.get("error") or None, "completed": bool(result.get("completed", True))}


# ── 3. Judgement ──────────────────────────────────────────────────────────────

def _briefs(requests: list[dict]) -> list[dict]:
    """Every olo_customer_context result the model was shown (tool messages)."""
    found = []
    for req in requests:
        for m in req["messages"]:
            if m.get("role") == "tool" and "olo commitment ledger" in str(m.get("content")):
                found.extend(_find(m["content"]))
    return found


def _find(obj: Any) -> list[dict]:
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return []
    if isinstance(obj, dict):
        if obj.get("authority") == "olo commitment ledger (olo.db)":
            return [obj]
        return [b for v in obj.values() for b in _find(v)]
    if isinstance(obj, list):
        return [b for v in obj for b in _find(v)]
    return []


def _summary(text: str) -> Optional[dict]:
    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    try:
        return json.loads(blocks[-1]) if blocks else None
    except ValueError:
        return None


def _sentences(text: str) -> list[str]:
    prose = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+", prose) if s.strip()]


def judge(response: str, truth: dict[str, dict], requests: list[dict]) -> dict:
    """Problems found in the turn (empty = passed), with the evidence for each."""
    problems: list[str] = []
    briefs = _briefs(requests)
    if not briefs:
        problems.append("the model never received an olo_customer_context result")
    leaked = [b for b in briefs if "ORD-9" in json.dumps(b.get("memory") or {})]
    if leaked:
        problems.append("the tool's memory carried another customer's data")
    support = [m.get("text") or "" for b in briefs for m in (b.get("memory") or {}).get("supporting") or []]
    if any(s in t for s in STALE for t in support):
        problems.append("a stale Hindsight observation was offered as supporting memory")
    summary = _summary(response)
    stated = {o.get("order"): o for o in (summary or {}).get("orders") or []} if summary else {}
    if summary is None:
        problems.append("no ```json summary in the answer")
    for key, want in truth.items():
        got = stated.get(key)
        if summary is not None and got is None:
            problems.append(f"{key}: missing from the summary")
            continue
        for field in ("status", "confirmed_date", "pending_request"):
            if got is not None and got.get(field) != want[field]:
                problems.append(f"{key}: summary {field}={got.get(field)!r}, ledger {want[field]!r}")
    superseded = {d for w in truth.values() for d in w["superseded"]}
    pending = {w["pending_request"] for w in truth.values() if w["pending_request"]}
    current = {w["confirmed_date"] for w in truth.values() if w["confirmed_date"]}
    for s in _sentences(response):
        low = s.lower()
        for d in _DATE.findall(s):
            if d in superseded and d not in current and not any(m in low for m in _HISTORY):
                problems.append(f"superseded date {d} stated as current: {s[:160]!r}")
            if d in pending and d not in current and not any(m in low for m in _REQUEST):
                problems.append(f"pending request {d} stated as confirmed: {s[:160]!r}")
    return {"passed": not problems, "problems": problems, "tool_results": len(briefs),
            "summary": summary,
            "memory_seen": [{**{k: [m.get("label") for m in (b.get("memory") or {}).get(k) or []]
                                for k in ("supporting", "no_longer_true", "uncertain")},
                             "stale_marked_no_longer_true": sum(
                                 1 for m in (b.get("memory") or {}).get("no_longer_true") or []
                                 if (m.get("text") or "") in STALE),
                             "excluded_other_scope": (b.get("memory") or {}).get("excluded_other_scope"),
                             "authoritative": (b.get("memory") or {}).get("authoritative")}
                            for b in briefs]}


def tool_invocations(requests: list[dict]) -> list[dict]:
    """The assistant's tool calls as the provider saw them (names and arguments)."""
    calls = []
    for req in requests:
        for m in req["messages"]:
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                calls.append({"name": fn.get("name"), "arguments": fn.get("arguments")})
    seen, out = set(), []
    for c in calls:
        k = json.dumps(c, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def run_turn(home: Path, upstream: str, api_key: str, *, max_calls: int = MAX_CALLS,
             max_usd: float = MAX_USD, max_output_tokens: int = MAX_OUTPUT_TOKENS) -> dict:
    """Build, run, judge; the evidence document (no key, no message bodies)."""
    from olo.m3.hindsight.budget import COST_INPUT_PER_M, COST_OUTPUT_PER_M
    from tests.olo.live_agent.metered import MeteredUpstream
    if not (1 <= max_calls <= MAX_CALLS and 0 < max_usd <= MAX_USD and max_output_tokens <= MAX_OUTPUT_TOKENS):
        raise SystemExit(f"caps may only be lowered: calls<={MAX_CALLS}, usd<={MAX_USD},"
                         f" output<={MAX_OUTPUT_TOKENS}")
    with business(home):
        truth = ledger_truth(home / "olo" / "olo.db")
        with MeteredUpstream(upstream, api_key=api_key, model=MODEL, max_calls=max_calls,
                             max_usd=max_usd, max_output_tokens=max_output_tokens,
                             price_in_per_m=COST_INPUT_PER_M, price_out_per_m=COST_OUTPUT_PER_M) as meter:
            turn = agent_turn(home, meter)
            verdict = judge(turn["final_response"], truth, meter.requests)
            if turn["error"]:
                verdict["problems"].append(f"the turn ended with an error: {str(turn['error'])[:200]}")
                verdict["passed"] = False
            return {"model": MODEL, "provider": "deepseek (Hermes built-in)", "question": QUESTION,
                    "ledger": truth, "tool_invocations": tool_invocations(meter.requests),
                    "response": turn["final_response"], "turn_error": turn["error"],
                    "verdict": verdict, "accounting": meter.report(),
                    "requests": [{"stream": r["stream"], "max_tokens": r["max_tokens"],
                                  "tools_offered": len(r["tools"])} for r in meter.requests]}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="olo-live-agent-turn")
    ap.add_argument("--home", required=True, help="a new, empty directory (the disposable HERMES_HOME)")
    ap.add_argument("--out", required=True, help="evidence JSON")
    ap.add_argument("--live", action="store_true", help="send real requests (needs its own approval)")
    ap.add_argument("--key-file", help="live: owner-only DeepSeek key file")
    ap.add_argument("--upstream", default="https://api.deepseek.com/v1")
    ap.add_argument("--max-calls", type=int, default=MAX_CALLS)
    ap.add_argument("--max-usd", type=float, default=MAX_USD)
    ap.add_argument("--max-output-tokens", type=int, default=MAX_OUTPUT_TOKENS)
    args = ap.parse_args(argv)
    home = Path(args.home)
    if home.exists() and any(home.iterdir()):
        raise SystemExit(f"{home} is not empty: the turn needs a fresh disposable home")
    if not args.live and "deepseek.com" in args.upstream:
        raise SystemExit("the real provider needs --live (and its approval)")
    key = "sk-offline"
    if args.live:
        from olo.activation import load_key_file
        if not args.key_file:
            raise SystemExit("--live needs --key-file")
        key = load_key_file(args.key_file)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HERMES_HOME"] = str(home)
    evidence = run_turn(home, args.upstream, key, max_calls=args.max_calls, max_usd=args.max_usd,
                        max_output_tokens=args.max_output_tokens)
    Path(args.out).write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"passed": evidence["verdict"]["passed"], "problems": evidence["verdict"]["problems"],
                      "accounting": evidence["accounting"]}, indent=2))
    return 0 if evidence["verdict"]["passed"] else 1


if __name__ == "__main__":
    sys.path.insert(0, str(REPO))
    raise SystemExit(main())
