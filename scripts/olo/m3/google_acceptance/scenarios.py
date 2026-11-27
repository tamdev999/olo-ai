"""The acceptance scenarios as checks against olo.db.

Every scenario names the manual action that precedes it (done by hand in the
test accounts' Gmail and Calendar UIs, or by a runner command) and which
identities it needs.  Test data is tagged: an email subject or event title
carries ``[OLO-<ID>]`` so a check finds exactly its own data.

``evaluate`` turns every scenario into one outcome, never a skip:

  passed          the check held
  failed          the check ran and did not hold (details say why)
  not_run         its action is not recorded yet (``run.py action <ID>``)
  needs_approval  mode B (paid inference) without that approval
  error           the check itself raised (recorded with the error)

The checks encode the plan's "Expected" column (GOOGLE_LIVE_ACCEPTANCE_PLAN.md)
and are never edited during a run: a mismatch is a failed result, recorded
with what Google actually returned.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Optional

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


@dataclass(frozen=True)
class Scenario:
    id: str
    area: str
    title: str
    action: str
    identities: str                      # account | account+customer | …+outlook | local
    check: Callable[[Ctx], tuple[bool, dict]]
    mode: str = "A"


@dataclass(frozen=True)
class Outcome:
    id: str
    status: str
    details: dict


# ── olo.db helpers ────────────────────────────────────────────────────────────

def _tagged(ctx: Ctx, tag: str, kinds=EMAIL_KINDS) -> list[dict]:
    marks = ",".join("?" * len(kinds))
    rows = ctx.conn.execute(
        f"SELECT source, external_id, kind, subject, payload FROM signals WHERE kind IN ({marks})"
        " AND subject LIKE ? ORDER BY occurred_at, id", (*kinds, f"%[OLO-{tag}]%")).fetchall()
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
    quoted = [s["sid"] for s in sent if any(line.lstrip().startswith(">") or "wrote:" in line
                                            for line in _authored(ctx, s).splitlines())]
    return (bool(received) and bool(sent) and ctx.customer in who and not quoted,
            {"received": len(received), "sent": len(sent), "customer_identified": ctx.customer in who,
             "quoted_in_authored": quoted})


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


def c5(ctx):
    outcomes = _outcomes(ctx, _cal(ctx, "C5"))
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
    before = ctx.facts.get("signals_before_rerun")
    after = ctx.conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    return before is not None and before == after and _dupes(ctx, "email.content") == 0, {
        "signals_before_rerun": before, "signals_after_rerun": after}


def e1(ctx):
    from cron.jobs import list_jobs
    names = {j["name"]: j for j in list_jobs(include_disabled=True)}
    want = [n for n in names if n.startswith(("olo-gmail-", "olo-gcal-", "olo-hindsight-"))]
    pinned = all(names[n].get("interpreter") for n in want)
    return len(want) == 3 and pinned, {"jobs": sorted(want), "interpreters_pinned": pinned}


def e2(ctx):
    from datetime import UTC, datetime

    from olo.health import assess
    conditions = [(c.severity, c.stage, c.message) for c in assess(ctx.conn, datetime.now(UTC))]
    bad = [c for c in conditions if c[0] in ("degraded", "stalled")]
    return not bad, {"conditions": conditions}


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
             " '[OLO-G1] order question'; business replies from Gmail web", "account+customer", g1),
    Scenario("G2", "gmail", "Outlook HTML reply", "an Outlook.com identity replies to a business"
             " email with subject '[OLO-G2] …'", "account+customer+outlook", g2),
    Scenario("G3", "gmail", "HTML-only and non-UTF-8", "send '[OLO-G3] …' HTML-only and as"
             " ISO-8859-1 (SMTP client), body containing 'Café déjà vu'", "account+customer", g3),
    Scenario("G4", "gmail", "attachments", "customer sends '[OLO-G4] …' with a .txt attachment"
             " containing OLO-ATTACHMENT-TEXT and a forwarded .eml", "account+customer", g4),
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
             " '[OLO-C4] …'; business: no response, accept, then decline", "account+customer", c4),
    Scenario("C5", "calendar", "recurring exceptions", "weekly '[OLO-C5] …'; move one occurrence,"
             " cancel another", "account+customer", c5),
    Scenario("C6", "calendar", "deleted event", "delete '[OLO-C6] …'", "account+customer", c6),
    Scenario("C7", "calendar", "expired sync token", "run.py expire-cursor gcal, then a sync",
             "local", c7),
    Scenario("C8", "calendar", "pagination", "12 events '[OLO-C8] n' with jobs at --limit 5",
             "account", c8),
    Scenario("L1", "linking", "invitation UID", "the customer receives the business's '[OLO-L1] …'"
             " invitation and forwards it back; the event is tagged [OLO-L1]", "account+customer", l1),
    Scenario("L2", "linking", "same day without UID", "business emails '[OLO-L2] …' confirming"
             " the L1 meeting by date only", "account+customer", l2, mode="B"),
    Scenario("L3", "linking", "cancel after linking", "cancel the [OLO-L1] event", "account", l3),
    Scenario("D1", "recovery", "duplicate delivery", "run.py rerun (both jobs twice back to back)",
             "local", d1),
    Scenario("E1", "installed", "three jobs installed", "hermes olo setup … --schedule for gmail"
             " and gcal", "local", e1),
    Scenario("E2", "installed", "unattended for 2 hours", "let the scheduler run 2 hours",
             "local", e2),
    Scenario("E3", "installed", "customer context", "run.py check E3 (the tool's brief)",
             "local", e3),
    Scenario("E4", "installed", "Hindsight killed", "kill -9 the Hindsight server", "local", e4),
    Scenario("E5", "installed", "reboot", "reboot (or wsl --shutdown) and wait 10 minutes",
             "local", e5),
    Scenario("E6", "installed", "OAuth revoked (last)", "revoke the grant in the Google account",
             "account", e6),
)


def evaluate(ctx: Ctx, only: Optional[set[str]] = None) -> list[Outcome]:
    out: list[Outcome] = []
    for sc in SCENARIOS:
        if only and sc.id not in only:
            continue
        if sc.mode == "B" and ctx.mode != "B":
            out.append(Outcome(sc.id, "needs_approval", {"reason": "mode B (paid inference)"}))
            continue
        if sc.id not in ctx.actions:
            out.append(Outcome(sc.id, "not_run", {"action": sc.action}))
            continue
        try:
            ok, details = sc.check(ctx)
        except Exception as exc:  # health: allow BLE001 -- a broken check is a recorded result
            out.append(Outcome(sc.id, "error", {"error": f"{type(exc).__name__}: {exc}"[:400]}))
            continue
        out.append(Outcome(sc.id, "passed" if ok else "failed", details))
    return out


def verdict(outcomes: list[Outcome]) -> tuple[str, int]:
    """``(verdict, exit code)``: failed/error 1, incomplete 2, accepted 0."""
    statuses = {o.status for o in outcomes}
    if statuses & {"failed", "error"}:
        return "rejected", 1
    if statuses & {"not_run", "needs_approval"}:
        return "incomplete", 2
    return "accepted", 0
