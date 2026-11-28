"""The acceptance scenarios as checks against olo.db.

Every scenario names the manual action that precedes it (done by hand in the
test accounts' Gmail and Calendar UIs, or by a runner command) and which
identities it needs.  Test data is tagged: an email subject or event title
carries ``[OLO-<ID>]`` so a check finds exactly its own data.  With a run id
(``Ctx.run_id``; the seeder's subjects and titles end in ``acc <run id>``) a
check sees only that run's data, so a diagnostic or repeated run in the same
home cannot mix into an earlier run's verdict.

Verdicts are point in time.  ``evaluate`` reuses a verdict already recorded
for the same run, scenario and action (``frozen``) unless the scenario is
re-checked explicitly, so later unrelated activity (more scenarios, the
unattended E2 window, cleanup) cannot rewrite it; run.py keeps every verdict
in an append-only log.  A scenario's ``version`` names its criterion: a
verdict recorded under an older criterion is never reused (E2 version 2
measures the unattended window itself).

Each scenario also has an evidence class, which says what a pass proves:

  api              Google's behaviour is verified for real.  Any external
                   party in the data is a seeded stand-in that the tested
                   assumption does not depend on (label history, deletion,
                   pagination, the business's own Calendar writes).
  simulated        the assumption depends on how another party's mail client
                   composes a message (quoting, charset, attachments).  Seeded
                   through the seeder project, a pass proves Olo against
                   Google-stored synthetic MIME, not against that client.
  second_identity  only a genuine second party can produce the data (a real
                   invitation and its responses, a real Outlook client).

``evaluate`` turns every scenario into one outcome, never a skip:

  passed            the check held
  passed_simulated  the check held on seeded data for a ``simulated`` scenario
  failed            the check ran and did not hold (details say why)
  not_run           its action is not recorded yet (``run.py action <ID>``)
  needs_approval    mode B (paid inference) without that approval
  needs_identity    a ``second_identity`` scenario without an action recorded
                    ``--by real-identity``: not tested, never passed
  error             the check itself raised (recorded with the error)

The checks encode the plan's "Expected" column (GOOGLE_LIVE_ACCEPTANCE_PLAN.md)
and are never edited during a run: a mismatch is a failed result, recorded
with what Google actually returned.
"""
from __future__ import annotations

import hashlib
import json
from itertools import pairwise
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from google_acceptance import real_identity

EMAIL_KINDS = ("email.received", "email.sent", "email.content")
CAL_KINDS = ("calendar.event.changed", "calendar.event.cancelled")


@dataclass
class Ctx:
    conn: sqlite3.Connection
    account: str
    customer: str
    actions: dict[str, dict]
    mode: str = "A"
    facts: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""                     # "" = unscoped (runs before run ids)
    now: Optional[datetime] = None       # None = the wall clock (tests pin it)
    outlook: str = ""                    # the Outlook.com identity (G2 provenance)

    def clock(self) -> datetime:
        return self.now or datetime.now(UTC)


@dataclass(frozen=True)
class Scenario:
    id: str
    area: str
    title: str
    action: str
    identities: str                      # account | account+customer | …+outlook | local
    check: Callable[[Ctx], tuple[bool, dict]]
    mode: str = "A"
    evidence: str = "api"                # api | simulated | second_identity
    version: int = 1                     # the check's criterion; bumped when it changes


@dataclass(frozen=True)
class Outcome:
    id: str
    status: str
    details: dict
    checked_at: str = ""                 # when the check ran ("" = no check ran)
    action_at: str = ""                  # the action it judged
    fresh: bool = False                  # evaluated now, not reused
    version: int = 1                     # the criterion version it was judged by


# ── olo.db helpers ────────────────────────────────────────────────────────────

def _tagged(ctx: Ctx, tag: str, kinds=EMAIL_KINDS) -> list[dict]:
    marks = ",".join("?" * len(kinds))
    run = f"%acc {ctx.run_id}%" if ctx.run_id else "%"
    rows = ctx.conn.execute(
        f"SELECT source, external_id, kind, subject, payload FROM signals WHERE kind IN ({marks})"
        " AND subject LIKE ? AND subject LIKE ? ORDER BY occurred_at, id",
        (*kinds, f"%[OLO-{tag}]%", run)).fetchall()
    return [{"source": r[0], "sid": r[1], "kind": r[2], "subject": r[3],
             "payload": json.loads(r[4]) if isinstance(r[4], str) else dict(r[4] or {})} for r in rows]


def _content(ctx: Ctx, tag: str) -> list[dict]:
    return [s for s in _tagged(ctx, tag) if s["kind"] == "email.content"]


def _authored(ctx: Ctx, sig: dict) -> str:
    from olo.signal_content import load_prepared_signal
    return load_prepared_signal(ctx.conn, sig["source"], sig["sid"]).authored_text


def _resolution(ctx: Ctx, sig: dict) -> dict:
    row = ctx.conn.execute("SELECT * FROM signal_resolution WHERE signal_id=? AND source=?",
                           (sig["sid"], sig["source"])).fetchone()
    return dict(row) if row else {}


def _contact_emails(ctx: Ctx, contact_id: Optional[str]) -> set[str]:
    return {r[0] for r in ctx.conn.execute(
        "SELECT identifier_value FROM contact_identifiers WHERE contact_id=? AND identifier_type='email'"
        " AND revoked_at IS NULL", (contact_id,))}


def _labels(ctx: Ctx, message_id: str, kind: str) -> list[list[str]]:
    rows = ctx.conn.execute("SELECT payload FROM signals WHERE kind=?"
                            " AND json_extract(payload, '$.message_id')=?", (kind, message_id))
    return [json.loads(r[0]).get("labels") or [] for r in rows]


def _state(ctx: Ctx, sig: dict) -> str:
    from olo.message_state import state_of
    return state_of(ctx.conn, sig["source"], sig["payload"].get("message_id") or "")


def _outcomes(ctx: Ctx, sigs: list[dict]) -> list[str]:
    return [_resolution(ctx, s).get("commitment_outcome") for s in sigs]


def _versions_for_key(ctx: Ctx, key: str) -> list[dict]:
    from olo.ledger import alias_target, get_all_versions
    row = ctx.conn.execute("SELECT id FROM commitments WHERE business_key=?", (key,)).fetchone()
    cid = row[0] if row else alias_target(ctx.conn, key)
    return get_all_versions(ctx.conn, cid) if cid else []


def _event_key(sig: dict) -> str:
    from olo.calendar_commitments import event_key
    return event_key(sig["payload"].get("calendar_id") or "", sig["payload"].get("event_id") or "")


def _dupes(ctx: Ctx, kind: str) -> int:
    return ctx.conn.execute(
        "SELECT COUNT(*) FROM (SELECT json_extract(payload,'$.message_id') m, COUNT(*) n FROM signals"
        " WHERE kind=? GROUP BY m HAVING n > 1)", (kind,)).fetchone()[0]


# ── Checks ────────────────────────────────────────────────────────────────────

def g1(ctx):
    sigs = _content(ctx, "G1")
    received = [s for s in sigs if s["payload"].get("direction") == "received"]
    sent = [s for s in sigs if s["payload"].get("direction") == "sent"]
    who = {e for s in received for e in _contact_emails(ctx, _resolution(ctx, s).get("contact_id"))}
    # Both directions (version 2): the business's reply and a customer's reply
    # to the business keep only what their author wrote, never the quote.
    replies = sent + [s for s in received if s["payload"].get("in_reply_to")]
    quoted = [s["sid"] for s in replies if any(line.lstrip().startswith(">") or "wrote:" in line
                                               for line in _authored(ctx, s).splitlines())]
    return (bool(received) and bool(sent) and ctx.customer in who and not quoted,
            {"received": len(received), "sent": len(sent), "customer_replies": len(replies) - len(sent),
             "customer_identified": ctx.customer in who, "quoted_in_authored": quoted})


def g2(ctx):
    sigs = [s for s in _content(ctx, "G2") if s["payload"].get("direction") == "received"]
    leaked = [s["sid"] for s in sigs if any(line.strip().lower().startswith(("from:", "sent:"))
                                            for line in _authored(ctx, s).splitlines())]
    return bool(sigs) and not leaked, {"messages": len(sigs), "header_block_in_authored": leaked}


def g3(ctx):
    sigs = _content(ctx, "G3")
    texts = {s["sid"]: _authored(ctx, s) for s in sigs}
    bad = [k for k, t in texts.items()
           if "Café déjà vu" not in t or "<" in t or "font-family" in t.lower()]
    return bool(sigs) and not bad, {"messages": len(sigs), "unreadable_or_markup": bad}


def g4(ctx):
    sigs = _content(ctx, "G4")
    leaked = [s["sid"] for s in sigs if "OLO-ATTACHMENT-TEXT" in _authored(ctx, s)]
    with_refs = [s["sid"] for s in sigs if s["payload"].get("attachments")]
    return bool(sigs) and not leaked and bool(with_refs), {
        "messages": len(sigs), "attachment_text_in_authored": leaked, "with_attachment_refs": len(with_refs)}


def g5(ctx):
    sigs = _content(ctx, "G5")
    ok = []
    for s in sigs:
        mid = s["payload"].get("message_id") or ""
        removed = _labels(ctx, mid, "email.labels_removed")
        ok.append({"active": _state(ctx, s) == "active",
                   "spam_removed": any("SPAM" in lbls for lbls in removed)})
    return bool(sigs) and all(o["active"] and o["spam_removed"] for o in ok), {"messages": ok}


def g6(ctx):
    restored, deleted = _content(ctx, "G6a"), _content(ctx, "G6b")
    states = {"G6a": [_state(ctx, s) for s in restored], "G6b": [_state(ctx, s) for s in deleted]}
    cancels = [v for s in deleted for v in ctx.conn.execute(
        "SELECT 1 FROM commitment_versions WHERE action='cancel' AND source_doc_id=?",
        (f"gmail:{s['payload'].get('message_id')}",))]
    return (states["G6a"] == ["active"] * len(restored) and bool(restored)
            and states["G6b"] == ["deleted"] * len(deleted) and bool(deleted) and not cancels,
            {"states": states, "ledger_cancels_from_deletion": len(cancels)})


def g7(ctx):
    sigs = _content(ctx, "G7")
    ctx.facts["G7_sent_message_ids"] = [s["payload"].get("message_id") for s in sigs]
    return len(sigs) == 1, {"captured": len(sigs), "draft_saves_not_captured": len(sigs) <= 1}


def g8(ctx):
    sigs = _content(ctx, "G8")
    mid = sigs[0]["payload"].get("message_id") if sigs else ""
    added = _labels(ctx, mid, "email.labels_added") + _labels(ctx, mid, "email.labels_removed")
    return len(sigs) == 1 and bool(added), {"content_copies": len(sigs), "label_signals": len(added)}


def g9(ctx):
    sigs = _content(ctx, "G9")
    ids = {s["payload"].get("message_id") for s in sigs}
    pages = int(ctx.facts.get("history_pages", 0))
    return len(sigs) >= 12 and len(ids) == len(sigs) and pages >= 2, {
        "messages": len(sigs), "distinct": len(ids), "history_pages_seen": pages}


def g10(ctx):
    health = ctx.conn.execute("SELECT status FROM pipeline_health WHERE stage='ingest'"
                              " AND source LIKE 'gmail_%'").fetchall()
    return (_dupes(ctx, "email.content") == 0 and [r[0] for r in health] == ["ok"],
            {"duplicate_messages": _dupes(ctx, "email.content"), "ingest": [r[0] for r in health]})


def _cal(ctx, tag):
    return _tagged(ctx, tag, CAL_KINDS)


def c1(ctx):
    sigs = _cal(ctx, "C1")
    versions = _versions_for_key(ctx, _event_key(sigs[0])) if sigs else []
    return (bool(versions) and versions[0]["action"] == "commit" and bool(versions[0]["confirmed_start"]),
            {"versions": [(v["action"], v["confirmed_date"], v["confirmed_start"]) for v in versions]})


def c2(ctx):
    sigs = _cal(ctx, "C2")
    vs = _versions_for_key(ctx, _event_key(sigs[0])) if sigs else []
    moved_day = any(v["action"] == "confirm_change" and v["confirmed_date"] != v["prior_confirmed_date"]
                    for v in vs)
    moved_time = any(v["action"] == "confirm_change" and v["confirmed_date"] == v["prior_confirmed_date"]
                     for v in vs)
    return moved_day and moved_time, {"versions": [(v["action"], v["confirmed_date"],
                                                    v["prior_confirmed_date"], v["confirmed_start"])
                                                   for v in vs]}


def c3(ctx):
    sigs = _cal(ctx, "C3")
    outcomes = _outcomes(ctx, sigs)
    return "no_change" in outcomes and outcomes.count("applied") == 1, {"outcomes": outcomes}


def c4(ctx):
    outcomes = _outcomes(ctx, _cal(ctx, "C4"))
    want = ["no_commitment", "applied", "applied"]
    return outcomes[:3] == want, {"outcomes": outcomes, "expected_prefix": want}


def _series(ctx: Ctx, tag: str) -> list[dict]:
    """The tagged series and every revision of its occurrences.  Google only
    guarantees id, recurringEventId and originalStartTime on a cancelled
    occurrence, so occurrences are found through the series, not the title."""
    tagged = _cal(ctx, tag)
    series = {s["payload"].get("event_id") for s in tagged if not s["payload"].get("recurring_event_id")}
    marks = ",".join("?" * len(series)) or "''"
    rows = ctx.conn.execute(
        f"SELECT source, external_id, kind, subject, payload FROM signals WHERE kind IN (?, ?)"
        f" AND json_extract(payload, '$.recurring_event_id') IN ({marks}) ORDER BY occurred_at, id",
        (*CAL_KINDS, *series)).fetchall()
    seen = {s["sid"] for s in tagged}
    extra = [{"source": r[0], "sid": r[1], "kind": r[2], "subject": r[3], "payload": json.loads(r[4])}
             for r in rows if r[1] not in seen]
    return tagged + extra


def c5(ctx):
    outcomes = _outcomes(ctx, _series(ctx, "C5"))
    return ("recurring_series" in outcomes and "applied" in outcomes and "no_commitment" in outcomes,
            {"outcomes": outcomes})


def c6(ctx):
    sigs = _cal(ctx, "C6")
    vs = _versions_for_key(ctx, _event_key(sigs[0])) if sigs else []
    stub = ctx.conn.execute("SELECT COUNT(*) FROM signals WHERE kind='calendar.event.cancelled'"
                            " AND json_extract(payload,'$.event_id')=?",
                            (sigs[0]["payload"].get("event_id") if sigs else "",)).fetchone()[0]
    return bool(vs) and vs[-1]["action"] == "cancel" and stub >= 1, {
        "versions": [v["action"] for v in vs], "cancel_stubs": stub}


def c7(ctx):
    dup = ctx.conn.execute(
        "SELECT COUNT(*) FROM (SELECT source_doc_id, COUNT(*) n FROM commitment_versions"
        " GROUP BY source_doc_id HAVING n > 1)").fetchone()[0]
    health = [r[0] for r in ctx.conn.execute("SELECT status FROM pipeline_health WHERE stage='ingest'"
                                             " AND source LIKE 'gcal_%'")]
    return dup == 0 and health == ["ok"], {"duplicate_versions": dup, "ingest": health}


def c8(ctx):
    sigs = _cal(ctx, "C8")
    events = {s["payload"].get("event_id") for s in sigs}
    pages = int(ctx.facts.get("calendar_pages", 0))
    return len(events) >= 12 and pages >= 2, {"events": len(events), "event_pages_seen": pages}


def l1(ctx):
    from olo.signal_content import load_prepared_signal
    invites = _content(ctx, "L1")
    events = _cal(ctx, "L1")
    uids = {u for s in invites for u in load_prepared_signal(ctx.conn, s["source"], s["sid"]).ical_uids}
    ical = {s["payload"].get("ical_uid") for s in events}
    linked = bool(uids & ical)
    details = {"invitation_uids": sorted(uids), "event_uids": sorted(x for x in ical if x),
               "uid_matches": linked}
    if ctx.mode == "B":
        from olo.ledger import alias_target
        details["linked_commitments"] = sorted({alias_target(ctx.conn, f"ical:{u}") for u in uids} - {None})
        return linked and len(details["linked_commitments"]) == 1, details
    return linked, details


def l2(ctx):
    rows = ctx.conn.execute("SELECT reason FROM unresolved_matches WHERE reason LIKE '%no invitation UID%'"
                            ).fetchall()
    return bool(rows), {"unresolved_same_day": len(rows)}


def l3(ctx):
    from olo.ledger import alias_target, get_current_state
    events = _cal(ctx, "L1")
    if not events:
        return False, {"events": 0}
    key = _event_key(events[0])
    row = ctx.conn.execute("SELECT id FROM commitments WHERE business_key=?", (key,)).fetchone()
    cid = row[0] if row else alias_target(ctx.conn, key)
    state = get_current_state(ctx.conn, cid) if cid else None
    return bool(state) and state["status"] == "cancelled", {"status": (state or {}).get("status")}


def d1(ctx):
    """The rerun window (run.py rerun records the signal ids around it) holds
    no second copy of anything stored before it: the same message (email
    kinds), event revision (calendar kinds) or ledger source document.  New
    mail or edits arriving meanwhile are legitimate; later ingestion is
    outside the window and never counts."""
    w = ctx.facts.get("rerun") or {}
    if "max_signal_id_before" not in w or "max_signal_id_after" not in w:
        return False, {"reason": "no rerun window recorded (run.py rerun records it)"}
    lo, hi = int(w["max_signal_id_before"]), int(w["max_signal_id_after"])
    e_marks, c_marks = ",".join("?" * len(EMAIL_KINDS)), ",".join("?" * len(CAL_KINDS))
    email = ctx.conn.execute(
        f"SELECT COUNT(*) FROM signals n WHERE n.id > ? AND n.id <= ? AND n.kind IN ({e_marks})"
        " AND EXISTS (SELECT 1 FROM signals o WHERE o.id <= ? AND o.kind = n.kind AND o.source = n.source"
        "  AND json_extract(o.payload,'$.message_id') = json_extract(n.payload,'$.message_id'))",
        (lo, hi, *EMAIL_KINDS, lo)).fetchone()[0]
    cal = ctx.conn.execute(
        f"SELECT COUNT(*) FROM signals n WHERE n.id > ? AND n.id <= ? AND n.kind IN ({c_marks})"
        " AND EXISTS (SELECT 1 FROM signals o WHERE o.id <= ? AND o.source = n.source"
        "  AND json_extract(o.payload,'$.calendar_id') = json_extract(n.payload,'$.calendar_id')"
        "  AND json_extract(o.payload,'$.event_id') = json_extract(n.payload,'$.event_id')"
        "  AND json_extract(o.payload,'$.updated') = json_extract(n.payload,'$.updated'))",
        (lo, hi, *CAL_KINDS, lo)).fetchone()[0]
    versions = ctx.conn.execute(
        "SELECT COUNT(*) FROM (SELECT source_doc_id, COUNT(*) n FROM commitment_versions"
        " GROUP BY source_doc_id HAVING n > 1)").fetchone()[0]
    return email == 0 and cal == 0 and versions == 0, {
        "window": [lo, hi], "inserted_during_rerun": hi - lo, "duplicate_messages": email,
        "duplicate_event_revisions": cal, "duplicate_ledger_versions": versions}


def e1(ctx):
    from cron.jobs import list_jobs
    names = {j["name"]: j for j in list_jobs(include_disabled=True)}
    want = [n for n in names if n.startswith(("olo-gmail-", "olo-gcal-", "olo-hindsight-"))]
    pinned = all(names[n].get("interpreter") for n in want)
    return len(want) == 3 and pinned, {"jobs": sorted(want), "interpreters_pinned": pinned}


#: E2: the unattended window the plan requires, and the longest a scheduled
#: job may go without a run inside it (twice Calendar's 15-minute interval).
E2_WINDOW = timedelta(hours=2)
E2_MAX_GAP = timedelta(minutes=30)
#: The installed jobs E2 watches (E1 installs them): ``olo-<kind>-<name>``.
#: A worker job drives the Olo source ``<kind>_<name>``; the Hindsight
#: supervisor drives no source.
E2_WORKER_KINDS = ("gmail", "gcal")
E2_JOB_KINDS = (*E2_WORKER_KINDS, "hindsight")
#: Fired by the scheduler (``hermes cron tick`` or the gateway ticker); a
#: ``hermes cron run`` fire is ``direct`` and a skipped slot is ``missed``.
_SCHEDULED = "builtin"


def _instant(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _e2_jobs() -> dict[str, dict]:
    """The installed Olo jobs by kind (cron's own job store, this HERMES_HOME)."""
    from cron.jobs import list_jobs
    jobs: dict[str, dict] = {}
    for job in list_jobs(include_disabled=True):
        parts = (job.get("name") or "").split("-", 2)
        if len(parts) == 3 and parts[0] == "olo" and parts[1] in E2_JOB_KINDS:
            jobs.setdefault(parts[1], {**job, "olo_source": f"{parts[1]}_{parts[2]}"
                                       if parts[1] in E2_WORKER_KINDS else None})
    return jobs


def _e2_executions(job_id: str, start: datetime) -> list[dict]:
    """The job's execution attempts since *start* (cron's durable audit ledger)."""
    from cron.executions import list_executions
    return [e for e in list_executions(job_id=job_id, limit=500) if _instant(e["claimed_at"]) >= start]


def _gap(start: datetime, times: list[datetime], now: datetime) -> float:
    marks = [start, *sorted(times), now]
    return round(max(b - a for a, b in pairwise(marks)).total_seconds() / 60, 1)


def _e2_job(ctx: Ctx, job: dict, start: datetime, now: datetime) -> dict:
    """One installed job across the window: what the scheduler fired, and for a
    worker, whether each fire really polled its source (an ingest run inside
    the execution) and whether any run happened outside a scheduled fire."""
    execs = _e2_executions(job["id"], start)
    fired = [e for e in execs if e["source"] == _SCHEDULED and e["status"] == "completed"]
    spans = [(_instant(e["started_at"] or e["claimed_at"]),
              _instant(e["finished_at"]) if e["finished_at"] else now) for e in fired]
    out = {"job": job["name"], "enabled": job.get("enabled", True) is not False,
           "scheduled_completed": len(fired),
           "scheduled_failed": sum(1 for e in execs if e["source"] == _SCHEDULED
                                   and e["status"] in ("failed", "unknown", "cancelled")),
           "missed": sum(1 for e in execs if e["status"] == "missed"),
           "manual_fires": sum(1 for e in execs if e["source"] != _SCHEDULED and e["status"] != "missed"),
           "max_gap_min": _gap(start, [a for a, _ in spans], now),
           "errors": sorted({(e["error"] or "")[:160] for e in execs if e["error"]})[:3]}
    source = job["olo_source"]
    if source is None:
        return out
    runs = [(_instant(r[0]), r[1]) for r in ctx.conn.execute(
        "SELECT started_at, status FROM ingest_runs WHERE source=?", (source,))]
    runs = [(t, s) for t, s in runs if t >= start]
    inside = [any(a <= t <= b for a, b in spans) for t, _ in runs]
    out.update({"source": source, "ingest_runs": len(runs),
                "unscheduled_runs": inside.count(False),
                "failed_runs": sum(1 for _, s in runs if s not in ("succeeded", "partial")),
                "fires_without_a_run": sum(1 for a, b in spans if not any(a <= t <= b for t, _ in runs))})
    return out


def _e2_job_ok(j: dict) -> bool:
    limit = E2_MAX_GAP.total_seconds() / 60
    ok = (j["enabled"] and j["scheduled_completed"] and j["max_gap_min"] <= limit
          and not (j["scheduled_failed"] or j["missed"] or j["manual_fires"]))
    if "source" in j:
        ok = ok and j["ingest_runs"] and not (j["unscheduled_runs"] or j["failed_runs"]
                                              or j["fires_without_a_run"])
    return bool(ok)


def _e2_model(ctx: Ctx) -> dict:
    """Mode A's model is off on purpose: extraction ``disabled`` is expected,
    an exhausted budget is not (the first live Mode A run's 21 failed fires)."""
    out = {}
    for src, detail in ctx.conn.execute(
            "SELECT source, detail FROM pipeline_health WHERE stage='commitment'"):
        d = json.loads(detail or "{}")
        counts = d.get("counts", d) if isinstance(d, dict) else {}
        out[src] = {"extraction_disabled": int(counts.get("extraction_disabled", 0) or 0),
                    "budget_exhausted": int(counts.get("budget_exhausted", 0) or 0)}
    return out


def _e2_canary(ctx: Ctx, start: datetime, spans_by_source: dict[str, list]) -> dict:
    """The mid-window canary (seeder ``E2`` phase 1): a new message reached
    olo.db through a *scheduled* run, so the pipeline progressed end to end
    rather than merely staying alive."""
    sigs = [s for s in _content(ctx, "E2") if s["payload"].get("direction") == "received"]
    if not sigs:
        return {"seen": False}
    row = ctx.conn.execute(
        "SELECT s.ingested_at, r.started_at, r.source FROM signals s JOIN ingest_runs r ON r.id = s.run_id"
        " WHERE s.source=? AND s.external_id=?", (sigs[0]["source"], sigs[0]["sid"])).fetchone()
    ran_at = _instant(row[1])
    scheduled = any(a <= ran_at <= b for a, b in spans_by_source.get(row[2], []))
    return {"seen": True, "ingested_at": row[0], "after_start": ran_at >= start,
            "by_scheduled_run": scheduled, "identity": _resolution(ctx, sigs[0]).get("identity_status")}


def e2(ctx):
    """Unattended for two hours through the installed scheduler.

    Measured, never assumed (criterion version 3):

    * **window**: at least ``E2_WINDOW`` since the action;
    * **scheduler, not a person**: every installed Olo job (gmail, gcal,
      Hindsight supervisor) was fired by the scheduler (cron's execution
      ledger, source ``builtin``) with no gap over ``E2_MAX_GAP``, and none
      failed, was missed or was fired by hand (``direct``);
    * **real polling**: every scheduled worker fire produced an ingest run of
      its source, and no ingest run happened outside a scheduled fire (a
      manually invoked worker); health queries create neither;
    * **progress, not survival**: the mid-window canary message was ingested
      by a scheduled run;
    * **model**: an exhausted extraction budget fails (a disabled model is
      Mode A's intent and is reported as such);
    * **now**: nothing degraded or stalled, calendar coverage complete.

    Version 2 counted any ingest run, so a manually invoked worker satisfied
    it; version 1 looked only at the conditions at check time.
    """
    from olo.health import assess
    now = ctx.clock()
    start = _instant(ctx.actions.get("E2", {}).get("at") or now.isoformat())
    conditions = [(c.severity, c.stage, c.message) for c in assess(ctx.conn, now)]
    bad = [c for c in conditions if c[0] in ("degraded", "stalled")]
    jobs = _e2_jobs()
    detail = {kind: _e2_job(ctx, jobs[kind], start, now) for kind in E2_JOB_KINDS if kind in jobs}
    spans = {}
    for kind in E2_WORKER_KINDS:
        if kind in jobs:
            fired = [e for e in _e2_executions(jobs[kind]["id"], start)
                     if e["source"] == _SCHEDULED and e["status"] == "completed"]
            spans[jobs[kind]["olo_source"]] = [
                (_instant(e["started_at"] or e["claimed_at"]),
                 _instant(e["finished_at"]) if e["finished_at"] else now) for e in fired]
    model = _e2_model(ctx)
    canary = _e2_canary(ctx, start, spans)
    elapsed = now - start
    missing = [k for k in E2_JOB_KINDS if k not in jobs]
    ok = (elapsed >= E2_WINDOW and not missing and not bad
          and all(_e2_job_ok(j) for j in detail.values())
          and not any(m["budget_exhausted"] for m in model.values())
          and canary.get("seen") and canary.get("after_start") and canary.get("by_scheduled_run"))
    return bool(ok), {"window_min": round(elapsed.total_seconds() / 60, 1),
                      "required_min": E2_WINDOW.total_seconds() / 60, "missing_jobs": missing,
                      "jobs": detail, "model": model, "canary": canary, "conditions": conditions}


# ── B1 (mode B): a delivery through paid extraction ──────────────────────────

#: The ledger history B1's five messages must produce, in order:
#: (action, actor, confirmed).  A customer's request and a customer's cancel
#: are recorded unconfirmed; only the business's messages become truth.
B1_SEQUENCE = (("commit", "business", True), ("request_change", "customer", False),
               ("confirm_change", "business", True), ("cancel", "customer", False),
               ("cancel", "business", True))


def _fp(value: object) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:10]


def _b1_commitment(ctx: Ctx) -> tuple[list[dict], Optional[str]]:
    sigs = _content(ctx, "B1")
    ids = {_resolution(ctx, s).get("commitment_id") for s in sigs} - {None}
    return sigs, (next(iter(ids)) if len(ids) == 1 else None)


def snapshot_b1(ctx: Ctx) -> dict:
    """B1's ledger state now (run.py snapshot B1, after each phase's sync)."""
    from olo.ledger import get_all_versions, get_current_state
    sigs, cid = _b1_commitment(ctx)
    st = (get_current_state(ctx.conn, cid) or {}) if cid else {}
    return {"messages": len(sigs), "commitment": _fp(cid) if cid else None,
            "versions": len(get_all_versions(ctx.conn, cid)) if cid else 0,
            **{k: st.get(k) for k in ("status", "scheduled_date", "pending_proposed_date",
                                      "pending_action")}}


SNAPSHOTS: dict[str, Callable[[Ctx], dict]] = {"B1": snapshot_b1}


def _b1_invariants(snaps: list[dict], vs: list[dict]) -> list[str]:
    """What the point-in-time snapshots after each phase must show."""
    if len(snaps) != len(B1_SEQUENCE) or len(vs) != len(B1_SEQUENCE):
        return [f"{len(snaps)} snapshots and {len(vs)} versions for {len(B1_SEQUENCE)} phases"]
    d1, d2 = vs[0]["confirmed_date"], vs[2]["confirmed_date"]
    want = [
        ("after the commitment", snaps[0], {"status": "scheduled", "scheduled_date": d1,
                                            "pending_proposed_date": None}),
        ("after the customer's request", snaps[1], {"status": "scheduled", "scheduled_date": d1,
                                                    "pending_proposed_date": d2}),
        ("after the confirmed change", snaps[2], {"status": "scheduled", "scheduled_date": d2,
                                                  "pending_proposed_date": None}),
        ("after the customer's cancel request", snaps[3], {"status": "scheduled",
                                                           "scheduled_date": d2,
                                                           "pending_action": "cancel"}),
        ("after the business's cancellation", snaps[4], {"status": "cancelled"}),
    ]
    out = [f"{label}: {k}={snap.get(k)!r}, want {v!r}"
           for label, snap, expect in want for k, v in expect.items() if snap.get(k) != v]
    if d1 == d2:
        out.append("the change did not move the date")
    return out


def _b1_tool_output(ctx: Ctx) -> dict:
    """The Hermes tool's answer (olo_customer_context, the production handler)."""
    from olo.hermes_plugin import customer_context_tool
    return json.loads(customer_context_tool({"customer": ctx.customer,
                                             "question": "deliveries and their dates"}))


def b1(ctx):
    """Customer request, business confirmation, confirmed change, cancellation,
    through the production extractor, ledger, projection and Hermes tool."""
    from olo.ledger import get_all_versions
    sigs, cid = _b1_commitment(ctx)
    vs = get_all_versions(ctx.conn, cid) if cid else []
    sequence = [(v["action"], v["actor_role"], bool(v["is_confirmed"])) for v in vs]
    problems = _b1_invariants(ctx.facts.get("snapshots", {}).get("B1", []), vs)
    if sequence != [tuple(x) for x in B1_SEQUENCE]:
        problems.append(f"ledger history {sequence}")
    docs = {r[0]: r[1] for r in ctx.conn.execute(
        "SELECT signal_id, status FROM hindsight_projection WHERE signal_id LIKE ?",
        (f"ledger:{cid}:v%",))} if cid else {}
    confirmed_versions = [v["version"] for v in vs if v["is_confirmed"]]
    if [docs.get(f"ledger:{cid}:v{n}") for n in confirmed_versions] != ["confirmed"] * len(confirmed_versions):
        problems.append(f"ledger memory documents {docs}")
    tool = _b1_tool_output(ctx)
    entry = next((c for c in tool.get("current") or [] if c.get("commitment_id") == cid), None)
    if tool.get("identity", {}).get("status") != "verified" or not entry \
            or entry.get("status") != "cancelled":
        problems.append(f"tool output: identity {tool.get('identity', {}).get('status')}, entry {entry}")
    used = ctx.facts.get("mode_b") or {}
    cap = (used.get("caps") or {}).get("calls", 0)
    if not used or used.get("calls_used", 0) > cap:
        problems.append(f"model usage {used} outside its caps")
    ctx.facts["B1_tool_output"] = json.loads(
        json.dumps(tool, default=str).replace(ctx.account, "<business account>"))
    return not problems, {
        "messages": [{"kind": s["payload"].get("direction"), "message": _fp(s["payload"].get("message_id")),
                      "source_doc": _fp(f"gmail:{s['payload'].get('message_id')}"),
                      "outcome": _resolution(ctx, s).get("commitment_outcome")} for s in sigs],
        "commitment": _fp(cid) if cid else None,
        "versions": [{"v": v["version"], "action": v["action"], "actor": v["actor_role"],
                      "confirmed": bool(v["is_confirmed"]), "date": v["confirmed_date"],
                      "proposed": v["proposed_date"], "prior": v["prior_confirmed_date"],
                      "source_doc": _fp(v["source_doc_id"])} for v in vs],
        "memory_documents": {k.rsplit(":", 1)[-1]: v for k, v in sorted(docs.items())},
        "tool_current": {k: entry.get(k) for k in ("status", "scheduled_date")} if entry else None,
        "model_usage": {k: used.get(k) for k in ("calls_used", "failed_calls", "tokens_used",
                                                 "cost_usd", "caps")},
        "problems": problems}


def e3(ctx):
    from olo.agent_context import customer_brief
    brief = customer_brief(ctx.conn, ctx.customer, "meetings")
    return (brief["identity"]["status"] == "verified" and bool(brief.get("current")),
            {"identity": brief["identity"]["status"], "commitments": len(brief.get("current") or []),
             "memory": (brief.get("memory") or {}).get("status")})


def e4(ctx):
    rows = [r[0] for r in ctx.conn.execute(
        "SELECT event FROM hindsight_service_events WHERE at >= ? ORDER BY id",
        (ctx.actions.get("E4", {}).get("at", ""),))]
    return "restarted" in rows, {"events_since_action": rows}


def e5(ctx):
    since = ctx.actions.get("E5", {}).get("at", "")
    ok = ctx.conn.execute("SELECT COUNT(*) FROM pipeline_health WHERE stage='ingest' AND status='ok'"
                          " AND last_success_at >= ?", (since,)).fetchone()[0]
    return ok >= 2, {"sources_ingesting_since_reboot": ok}


def e6(ctx):
    rows = [r[0] for r in ctx.conn.execute(
        "SELECT needs_operator FROM pipeline_health WHERE stage='ingest' AND needs_operator IS NOT NULL")]
    return any("oauth" in (r or "") for r in rows), {"needs_operator": rows}


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("G1", "gmail", "sent and received, plain reply", "customer emails the business"
             " '[OLO-G1] order question'; business replies from Gmail web", "account+customer", g1,
             evidence="simulated", version=2),
    Scenario("G2", "gmail", "Outlook HTML reply", "an Outlook.com identity replies to a business"
             " email with subject '[OLO-G2] …'", "account+customer+outlook", g2, evidence="second_identity"),
    Scenario("G3", "gmail", "HTML-only and non-UTF-8", "send '[OLO-G3] …' HTML-only and as"
             " ISO-8859-1 (SMTP client), body containing 'Café déjà vu'", "account+customer", g3, evidence="simulated"),
    Scenario("G4", "gmail", "attachments", "customer sends '[OLO-G4] …' with a .txt attachment"
             " containing OLO-ATTACHMENT-TEXT and a forwarded .eml", "account+customer", g4, evidence="simulated"),
    Scenario("G5", "gmail", "spam rescue", "customer's '[OLO-G5] …' to Spam, then 'Not spam'",
             "account+customer", g5),
    Scenario("G6", "gmail", "trash, restore, delete", "trash and restore '[OLO-G6a] …'; trash"
             " and delete forever '[OLO-G6b] …'", "account+customer", g6),
    Scenario("G7", "gmail", "draft then send", "save, edit and send a draft '[OLO-G7] …'", "account", g7),
    Scenario("G8", "gmail", "archive, star, read", "archive, star and read '[OLO-G8] …'", "account", g8),
    Scenario("G9", "gmail", "pagination", "12 messages '[OLO-G9] n' with jobs at --limit 5",
             "account+customer", g9),
    Scenario("G10", "gmail", "expired history cursor", "run.py expire-cursor gmail, then a sync",
             "local", g10),
    Scenario("C1", "calendar", "event created", "business creates '[OLO-C1] visit' and invites"
             " the customer", "account+customer", c1),
    Scenario("C2", "calendar", "rescheduled, then same-day time change", "move '[OLO-C2] …' to"
             " another day, then to another time that day", "account", c2),
    Scenario("C3", "calendar", "title-only edit", "edit only the title/description of '[OLO-C3] …'",
             "account", c3),
    Scenario("C4", "calendar", "invitation responses", "customer invites the business to"
             " '[OLO-C4] …'; business: no response, accept, then decline", "account+customer", c4, evidence="second_identity"),
    Scenario("C5", "calendar", "recurring exceptions", "weekly '[OLO-C5] …'; move one occurrence,"
             " cancel another", "account+customer", c5),
    Scenario("C6", "calendar", "deleted event", "delete '[OLO-C6] …'", "account+customer", c6),
    Scenario("C7", "calendar", "expired sync token", "run.py expire-cursor gcal, then a sync",
             "local", c7),
    Scenario("C8", "calendar", "pagination", "12 events '[OLO-C8] n' with jobs at --limit 5",
             "account", c8),
    Scenario("L1", "linking", "invitation UID", "the customer invites the business to '[OLO-L1] …'"
             " from Google Calendar; Google's own invitation email (with its iCalendar UID) reaches the"
             " business's Gmail (real_identity.plan)", "account+customer", l1, evidence="second_identity"),
    Scenario("L2", "linking", "same day without UID", "business emails '[OLO-L2] …' confirming"
             " the L1 meeting by date only", "account+customer", l2, mode="B"),
    Scenario("L3", "linking", "cancel after linking", "the customer (organizer) cancels the [OLO-L1] event",
             "account+customer", l3, evidence="second_identity"),
    Scenario("B1", "mode B", "request, confirmation, change, cancellation", "five-message thread"
             " '[OLO-B1] delivery of ORD-…': business commits; customer asks to move it; business"
             " confirms the move; customer asks to cancel; business cancels (run.py snapshot B1"
             " after each phase's sync)", "account+customer", b1, mode="B"),
    Scenario("D1", "recovery", "duplicate delivery", "run.py rerun (both jobs twice back to back)",
             "local", d1),
    Scenario("E1", "installed", "three jobs installed", "hermes olo setup … --schedule for gmail"
             " and gcal", "local", e1),
    Scenario("E2", "installed", "unattended for 2 hours", "let the installed scheduler run 2 hours"
             " untouched; seed the E2 canary (seeder E2 phase 1) about an hour in",
             "local", e2, version=3),
    Scenario("E3", "installed", "customer context", "run.py check E3 (the tool's brief)",
             "local", e3),
    Scenario("E4", "installed", "Hindsight killed", "kill -9 the Hindsight server", "local", e4),
    Scenario("E5", "installed", "reboot", "reboot (or wsl --shutdown) and wait 10 minutes",
             "local", e5),
    Scenario("E6", "installed", "OAuth revoked (last)", "revoke the grant in the Google account",
             "account", e6),
)


def evaluate(ctx: Ctx, only: Optional[set[str]] = None, *,
             frozen: Optional[dict[str, Outcome]] = None,
             recheck: frozenset[str] = frozenset(), now: str = "") -> list[Outcome]:
    """One outcome per scenario.  ``frozen`` maps a scenario to the verdict
    recorded for this run; it is reused while it judged the scenario's
    current action and the scenario is not in ``recheck``."""
    frozen = frozen or {}
    out: list[Outcome] = []
    for sc in SCENARIOS:
        if only and sc.id not in only:
            continue
        act = ctx.actions.get(sc.id)
        real = bool(act) and act.get("by") == "real-identity"
        if sc.evidence == "second_identity" and not real:
            out.append(Outcome(sc.id, "needs_identity", {
                "reason": "needs a genuine second identity; seeded data cannot stand in",
                "action": sc.action}))
            continue
        if sc.mode == "B" and ctx.mode != "B":
            out.append(Outcome(sc.id, "needs_approval", {"reason": "mode B (paid inference)"}))
            continue
        if act is None:
            out.append(Outcome(sc.id, "not_run", {"action": sc.action}))
            continue
        prior = frozen.get(sc.id)
        if (prior is not None and prior.action_at == act.get("at", "") and prior.version == sc.version
                and sc.id not in recheck):
            out.append(prior)
            continue
        try:
            ok, details = sc.check(ctx)
        except Exception as exc:  # health: allow BLE001 -- a broken check is a recorded result
            out.append(Outcome(sc.id, "error", {"error": f"{type(exc).__name__}: {exc}"[:400]},
                               now, act.get("at", ""), True, sc.version))
            continue
        details = {**details, "evidence": sc.evidence, "by": act.get("by", "operator")}
        if real and sc.id in real_identity.PROVENANCE:
            genuine, provenance = real_identity.PROVENANCE[sc.id](ctx)
            details["provenance"] = {"genuine": genuine, **provenance}
            ok = ok and genuine            # seeded data recorded as real never passes
        status = "passed" if ok else "failed"
        if ok and sc.evidence == "simulated" and not real:
            status = "passed_simulated"
        out.append(Outcome(sc.id, status, details, now, act.get("at", ""), True, sc.version))
    return out


def verdict(outcomes: list[Outcome]) -> tuple[str, int]:
    """``(verdict, exit code)``.

    ``rejected`` (1) on any failure or error; ``incomplete`` (2) while any
    scenario is not run or awaits approval; ``accepted_within_scope`` (4) when
    everything else passed but some scenarios passed only on seeded data or
    need a second identity (they are listed, never counted as passed);
    ``accepted`` (0) only when every scenario passed for real.
    """
    statuses = {o.status for o in outcomes}
    if statuses & {"failed", "error"}:
        return "rejected", 1
    if statuses & {"not_run", "needs_approval"}:
        return "incomplete", 2
    if statuses & {"passed_simulated", "needs_identity"}:
        return "accepted_within_scope", 4
    return "accepted", 0
