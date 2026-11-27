"""Batch 5 live driver: Olo against the real pinned Hindsight 1.2.1.

Run by run_live.sh as olo_hs inside a private network namespace, with
HERMES_HOME set to a disposable home that the real setup.sh prepared
(provider none).  Every stage records what was verified against the real
server and what (if anything) stayed simulated; the model is never called:
the commitment stage gets a model function that refuses, and only Calendar
data (structured documents) is projected.

Usage: python driver.py --out evidence.json
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

BIZ, BANK = "b5live", "olo-b5live"
OWNER = "owner@b5live.example"
ALICE, BOB = "alice@customer.example", "bob@other.example"


class NoModel:
    """The commitment stage's model: calendar commitments never call it."""
    model_id = "refuses"
    max_output_tokens = 1

    def __call__(self, prompt):
        raise RuntimeError("a model call was attempted; batch 5 live runs no inference")


class Evidence:
    def __init__(self, out: Path) -> None:
        self.out, self.stages, self.t0 = out, [], time.monotonic()

    def stage(self, name: str, ok: bool, live: str, **detail) -> None:
        row = {"stage": name, "ok": bool(ok), "verified_against": live,
               "t_s": round(time.monotonic() - self.t0, 1), **detail}
        self.stages.append(row)
        print(json.dumps(row, default=str), flush=True)
        self.save()
        if not ok:
            raise SystemExit(f"stage {name} failed")

    def save(self) -> None:
        self.out.write_text(json.dumps({"run_at": datetime.now(UTC).isoformat(),
                                        "stages": self.stages}, indent=2, default=str),
                            encoding="utf-8")


def now() -> str:
    from olo.signals import format_utc
    return format_utc(datetime.now(UTC))


def event(eid: str, day: str, updated: str, guest: str, hour: int = 15) -> dict:
    return {"kind": "calendar#event", "id": eid, "status": "confirmed", "summary": "Site visit",
            "created": "2027-02-01T09:00:00.000Z", "updated": updated, "iCalUID": f"{eid}@google.com",
            "start": {"dateTime": f"{day}T{hour:02d}:00:00-05:00"},
            "end": {"dateTime": f"{day}T{hour + 1:02d}:00:00-05:00"},
            "organizer": {"email": OWNER, "self": True},
            "attendees": [{"email": OWNER, "self": True, "organizer": True, "responseStatus": "accepted"},
                          {"email": guest, "responseStatus": "accepted"}]}


class Queue:
    source = "gcal_b5live"

    def __init__(self) -> None:
        self.queue, self.n = [], 0

    def fetch(self, cursor, *, limit):
        from olo.adapters import FetchBatch
        batch, self.queue = self.queue, []
        self.n += 1
        return FetchBatch(signals=batch, has_more=False, next_cursor=f"c{self.n}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ev = Evidence(Path(ap.parse_args().out))
    home = Path(os.environ["HERMES_HOME"])
    try:
        return run(ev, home)
    except SystemExit:
        raise
    except Exception:  # health: allow BLE001 -- recorded with its traceback as a failed stage
        ev.stage("unexpected_error", False, "n/a", error=traceback.format_exc()[-2000:])
        return 1
    finally:
        try:
            from olo.hindsight_service import stop
            from olo.paths import db_path
            from olo.store import connect
            stop(connect(db_path()), now(), reason="live run teardown")
        except Exception:  # health: allow BLE001 -- teardown is best effort; the home is removed
            print("teardown: could not stop the server cleanly", file=sys.stderr)
            traceback.print_exc()


def run(ev: Evidence, home: Path) -> int:
    import psutil

    from olo.activation import bind_business
    from olo.adapters.gcal import _event_to_signal
    from olo.commitments import CommitmentStageConfig
    from olo.hindsight_reconcile import reconcile_bank
    from olo.hindsight_service import configure, load_spec, probe, service_status, start, supervise_once
    from olo.m3.hindsight.budget import UsageGuard
    from olo.m3.hindsight.client import HindsightAPIError, HindsightClient, HindsightConfig
    from olo.paths import db_path
    from olo.projection import ProjectionConfig, ProjectionLimits, desired_documents
    from olo.runner import run_source
    from olo.store import connect
    from olo.store import migrate as migrate_store

    conn = connect(db_path())
    migrate_store(conn)
    bind_business(conn, BIZ, now())
    conn.commit()
    configure(conn, BIZ, str(home / "olo" / "hindsight-venv"), now(),
              hf_home="/var/cache/huggingface")
    spec = load_spec(conn, home)
    ev.stage("configure", True, "setup.sh registry and .env", port=spec.port, bank=spec.bank_id,
             executable=str(spec.executable), tenant_key_present=bool(spec.api_key))

    # 1. Start through the supervisor; readiness.
    pid = start(conn, spec, now())
    deadline, status = time.monotonic() + 420, ""
    while time.monotonic() < deadline:
        status = supervise_once(conn, spec, datetime.now(UTC))
        if status == "healthy":
            break
        time.sleep(3)
    ev.stage("start_and_ready", status == "healthy", "real hindsight-api 1.2.1",
             pid=pid, supervisor_status=status)

    # 2. Authentication (C4): the probe requires anonymous 401 and key accepted.
    cfg = HindsightConfig(host="127.0.0.1", port=spec.port, api_key=spec.api_key, bank_id=BANK)
    wrong = HindsightClient(HindsightConfig(host="127.0.0.1", port=spec.port, api_key="not-the-key",
                                            bank_id=BANK))
    try:
        wrong.get_bank_config(bank_id=BANK)
        wrong_status = 200
    except HindsightAPIError as exc:
        wrong_status = exc.status_code
    ev.stage("tenant_auth", probe(spec).status == "healthy" and wrong_status == 401,
             "real server", probe=probe(spec).status, wrong_key_status=wrong_status)

    client = HindsightClient(cfg)
    q = Queue()
    fetched = datetime(2027, 2, 1, tzinfo=UTC)

    def project(*resources, rounds: int = 25) -> dict:
        q.queue.extend(_event_to_signal(r, q.source, fetched, "primary") for r in resources)
        last: dict = {}
        for _ in range(rounds):
            r = run_source(q, db_path=db_path(),
                           commitment=CommitmentStageConfig(model_fn=NoModel()),
                           projection=ProjectionConfig(
                               client=client, bank_id=BANK,
                               usage_guard=UsageGuard(client, bank_id=BANK, budget_usd=0.01),
                               limits=ProjectionLimits(max_docs_per_run=50, max_prose_docs_per_run=1)))
            last = {"status": r.status, "commitment": r.commitment, "projection": r.projection}
            pending = conn.execute("SELECT COUNT(*) FROM hindsight_projection WHERE bank_id=?"
                                   " AND status!='confirmed'", (BANK,)).fetchone()[0]
            if r.status == "succeeded" and pending == 0 and not r.projection.get("operations_in_flight"):
                in_flight = conn.execute("SELECT COUNT(*) FROM hindsight_operations WHERE bank_id=?"
                                         " AND status='submitted'", (BANK,)).fetchone()[0]
                if in_flight == 0:
                    return last
            time.sleep(3)
        return {**last, "unsettled": True}

    def server_docs() -> dict[str, dict]:
        from olo.hindsight_reconcile import server_documents
        return server_documents(client, BANK) or {}

    def wanted() -> dict[str, dict]:
        return {d["document_id"]: d for d in desired_documents(conn, q.source)}

    # 3. First projection: two customers, structured documents only.
    r = project(event("e1", "2027-03-10", "2027-02-01T10:00:00.000Z", ALICE),
                event("e2", "2027-03-10", "2027-02-01T10:05:00.000Z", BOB))
    held, want = server_docs(), wanted()
    ev.stage("first_projection", not r.get("unsettled") and set(want) <= set(held),
             "real retain + listing", run=r, desired=sorted(want), held=sorted(held))

    # 4. Metadata and text as the server stores them (open Batch 4 risk).
    sample = min(d for d in want if d.startswith("ledger:"))
    got = client.get_document(sample, bank_id=BANK)
    listing_meta = {d: bool((held.get(d) or {}).get("document_metadata")) for d in want}
    meta_equal = {d: _meta_eq((client.get_document(d, bank_id=BANK) or {}).get("document_metadata"),
                              want[d].get("metadata")) for d in want}
    ev.stage("metadata_consistency", True, "real get_document / listing",
             sample=sample, text_stored=got.get("original_text") == want[sample]["content"],
             listing_has_metadata=listing_meta, get_document_metadata_equal=meta_equal,
             tags_equal={d: set(held[d].get("tags") or []) == set(want[d]["tags"]) for d in want})

    # 5. Replacement: a reschedule supersedes ledger v1 in place.
    r = project(event("e1", "2027-03-12", "2027-02-02T10:00:00.000Z", ALICE))
    held, want = server_docs(), wanted()
    # Alice's rescheduled commitment (calendar event e1), not Bob's unchanged one.
    e1_cid = conn.execute("SELECT id FROM commitments WHERE business_key='gcal:primary:e1'"
                          ).fetchone()[0]
    v1 = f"ledger:{e1_cid}:v1"
    v1_tags = set(held.get(v1, {}).get("tags") or [])
    v1_doc = client.get_document(v1, bank_id=BANK) or {}
    v1_text = v1_doc.get("original_text") or ""
    outbox = conn.execute("SELECT p.status, p.operation_id, o.status, o.submitted_at, o.settled_at"
                          " FROM hindsight_projection p LEFT JOIN hindsight_operations o"
                          " ON o.operation_id = p.operation_id WHERE p.signal_id=?", (v1,)).fetchone()
    ops = conn.execute("SELECT o.operation_id, o.status, o.submitted_at FROM hindsight_operation_docs d"
                       " JOIN hindsight_operations o ON o.operation_id=d.operation_id"
                       " WHERE d.document_id=? ORDER BY o.submitted_at", (v1,)).fetchall()
    ev.stage("document_replacement", "ledger:superseded" in v1_tags and v1_text.startswith("SUPERSEDED")
             and set(want) <= set(held), "real retain upsert",
             run=r, v1=v1, v1_tags=sorted(v1_tags), held_count=len(held), desired_count=len(want),
             desired_v1_tags=sorted(want[v1]["tags"]), desired_v1_prefix=want[v1]["content"][:60],
             server_get_tags=sorted(v1_doc.get("tags") or []), server_text_prefix=v1_text[:60],
             server_updated_at=v1_doc.get("updated_at"), outbox=tuple(outbox or ()),
             operations=[tuple(o) for o in ops])

    # 6. Forced termination and supervised restart; data persists.
    before = set(held)
    old = service_status(conn)["pid"]
    os.kill(old, signal.SIGKILL)
    psutil.Process(old).wait(15) if psutil.pid_exists(old) else None
    first = supervise_once(conn, spec, datetime.now(UTC))
    deadline, status = time.monotonic() + 420, first
    while time.monotonic() < deadline and status != "healthy":
        time.sleep(3)
        status = supervise_once(conn, spec, datetime.now(UTC))
    held = server_docs()
    ev.stage("forced_restart", first == "restarting" and status == "healthy" and before <= set(held),
             "real process kill -9 + supervisor", killed_pid=old, new_pid=service_status(conn)["pid"],
             after_kill=first, final=status, documents_persisted=before <= set(held))

    # 7. Reconciliation after the restart (triggered by the supervisor's event).
    r = project()
    rows = conn.execute("SELECT trigger, status, report FROM hindsight_reconciliations"
                        " ORDER BY id DESC LIMIT 1").fetchone()
    ev.stage("reconcile_after_restart", rows is not None and rows[0] == "restart" and rows[1] == "ok",
             "real listing", reconciliation=dict(zip(("trigger", "status", "report"), tuple(rows or ()))))

    # 8. Partial write: a document disappears from the bank.
    lost = min(d for d in want if d.startswith("sig:"))
    client.delete_document(lost, bank_id=BANK)
    rep = reconcile_bank(conn, client, BANK, now(), trigger="operator")
    r = project()
    ev.stage("partial_write_recovery", lost in rep.missing and lost in server_docs(),
             "real delete + listing + retain", lost=lost, report=rep.summary(), run=r)

    # 9. Drift: an older copy (still tagged current) lands last.
    stale = json.loads(json.dumps(wanted()[v1]))
    stale["tags"] = [t if t != "ledger:superseded" else "ledger:current" for t in stale["tags"]]
    op = str(uuid.uuid4())
    client.retain([stale], operation_id=op, async_=False, bank_id=BANK)
    rep = reconcile_bank(conn, client, BANK, now(), trigger="operator")
    r = project()
    fixed = set(server_docs().get(v1, {}).get("tags") or [])
    ev.stage("drift_recovery", v1 in rep.drifted and "ledger:superseded" in fixed,
             "real retain + listing", report=rep.summary(), tags_after=sorted(fixed))

    # 10. Bank loss: the bank is deleted; olo.db rebuilds it.
    count = len(server_docs())
    client.delete_bank(BANK)
    r = project()
    held = server_docs()
    ev.stage("bank_loss_recovery", set(wanted()) <= set(held), "real delete_bank + rebuild",
             documents_before=count, documents_after=len(held), run=r)

    # 11. The Hermes tool over real recall (plugin discovery + handle_function_call).
    plugins = home / "plugins"
    plugins.mkdir(exist_ok=True)
    if not (plugins / "olo").exists():
        (plugins / "olo").symlink_to(REPO / "olo" / "hermes_plugin", target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled:\n  - olo\n", encoding="utf-8")
    import model_tools
    brief = json.loads(model_tools.handle_function_call(
        "olo_customer_context", {"customer": ALICE, "question": "site visit date"},
        task_id="live", session_id="live"))
    current = {c["key"]: c for c in brief.get("current", [])}
    e1 = current.get("gcal:primary:e1", {})
    groups = {g: [m["text"][:80] for m in brief["memory"][g]] for g in
              ("supporting", "no_longer_true", "unconfirmed", "uncertain")}
    ev.stage("tool_over_real_recall",
             e1.get("date") == "2027-03-12" and brief["memory"]["status"] == "ok"
             and any(t.startswith("SUPERSEDED") for t in groups["no_longer_true"])
             and not any("bob" in json.dumps(brief).lower() for _ in [0]),
             "real recall (local embeddings)", current=list(current), date=e1.get("date"),
             start=e1.get("start"), memory_groups=groups)

    # 12. A real Hermes agent turn (scripted loopback model, no inference).
    from run_agent import AIAgent
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
    script = [ToolCall("tool_call", {"calls": [{"name": "olo_customer_context",
                                                "arguments": {"customer": ALICE}}]}),
              Text("Your site visit is on 2027-03-12.")]
    with FakeLLMServer(script) as srv:
        write_hermes_home(home, srv.base_url, extra_config=(
            "plugins:\n  enabled:\n  - olo\nupdates:\n  check: false\n"
            "models_dev:\n  url: http://127.0.0.1:9/api.json\n"
            "skills:\n  creation_nudge_interval: 0\nmemory:\n  nudge_interval: 0\n"))
        agent = AIAgent(provider="custom", base_url=srv.base_url, api_key="sk-fake",
                        model="fake-model", session_id="b5live", quiet_mode=True,
                        platform="cli", skip_memory=True)
        try:
            result = agent.run_conversation("When is my site visit?", conversation_history=[],
                                            task_id="live")
        finally:
            agent.close()
        reqs = srv.main_requests()
    tool_msgs = [m for m in reqs[1]["messages"] if m.get("role") == "tool"] if len(reqs) > 1 else []
    ok = bool(tool_msgs) and "2027-03-12" in tool_msgs[0]["content"]
    ev.stage("agent_turn", ok and result.get("final_response") == "Your site visit is on 2027-03-12.",
             "real AIAgent loop + real Hindsight recall; model scripted (loopback, no inference)",
             model_requests=len(reqs))

    # 13. The installed schedule: `hermes olo setup --schedule`, then the
    #     supervisor's cron job recovers a killed server via `hermes cron run`.
    import subprocess
    hermes = str(Path(sys.executable).with_name("hermes"))
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(REPO)}

    def cli(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([hermes, *argv], capture_output=True, text=True, env=env,
                              check=False, timeout=600)

    setup = cli("olo", "setup", "--source", "synthetic", "--enable-projection",
                "--business-id", BIZ, "--bank-id", BANK, "--projection-max-docs", "50",
                "--projection-max-prose-docs", "1", "--projection-budget-usd", "0.01",
                "--hindsight-registry", str(home / "hindsight_port_registry.json"),
                "--hindsight-venv", str(home / "olo" / "hindsight-venv"),
                "--hindsight-hf-home", "/var/cache/huggingface", "--schedule", "--force")
    from cron.jobs import list_jobs
    jobs = {j["name"]: j for j in list_jobs(include_disabled=True)}
    sup = jobs.get(f"olo-hindsight-{BIZ}") or {}
    old = service_status(conn)["pid"]
    os.kill(old, signal.SIGKILL)
    ran = cli("cron", "run", sup.get("id", "?"))
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline and probe(spec).status != "healthy":
        time.sleep(3)
    again = cli("cron", "run", sup.get("id", "?"))
    new_pid = service_status(conn)["pid"]
    ev.stage("installed_cron_supervisor",
             setup.returncode == 0 and _succeeded(ran.stdout)
             and probe(spec).status == "healthy" and new_pid not in (None, old)
             and _succeeded(again.stdout),
             "real Hermes cron + installed shim + real server",
             setup_exit=setup.returncode, setup_stderr=setup.stderr[-400:],
             job=sup.get("name"), schedule=sup.get("schedule_display"),
             interpreter=sup.get("interpreter"), killed_pid=old, new_pid=new_pid,
             first_run=ran.stdout.strip()[-120:], second_run=again.stdout.strip()[-120:],
             status_lines=[ln for ln in cli("olo", "status").stdout.splitlines()
                           if "Hindsight" in ln or "hindsight" in ln])
    return 0


def _succeeded(cron_output: str) -> bool:
    """`hermes cron run` reports a synchronous run as "Ran now: <status>"."""
    return any(f"Ran now: {word}" in cron_output for word in ("succeeded", "completed"))


def _meta_eq(got, want) -> str:
    if not got:
        return "absent"
    return "equal" if {k: str(v) for k, v in got.items()} == {k: str(v) for k, v in (want or {}).items()} \
        else "different"


if __name__ == "__main__":
    raise SystemExit(main())
