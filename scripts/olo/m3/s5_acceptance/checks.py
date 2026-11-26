"""S5 acceptance checks — pure functions over recorded evidence.

Every check returns a :class:`Findings` that keeps three kinds of failure
apart:

* ``ledger``     — olo.db holds the wrong commitment state;
* ``hindsight``  — memory (documents, observations, recall) is wrong;
* ``incomplete`` — processing did not finish (timeouts, unfinished or failed
  operations, pending consolidation, missing observations, spend stops), so
  the other two cannot be judged.

A run passes only when all three lists are empty.  Nothing here calls a
server or a model; the driver gathers the evidence and these functions judge
it, so the judgements can be tested offline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

CATEGORIES = ("ledger", "hindsight", "incomplete")

#: Projection counters that mean work is still running or was refused.
BUSY_FLAGS = ("submitted", "deferred", "consolidation_in_flight", "consolidation_deferred",
              "consolidation_submitted")
#: Projection counters that mean the stage stopped short of finishing.
STOP_FLAGS = ("budget_exceeded", "stats_unavailable", "bank_unprepared", "retries_exhausted",
              "consolidation_retries_exhausted", "consolidation_capped", "consolidation_unknown",
              "consolidation_failed_memories")
#: commitment_status values a finished extraction leaves.
COMMITMENT_SETTLED = ("resolved", "unresolved", "skipped")
#: Words that mark a statement as a request rather than an agreed fact.
REQUEST_MARKERS = re.compile(
    r"\b(request\w*|propos\w*|ask\w*|could we|can we|would like|wants?|wanted|"
    r"suggest\w*|pending|unconfirmed|not (yet )?confirmed|reschedul\w*)\b",
    re.IGNORECASE)


@dataclass
class Findings:
    ledger: list[str] = field(default_factory=list)
    hindsight: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    #: Recorded for the reader, never a pass or a failure (e.g. a retain that
    #: failed once and completed when resubmitted).
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.ledger or self.hindsight or self.incomplete)

    def add(self, other: Findings) -> Findings:
        for c in (*CATEGORIES, "notes"):
            getattr(self, c).extend(getattr(other, c))
        return self

    def as_dict(self) -> dict:
        return {c: list(getattr(self, c)) for c in (*CATEGORIES, "notes")}


# ── Dates as text ─────────────────────────────────────────────────────────────

def date_forms(iso: str) -> list[str]:
    """Ways a text may state *iso* (YYYY-MM-DD): ISO, 'June 15', '15 June',
    'Jun 15', '15th of June', '06/15', '15/06'."""
    d = date.fromisoformat(iso)
    month, short = d.strftime("%B"), d.strftime("%b")
    day = str(d.day)
    forms = [iso]
    for m in (month, short):
        forms += [rf"\b{m}\.? {day}(st|nd|rd|th)?\b", rf"\b{day}(st|nd|rd|th)? (of )?{m}\b"]
    forms += [rf"\b{d.month:02d}/{d.day:02d}\b", rf"\b{d.day:02d}/{d.month:02d}\b"]
    return forms


def mentions(text: str, iso: str) -> bool:
    text = text or ""
    return any((f in text) if i == 0 else bool(re.search(f, text, re.IGNORECASE))
               for i, f in enumerate(date_forms(iso)))


# ── Processing ────────────────────────────────────────────────────────────────

def settle_findings(*, runs: list[dict], outbox: dict, prose_pending, resolution: dict,
                    stopped_by_spend: bool, max_runs: int) -> Findings:
    """Did retains and consolidation finish?  Anything short of that is
    ``incomplete``, never a pass."""
    f = Findings()
    if stopped_by_spend:
        f.incomplete.append("spend stop reached before processing settled")
    if not runs:
        f.incomplete.append("no worker run completed while settling")
        return f
    last = runs[-1]
    if last.get("status") != "succeeded" or last.get("error"):
        f.incomplete.append(f"last worker run: status {last.get('status')!r}, error {last.get('error')!r}")
    proj = last.get("projection") or {}
    busy = [k for k in BUSY_FLAGS if proj.get(k)]
    if busy:
        f.incomplete.append(f"still busy after {len(runs)} of {max_runs} runs: {busy}")
    stops = {k: proj[k] for k in STOP_FLAGS if proj.get(k)}
    if stops:
        f.incomplete.append(f"projection reported {stops}")
    for op in outbox.get("operations") or []:
        line = f"{op.get('kind')} operation {op.get('status')}: {op.get('detail') or ''}"[:200]
        if op.get("status") == "submitted":
            f.incomplete.append(line)
        elif op.get("status") != "completed":
            f.notes.append(line)       # the documents' and prose state below decide
    for doc, status in sorted((outbox.get("documents") or {}).items()):
        if status != "confirmed":
            f.incomplete.append(f"document {doc} projection status {status}")
    if prose_pending is None:
        f.incomplete.append("prose consolidation pending count unavailable")
    elif prose_pending:
        f.incomplete.append(f"{prose_pending} prose memories still pending consolidation")
    for sid, r in sorted(resolution.items()):
        if r.get("identity_status") != "resolved":
            f.incomplete.append(f"{sid}: identity {r.get('identity_status')}")
        elif r.get("commitment_status") not in COMMITMENT_SETTLED:
            f.incomplete.append(
                f"{sid}: commitment extraction {r.get('commitment_status')} after "
                f"{r.get('commitment_attempts')} attempts ({r.get('commitment_outcome')}); "
                "held back from projection")
    return f


# ── Ledger ────────────────────────────────────────────────────────────────────

def _by_type(ledger: dict, event_type: str) -> list[tuple[str, dict]]:
    return [(cid, c) for cid, c in ledger.items() if c.get("event_type") == event_type]


def _version(c: dict, n: int) -> dict:
    return next((v for v in c.get("versions") or [] if v.get("version") == n), {})


def _expect(f: list, label: str, got, want) -> None:
    if got != want:
        f.append(f"{label}: expected {want!r}, got {got!r}")


def ledger_after_request(ledger: dict) -> Findings:
    """Run 1: business commits ORD-501 for 06-10, customer asks for 06-15,
    business confirms a meeting on 06-05."""
    f = Findings()
    deliveries, meetings = _by_type(ledger, "delivery"), _by_type(ledger, "meeting")
    _expect(f.ledger, "delivery commitments", len(deliveries), 1)
    _expect(f.ledger, "meeting commitments", len(meetings), 1)
    if deliveries:
        _, d = deliveries[0]
        _expect(f.ledger, "delivery key", d.get("key"), "ORD-501")
        _expect(f.ledger, "delivery status", d.get("status"), "scheduled")
        _expect(f.ledger, "delivery confirmed date", d.get("scheduled_date"), "2026-06-10")
        _expect(f.ledger, "delivery pending action", d.get("pending_action"), "request_change")
        _expect(f.ledger, "delivery pending proposed date", d.get("pending_proposed_date"), "2026-06-15")
        _expect(f.ledger, "delivery versions", len(d.get("versions") or []), 2)
        v1, v2 = _version(d, 1), _version(d, 2)
        _expect(f.ledger, "delivery v1", (v1.get("actor_role"), v1.get("action"), v1.get("is_confirmed"),
                                          v1.get("confirmed_date")), ("business", "commit", 1, "2026-06-10"))
        _expect(f.ledger, "delivery v2", (v2.get("actor_role"), v2.get("action"), v2.get("is_confirmed"),
                                          v2.get("proposed_date")), ("customer", "request_change", 0, "2026-06-15"))
    if meetings:
        _, m = meetings[0]
        _expect(f.ledger, "meeting status", m.get("status"), "scheduled")
        _expect(f.ledger, "meeting date", m.get("scheduled_date"), "2026-06-05")
        _expect(f.ledger, "meeting pending action", m.get("pending_action"), None)
    return f


def ledger_after_confirmation(ledger: dict) -> Findings:
    """Run 2: the business confirms the move; v1 and the customer's v2 stay."""
    f = Findings()
    deliveries, meetings = _by_type(ledger, "delivery"), _by_type(ledger, "meeting")
    _expect(f.ledger, "delivery commitments", len(deliveries), 1)
    _expect(f.ledger, "meeting commitments", len(meetings), 1)
    if deliveries:
        _, d = deliveries[0]
        _expect(f.ledger, "delivery status", d.get("status"), "scheduled")
        _expect(f.ledger, "delivery confirmed date", d.get("scheduled_date"), "2026-06-15")
        _expect(f.ledger, "delivery pending action", d.get("pending_action"), None)
        _expect(f.ledger, "delivery versions", len(d.get("versions") or []), 3)
        v1, v2, v3 = _version(d, 1), _version(d, 2), _version(d, 3)
        _expect(f.ledger, "delivery v1 kept", (v1.get("action"), v1.get("is_confirmed"), v1.get("confirmed_date")),
                ("commit", 1, "2026-06-10"))
        _expect(f.ledger, "delivery v2 kept unconfirmed", (v2.get("actor_role"), v2.get("is_confirmed")),
                ("customer", 0))
        _expect(f.ledger, "delivery v3", (v3.get("actor_role"), v3.get("action"), v3.get("is_confirmed"),
                                          v3.get("confirmed_date")), ("business", "confirm_change", 1, "2026-06-15"))
    if meetings:
        _, m = meetings[0]
        _expect(f.ledger, "meeting date", m.get("scheduled_date"), "2026-06-05")
    return f


def unconfirmed_proposals(ledger: dict) -> list[str]:
    """Dates customers proposed that no confirmed version has adopted."""
    out = []
    for c in ledger.values():
        versions = c.get("versions") or []
        confirmed = {v.get("confirmed_date") for v in versions if v.get("is_confirmed")}
        for v in versions:
            d = v.get("proposed_date")
            if not v.get("is_confirmed") and d and d not in confirmed:
                out.append(d)
    return sorted(set(out))


# ── Hindsight ─────────────────────────────────────────────────────────────────

def ledger_documents(memory: dict, ledger: dict) -> Findings:
    """One ledger document per confirmed version, none for unconfirmed ones,
    and no ledger document states an unconfirmed proposal."""
    f = Findings()
    pending = unconfirmed_proposals(ledger)
    for cid, c in ledger.items():
        for v in c.get("versions") or []:
            doc = f"ledger:{cid}:v{v.get('version')}"
            if v.get("is_confirmed") and doc not in memory:
                f.hindsight.append(f"missing ledger document {doc}")
            if not v.get("is_confirmed") and doc in memory:
                f.hindsight.append(f"ledger document {doc} exists for an unconfirmed version")
    for doc_id, d in memory.items():
        if doc_id.startswith("ledger:"):
            for iso in pending:
                if mentions(d.get("text") or "", iso):
                    f.hindsight.append(f"{doc_id} states the unconfirmed proposal {iso}")
    return f


def customer_scope(memory: dict, observations: list, ent_tag: str, *,
                   exclude_prefix: str = "sig:probe") -> Findings:
    """Every scenario document and observation is Alice's alone, and at least
    one observation exists (missing observations are incomplete processing)."""
    f = Findings()
    if not ent_tag:
        f.incomplete.append("customer contact id unknown: scope cannot be checked")
        return f
    for doc_id, d in memory.items():
        if doc_id.startswith(exclude_prefix):
            continue
        ents = {t for t in d.get("tags") or [] if t.startswith("ent:")}
        if ents != {ent_tag}:
            f.hindsight.append(f"{doc_id} entity tags {sorted(ents)} (expected only {ent_tag})")
    mine = [o for o in observations if ent_tag in (o.get("tags") or [])]
    if not mine:
        f.incomplete.append(f"no observation for {ent_tag} after consolidation")
    for o in observations:
        tags = o.get("tags") or []
        if not any(t.startswith("ent:cid_probe") for t in tags):
            ents = {t for t in tags if t.startswith("ent:")}
            if ents != {ent_tag}:
                f.hindsight.append(f"observation entity tags {sorted(ents)}: {o.get('text', '')[:100]}")
            if "kind:ledger.state" in tags:
                f.hindsight.append(f"ledger-state memory was consolidated: {o.get('text', '')[:100]}")
    return f


def no_proposal_as_fact(texts: list[tuple[str, str]], proposals: list[str]) -> Findings:
    """A customer's unconfirmed date may appear in memory only as a request.

    *texts* are (label, text) pairs from observations and recall.  A text
    that states a proposed date without request wording presents it as an
    agreed commitment.  Lexical, so deliberately strict: a false alarm is
    resolved by reading the recorded text, never by retrying.
    """
    f = Findings()
    for label, text in texts:
        for iso in proposals:
            if mentions(text, iso) and not REQUEST_MARKERS.search(text or ""):
                f.hindsight.append(f"{label} states the unconfirmed {iso} as fact: {text[:160]!r}")
    return f


def recall_states(results: list[dict], iso: str, label: str) -> Findings:
    f = Findings()
    if not results:
        f.incomplete.append(f"{label}: recall returned nothing")
    elif not any(mentions(r.get("text") or "", iso) for r in results):
        f.hindsight.append(f"{label}: no recalled memory states {iso}")
    return f


def run_ok(run: dict, *, inserted: int | None = None, duplicates: int | None = None,
           resolved: int | None = None) -> Findings:
    f = Findings()
    if run.get("status") != "succeeded" or run.get("error"):
        f.incomplete.append(f"worker run status {run.get('status')!r}, error {run.get('error')!r}")
    for name, want in (("inserted", inserted), ("duplicates", duplicates), ("identity_resolved", resolved)):
        if want is not None and run.get(name) != want:
            f.ledger.append(f"run {name}: expected {want}, got {run.get(name)}")
    return f


def replay_unchanged(before: dict, after: dict) -> Findings:
    f = Findings()
    _expect(f.ledger, "ledger versions after replay", after.get("versions"), before.get("versions"))
    _expect(f.hindsight, "projection operations after replay", after.get("operations"), before.get("operations"))
    _expect(f.hindsight, "documents after replay", after.get("documents"), before.get("documents"))
    return f


def probe_operation(result: dict) -> Findings:
    f = Findings()
    for k in ("retain", "consolidation"):
        if result.get(k) != "completed":
            f.incomplete.append(f"probe {k} status {result.get(k)!r}")
    if result.get("prose_pending_after"):
        f.incomplete.append(f"probe: {result['prose_pending_after']} prose memories still pending")
    return f


def probe_replacement(snapshot: dict, *, new_iso: str, old_iso: str) -> Findings:
    """After re-retaining the same document id, memory states only the new date."""
    f = Findings()
    if not mentions(snapshot.get("text") or "", new_iso) or mentions(snapshot.get("text") or "", old_iso):
        f.hindsight.append("probe document text was not replaced")
    hist = snapshot.get("history") or []
    if not any((h.get("facts_removed") or 0) > 0 for h in hist):
        f.hindsight.append("probe history shows no removed facts")
    for o in snapshot.get("observations") or []:
        if mentions(o.get("text") or "", old_iso):
            f.hindsight.append(f"observation still states the replaced date {old_iso}: {o.get('text', '')[:120]!r}")
    for r in snapshot.get("recall") or []:
        if mentions(r.get("text") or "", old_iso):
            f.hindsight.append(f"recall still returns the replaced date {old_iso}: {r.get('text', '')[:120]!r}")
    if not snapshot.get("observations"):
        f.incomplete.append("probe: no observation after consolidation")
    return f


def cancellation(result: dict) -> Findings:
    """A cancel is judged by what the server holds afterwards, not by the
    operation status: Hindsight cancellation is cooperative and rolls nothing
    back.  Olo's reconciliation must match the document state read directly."""
    f = Findings()
    status = (result.get("status_after") or {}).get("status")
    if status != "cancelled":
        f.incomplete.append(f"cancellation not observed: operation status {status!r}")
    rec = result.get("reconciled")
    exists = (result.get("document") or {}).get("exists")
    if rec is None:
        f.hindsight.append("cancellation outcome not reconciled against the persisted document "
                           f"(document exists: {exists})")
        return f
    doc = result.get("document_id") or "sig:probe-cancel-1"
    claimed = doc in (rec.get("persisted") or {})
    if exists is None or claimed != bool(exists):
        f.hindsight.append(f"reconciliation says persisted={claimed}, server document exists={exists}")
    elif claimed:
        f.notes.append(f"cancel left {doc} persisted ({rec['persisted'][doc]} memory units): "
                       "cooperative cancellation, reported as a partial effect")
    return f


# ── Current state vs history (S5 live findings) ───────────────────────────────

LEDGER_CURRENT, LEDGER_SUPERSEDED = "ledger:current", "ledger:superseded"
NON_CURRENT_LABELS = ("historical", "superseded_ledger", "unconfirmed_request")


def _current_version(c: dict):
    if c.get("current_version") is not None:
        return c["current_version"]
    confirmed = [v["version"] for v in c.get("versions") or [] if v.get("is_confirmed")]
    return max(confirmed) if confirmed else None


def ledger_markers(memory: dict, ledger: dict) -> Findings:
    """Each commitment has exactly one ledger document marked current — the
    ledger's current version — and every other confirmed version is marked
    superseded, in its tags and its first line."""
    f = Findings()
    for cid, c in ledger.items():
        cur = _current_version(c)
        for v in c.get("versions") or []:
            if not v.get("is_confirmed"):
                continue
            doc_id = f"ledger:{cid}:v{v['version']}"
            d = memory.get(doc_id)
            if d is None:
                continue                      # ledger_documents reports missing documents
            tags, text = d.get("tags") or [], d.get("text") or ""
            if v["version"] == cur:
                if LEDGER_CURRENT not in tags or not text.startswith("CURRENT:"):
                    f.hindsight.append(f"{doc_id} is the ledger's current version but is not marked current")
            elif LEDGER_SUPERSEDED not in tags or not text.startswith("SUPERSEDED:"):
                f.hindsight.append(f"{doc_id} is superseded (current is v{cur}) but not marked superseded")
    return f


def current_view(view: dict | None, expected: dict) -> Findings:
    """The customer-facing view: ledger state first, every memory labelled.

    *expected* maps event_type → (current_date, pending_date).  Labels are
    re-verified here with this module's own date matching: a memory stating a
    superseded or proposed date must carry a non-current label, and only a
    current-version ledger document may be labelled current.
    """
    f = Findings()
    if not view:
        f.hindsight.append("no customer-facing current-state view: recall results are not "
                           "distinguished from the ledger's current state")
        return f
    current = {c["event_type"]: c for c in view.get("current") or []}
    for et, (cur_date, pending) in expected.items():
        c = current.get(et)
        if c is None:
            f.ledger.append(f"current-state view has no {et} commitment")
            continue
        _expect(f.ledger, f"view {et} current date", c.get("current_date"), cur_date)
        _expect(f.ledger, f"view {et} pending (unconfirmed) date", c.get("pending_date"), pending)
    old = {d for c in current.values() for d in c.get("superseded_dates") or []}
    asked = {c["pending_date"] for c in current.values() if c.get("pending_date")}
    for m in view.get("memories") or []:
        text, label = m.get("text") or "", m.get("label")
        # A current-version ledger document names the version it supersedes
        # (and so its date); it is verified by version below, not by dates.
        if (label != "current_ledger" and label not in NON_CURRENT_LABELS
                and any(mentions(text, d) for d in old | asked)):
            f.hindsight.append(f"memory states a superseded or unconfirmed date but is labelled "
                               f"{label!r}: {text[:140]!r}")
        if label == "current_ledger":
            cid_v = (m.get("document_id") or "").rsplit(":v", 1)
            ok = any(c.get("commitment_id") and f"ledger:{c['commitment_id']}" == cid_v[0]
                     and str(c.get("current_version")) == (cid_v[1] if len(cid_v) == 2 else "")
                     for c in current.values())
            if not ok:
                f.hindsight.append(f"{m.get('document_id')} labelled current but is not the current version")
    for x in view.get("excluded") or []:
        f.hindsight.append(f"recall for the customer returned another customer's memory: "
                           f"{(x.get('text') or '')[:100]!r}")
    return f


def stale_raw_observations(observations: list, ledger: dict) -> Findings:
    """Raw observations that still state a superseded date: recorded, not
    failed — the customer-facing view labels them (current_view)."""
    f = Findings()
    old = set()
    for c in ledger.values():
        cur = _current_version(c)
        for v in c.get("versions") or []:
            if v.get("is_confirmed") and v["version"] != cur and v.get("confirmed_date"):
                old.add(v["confirmed_date"])
    for o in observations:
        for d in sorted(old):
            if mentions(o.get("text") or "", d):
                f.notes.append(f"raw observation still states superseded {d}: {(o.get('text') or '')[:140]!r}")
    return f


_QUOTED = re.compile(r"^\s*>|^On .{0,80}wrote:\s*$|^-{2,}\s*(Original|Forwarded) Message", re.MULTILINE)


def authored_prose(memory: dict, *, replies: tuple[str, ...] = ("sig:c:m2",)) -> Findings:
    """Retained prose is its author's own text: no quoted history, marked
    ``text_scope=authored``, and the replies that quoted mail say so."""
    f = Findings()
    for doc_id, d in memory.items():
        if not doc_id.startswith("sig:") or doc_id.startswith("sig:probe"):
            continue
        meta = d.get("metadata") or {}
        if _QUOTED.search(d.get("text") or ""):
            f.hindsight.append(f"{doc_id} retains quoted history as if it were the author's text")
        if meta.get("text_scope") != "authored":
            f.hindsight.append(f"{doc_id} is not marked text_scope=authored")
        if doc_id in replies and meta.get("quoted_history") != "omitted":
            f.hindsight.append(f"{doc_id} quoted earlier mail but is not marked quoted_history=omitted")
    return f


def bank_strategies(config_strategies: dict, wanted: dict) -> Findings:
    f = Findings()
    for name, settings in wanted.items():
        got = config_strategies.get(name)
        if not isinstance(got, dict) or any(got.get(k) != v for k, v in settings.items()):
            f.hindsight.append(f"retain strategy {name!r} not registered as {settings} (got {got})")
    return f


def server_log(text: str | None) -> Findings:
    """No silent strategy fallback and no server error during the run."""
    f = Findings()
    if text is None:
        f.incomplete.append("server log unavailable")
        return f
    unknown = re.findall(r"Unknown retain strategy '([^']+)'", text)
    if unknown:
        f.hindsight.append(f"{len(unknown)} retains fell back to the default config: unknown "
                           f"strategy {sorted(set(unknown))}")
    errors = [ln for ln in text.splitlines() if " - ERROR - " in ln]
    if errors:
        f.incomplete.append(f"{len(errors)} server ERROR lines, first: {errors[0][:160]}")
    return f


def usage(breakdown: dict | None) -> Findings:
    """Hindsight's LLM trace reconciled by call type.

    The trace records every model call — LLM, embedding, rerank, decision —
    so its total is not a count of DeepSeek requests, and a server-log line
    count is a third, different metric (notes only).
    """
    f = Findings()
    if not breakdown:
        f.incomplete.append("usage not reconciled: no per-call-type trace breakdown recorded")
        return f
    total, parts = breakdown.get("total") or {}, breakdown.get("by_call_type") or {}
    for key in ("count", "input", "output"):
        s = sum((p or {}).get(key, 0) for p in parts.values())
        if s != total.get(key, 0):
            f.incomplete.append(f"trace {key} by call type sums to {s}, total {total.get(key, 0)}")
    if total.get("errors"):
        f.incomplete.append(f"{total['errors']} traced model calls failed")
    llm = parts.get("llm") or {}
    if llm.get("thoughts"):
        f.hindsight.append(f"{llm['thoughts']} reasoning tokens traced: thinking was not disabled")
    f.notes.append(f"traced calls by type: { {k: (v or {}).get('count', 0) for k, v in parts.items()} }; "
                   f"server-log HTTP chat-completion lines: {breakdown.get('http_log_lines')}")
    return f


def spend(s: dict, stop_usd: float) -> Findings:
    f = Findings()
    if s.get("hindsight_thoughts_tokens"):
        f.hindsight.append(f"Hindsight recorded {s['hindsight_thoughts_tokens']} reasoning tokens: "
                           "thinking was not disabled")
    if s.get("total_usd_upper", 0) >= stop_usd:
        f.incomplete.append(f"estimated spend {s.get('total_usd_upper')} reached the {stop_usd} stop")
    return f
