"""olo/calendar_commitments.py — Google Calendar events as meeting commitments.

The business's own calendar is a structured record of the meetings it has
agreed to, so this part of the commitment stage needs no model: every
calendar event whose customer is verified maps deterministically onto the
ledger (olo.ledger).

Identity.  ``gcal:<calendar_id>:<event_id>`` is the commitment's key, a
provider key (olo.ledger.PROVIDER_KEY_PREFIX) matched exactly and never
through dates or threads.  Every revision of an event updates that one
commitment, two events never merge because their dates match, and an
attendee, title or notes edit is not a new commitment.  Each revision is
its own ledger source document, ``<key>@<updated>``, so a duplicate or
replayed delivery is UNCHANGED.

Authority.  A revision binds the business when the business organises the
event or has accepted the invitation.  An invitation it has not accepted is
no commitment; declining one it had accepted cancels it.

One revision against the commitment's current ledger state:

  ====================================  =========================================
  revision                              ledger
  ====================================  =========================================
  recurring series master               recurring_series (no single date)
  older than the last applied revision  stale_revision (never rolls back)
  cancelled, or declined                cancel (prior = scheduled date), or
                                        no_commitment / no_change when nothing
                                        is scheduled
  tentative, or not accepted            no_commitment
  bound, no commitment yet              commit
  bound, commitment cancelled           commit (restored)
  bound, same date and start time       no_change
  bound, new start time, same day       confirm_change (confirmed_start changes)
  bound, new date                       confirm_change (prior = old date)
  ====================================  =========================================

Email and calendar.  A Gmail invitation carries the event's iCalendar UID,
the one authoritative link between an email and a calendar event
(olo.commitments.apply_invitation_aware).  Whichever arrives first records
the commitment; the UID (``ical:<uid>``) and the event's key become aliases
of it, so the other source's events apply to the same commitment in any
order.  With no UID, a calendar event on a day that already holds a meeting
recorded from email for the same customer is left unresolved for an
operator: the same date alone neither links them nor justifies a second
commitment.

Recurring series.  A series master has no single date, so it is not a dated
commitment.  Google returns each moved or cancelled occurrence as its own
event (``recurringEventId`` + ``originalStartTime``), tracked as its own
commitment under the occurrence's id.  A series and all its occurrences share
one iCalendar UID (confirmed against live Google, 2026-11-27), so for them
the UID names the series, not a meeting: it never links or aliases a
recurring revision, and cancelling or moving one occurrence cannot reach
another's commitment.  The occurrence's place in its series is recorded
apart, as the alias ``series:<series key>@<original start>`` (basis
``series_occurrence``), which no matcher consults.

Ordering.  Revisions apply in Google's ``updated`` order: an older revision
arriving after a newer one (a full resync after an expired sync token,
reordered pages) is stale and never rolls the ledger back.

Customer.  The commitment belongs to the event's single verified customer
(identity stage).  A revision whose customer differs from the commitment's
stays unresolved for an operator; it never becomes a second commitment.

Evidence.  The full event document (the content blob) is read, never
rewritten; the stage only records ledger versions and outcomes.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from olo.ledger import (
    PROVIDER_KEY_PREFIX,
    PROVIDER_SOURCE_KINDS,
    STATUS_CANCELLED,
    STATUS_SCHEDULED,
    LedgerEvent,
    OutcomeKind,
    add_alias,
    alias_target,
    apply_event,
    get_current_state,
    record_unresolved,
    utc_instant,
    waiting_on,
)
from olo.signals import format_utc

__all__ = [
    "CALENDAR_EVENT_KINDS",
    "Revision",
    "Step",
    "decide",
    "event_key",
    "process_calendar_signal",
    "read_revision",
]

CALENDAR_EVENT_KINDS: frozenset[str] = PROVIDER_SOURCE_KINDS

OUTCOME_APPLIED = "applied"
OUTCOME_NO_CHANGE = "no_change"
OUTCOME_NO_COMMITMENT = "no_commitment"
OUTCOME_NO_INPUT = "no_input"
OUTCOME_STALE = "stale_revision"
OUTCOME_RECURRING_SERIES = "recurring_series"
OUTCOME_LEDGER_UNRESOLVED = "ledger_unresolved"
OUTCOME_LEDGER_INVALID = "ledger_invalid"

#: outcome → commitment_status
STATUS_OF: dict[str, str] = {
    OUTCOME_APPLIED: "resolved",
    OUTCOME_NO_CHANGE: "resolved",
    OUTCOME_NO_COMMITMENT: "resolved",
    OUTCOME_NO_INPUT: "skipped",
    OUTCOME_STALE: "skipped",
    OUTCOME_RECURRING_SERIES: "skipped",
    OUTCOME_LEDGER_UNRESOLVED: "unresolved",
    OUTCOME_LEDGER_INVALID: "unresolved",
}

_LEDGER_OUTCOME: dict[OutcomeKind, str] = {
    OutcomeKind.CREATED: OUTCOME_APPLIED,
    OutcomeKind.UPDATED: OUTCOME_APPLIED,
    OutcomeKind.UNCHANGED: OUTCOME_APPLIED,
    OutcomeKind.UNRESOLVED: OUTCOME_LEDGER_UNRESOLVED,
    OutcomeKind.INVALID: OUTCOME_LEDGER_INVALID,
}


def event_key(calendar_id: str, event_id: str) -> str:
    """The commitment key of one calendar event (or one occurrence)."""
    return f"{PROVIDER_KEY_PREFIX}{calendar_id}:{event_id}"


@dataclass(frozen=True)
class Revision:
    """One revision of a calendar event, as far as the ledger needs it."""
    key: str
    status: str                     # confirmed | tentative | cancelled
    updated: Optional[str]          # UTC; None when Google gave no valid time
    date: Optional[str]             # local start date (YYYY-MM-DD)
    original_date: Optional[str]    # occurrence's original date (exceptions)
    bound: bool                     # business organises or has accepted
    declined: bool                  # business declined the invitation
    series: bool                    # recurring series master
    start: Optional[str] = None     # start dateTime with offset (None: all-day)
    ical_uid: str = ""              # iCalUID shared with its invitation emails
    series_key: str = ""            # the series' key, for an occurrence of one
    original_start: str = ""        # the occurrence's original slot, as Google gave it

    @property
    def doc_id(self) -> str:
        return f"{self.key}@{self.updated}"

    @property
    def recurring(self) -> bool:
        """A series master or one of its occurrences: its iCalUID names the series."""
        return self.series or bool(self.series_key)

    @property
    def link_uid(self) -> str:
        """The iCalUID that identifies this one meeting, or "" (recurring)."""
        return "" if self.recurring else self.ical_uid


@dataclass(frozen=True)
class Current:
    """The ledger's state of the event's commitment."""
    commitment_id: str
    status: Optional[str]
    date: Optional[str]
    latest: Optional[datetime]      # last applied calendar revision
    start: Optional[str] = None     # confirmed start time, when known


@dataclass(frozen=True)
class Step:
    outcome: str
    reason: str
    action: Optional[str] = None
    confirmed_date: Optional[str] = None
    prior_confirmed_date: Optional[str] = None
    confirmed_start: Optional[str] = None


def _same_instant(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a == b
    return utc_instant(a) == utc_instant(b)


def _local_date(when: object) -> Optional[str]:
    """The date of a Calendar start, in the event's own offset or all-day."""
    if not isinstance(when, dict):
        return None
    raw = when.get("dateTime") or when.get("date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw).date().isoformat()
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


def read_revision(doc: dict, calendar_id: str) -> Revision:
    """Parse a Calendar API event resource (the stored event document)."""
    attendees = [a for a in doc.get("attendees") or [] if isinstance(a, dict)]
    me = next((a for a in attendees if a.get("self")), None)
    response = (me or {}).get("responseStatus", "")
    organiser_self = bool((doc.get("organizer") or {}).get("self")) or bool(
        me and me.get("organizer"))
    updated = utc_instant((doc.get("updated") or "").replace("Z", "+00:00"))
    series_id = str(doc.get("recurringEventId") or "")
    original = doc.get("originalStartTime") if isinstance(doc.get("originalStartTime"), dict) else {}
    return Revision(
        key=event_key(calendar_id, str(doc.get("id") or "")),
        status=str(doc.get("status") or "confirmed"),
        updated=format_utc(updated) if updated else None,
        date=_local_date(doc.get("start")),
        original_date=_local_date(doc.get("originalStartTime")),
        bound=organiser_self or response == "accepted",
        declined=not organiser_self and response == "declined",
        series=bool(doc.get("recurrence")) and not doc.get("recurringEventId"),
        start=_start_time(doc.get("start")),
        ical_uid=str(doc.get("iCalUID") or ""),
        series_key=event_key(calendar_id, series_id) if series_id else "",
        original_start=str(original.get("dateTime") or original.get("date") or ""),
    )


def _start_time(when: object) -> Optional[str]:
    """A timed start as ISO 8601 with its own offset; None for all-day."""
    raw = when.get("dateTime") if isinstance(when, dict) else None
    if not isinstance(raw, str) or "T" not in raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.isoformat() if parsed.utcoffset() is not None else None


def decide(rev: Revision, cur: Optional[Current]) -> Step:
    """The ledger step for one revision (see the module table)."""
    if rev.series:
        return Step(OUTCOME_RECURRING_SERIES,
                    "a recurring series has no single date; its moved or cancelled "
                    "occurrences are tracked individually")
    if rev.updated is None:
        return Step(OUTCOME_NO_INPUT, "revision has no valid 'updated' time to order it by")
    if cur is not None and cur.latest is not None and utc_instant(rev.updated) < cur.latest:
        return Step(OUTCOME_STALE,
                    "an older revision than the one the ledger already reflects")
    if rev.status == "cancelled" or rev.declined:
        what = "cancelled" if rev.status == "cancelled" else "declined by the business"
        if cur is None:
            return Step(OUTCOME_NO_COMMITMENT,
                        f"event {what} with no commitment recorded for it")
        if cur.status != STATUS_SCHEDULED:
            return Step(OUTCOME_NO_CHANGE, f"event {what}; its commitment is already cancelled")
        return Step(OUTCOME_APPLIED, f"event {what}", action="cancel",
                    prior_confirmed_date=cur.date)
    if rev.status != "confirmed" or not rev.bound:
        return Step(OUTCOME_NO_COMMITMENT,
                    f"not confirmed by the business (status {rev.status!r}, "
                    "invitation not accepted)" if not rev.bound else
                    f"not confirmed by the business (status {rev.status!r})")
    if rev.date is None:
        return Step(OUTCOME_NO_INPUT, "event has no readable start date")
    if cur is None or cur.status == STATUS_CANCELLED:
        moved = rev.original_date if rev.original_date and rev.original_date != rev.date else None
        return Step(OUTCOME_APPLIED, "event scheduled" if cur is None else "event restored",
                    action="commit", confirmed_date=rev.date, prior_confirmed_date=moved,
                    confirmed_start=rev.start)
    if cur.date == rev.date and _same_instant(cur.start, rev.start):
        return Step(OUTCOME_NO_CHANGE,
                    "revision does not change the meeting's date or time "
                    "(attendees, title or notes only)")
    what = "moved to another time the same day" if cur.date == rev.date else "rescheduled"
    return Step(OUTCOME_APPLIED, f"event {what}", action="confirm_change",
                confirmed_date=rev.date, prior_confirmed_date=cur.date,
                confirmed_start=rev.start)


def _current(conn: sqlite3.Connection, commitment_id: str) -> Current:
    state = get_current_state(conn, commitment_id) or {}
    marks = ",".join("?" * len(CALENDAR_EVENT_KINDS))
    instants = [utc_instant(r[0]) for r in conn.execute(
        f"SELECT occurred_at FROM commitment_versions WHERE commitment_id=? AND source_kind IN ({marks})",
        (commitment_id, *sorted(CALENDAR_EVENT_KINDS)))]
    instants = [i for i in instants if i is not None]
    return Current(commitment_id, state.get("status"), state.get("scheduled_date"),
                   max(instants) if instants else None, state.get("scheduled_start"))


def _owners(conn: sqlite3.Connection, rev: Revision) -> list[tuple[str, str, str]]:
    """(commitment, customer, basis) of every commitment holding this event's
    identity: its provider key as name or alias, or — for a single,
    non-recurring event only — its iCalendar UID."""
    out = [(r[0], r[1], "provider_key") for r in conn.execute(
        "SELECT id, contact_id FROM commitments WHERE event_type='meeting' AND business_key=?",
        (rev.key,))]
    for alias, basis in ((rev.key, "provider_key"),
                         (f"ical:{rev.link_uid}" if rev.link_uid else "", "ical_uid")):
        target = alias_target(conn, alias) if alias else None
        if target and all(target != o[0] for o in out):
            row = conn.execute("SELECT contact_id FROM commitments WHERE id=?", (target,)).fetchone()
            if row:
                out.append((target, row[0], basis))
    return out


def _named(conn: sqlite3.Connection, commitment_id: str) -> str:
    row = conn.execute("SELECT business_key FROM commitments WHERE id=?", (commitment_id,)).fetchone()
    return row[0] if row else ""


def _same_day_from_another_source(conn: sqlite3.Connection, contact_id: str,
                                  day: str) -> list[str]:
    """This customer's meeting commitments on *day* that no calendar event
    identifies (recorded from email or calls)."""
    clash = []
    for (cid, key) in conn.execute(
            "SELECT id, business_key FROM commitments WHERE contact_id=? AND event_type='meeting'",
            (contact_id,)).fetchall():
        if key.startswith(PROVIDER_KEY_PREFIX) or conn.execute(
                "SELECT 1 FROM commitment_aliases WHERE commitment_id=? AND alias LIKE ?",
                (cid, f"{PROVIDER_KEY_PREFIX}%")).fetchone():
            continue
        st = get_current_state(conn, cid) or {}
        if day in (st.get("scheduled_date"), st.get("pending_proposed_date")):
            clash.append(cid)
    return sorted(clash)


def inherited_contact(conn: sqlite3.Connection, source: str, payload: dict) -> tuple[str, str]:
    """``(contact_id, ambiguity_reason)`` from earlier revisions of the event.

    Google returns a deleted event to incremental sync as a stub (id, status,
    updated) with no organiser or attendees, so the identity stage finds no
    counterparty in it.  The event id is the provider's own record identity:
    the stub belongs to whichever customer that event's earlier revisions
    were verified for — or, for an occurrence of a recurring series, the
    series'.  Two different customers there is ambiguous; none leaves the
    stub unresolved (there is nothing of theirs to cancel).
    """
    event_id, calendar_id = payload.get("event_id"), payload.get("calendar_id")
    if not event_id:
        return "", ""
    ids = sorted({event_id, payload.get("recurring_event_id") or event_id})
    marks = ",".join("?" * len(CALENDAR_EVENT_KINDS))
    contacts = sorted({r[0] for r in conn.execute(
        "SELECT sr.contact_id FROM signals s JOIN signal_resolution sr"
        "  ON sr.signal_id = s.external_id AND sr.source = s.source"
        f" WHERE s.source=? AND s.kind IN ({marks}) AND sr.identity_status='resolved'"
        f"   AND json_extract(s.payload, '$.event_id') IN ({','.join('?' * len(ids))})"
        "   AND json_extract(s.payload, '$.calendar_id')=?",
        (source, *sorted(CALENDAR_EVENT_KINDS), *ids, calendar_id))})
    if len(contacts) > 1:
        return "", (f"earlier revisions of calendar event {event_id} name "
                    f"{len(contacts)} customers: {', '.join(contacts)}")
    return (contacts[0] if contacts else ""), ""


def _event_document(conn: sqlite3.Connection, source: str, payload: dict) -> Optional[dict]:
    """The stored full event resource, or None when the blob is unreadable."""
    from olo.store import get_blob_raw
    refs = payload.get("content_refs") or []
    if not refs:
        return None
    raw = get_blob_raw(source, refs[0].get("sha256", ""), conn)
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _document_from_payload(payload: dict) -> dict:
    """A best-effort event resource from the metadata payload (no blob)."""
    def when(value: str) -> dict:
        if not value:
            return {}
        return {"dateTime": value} if "T" in value else {"date": value}
    return {
        "id": payload.get("event_id"), "status": payload.get("status"),
        "updated": payload.get("updated"), "start": when(payload.get("start") or ""),
        "originalStartTime": when(payload.get("original_start") or ""),
        "organizer": {"self": payload.get("organizer_self", False)},
        "attendees": [{"self": a.get("self"), "organizer": a.get("organizer"),
                       "responseStatus": a.get("response_status")}
                      for a in payload.get("attendees") or [] if isinstance(a, dict)],
        "recurrence": payload.get("recurrence") or [],
        "recurringEventId": payload.get("recurring_event_id"),
        "iCalUID": payload.get("ical_uid") or "",
    }


def process_calendar_signal(
    conn: sqlite3.Connection,
    source: str,
    signal_id: str,
    contact_id: str,
    kind: str,
    payload: dict,
    now: str,
    record: Callable[..., None],
) -> str:
    """Apply one verified calendar signal to the ledger; return its outcome.

    ``record(conn, source, signal_id, status, outcome, reason, now, *,
    commitment_id, version, attempted)`` persists the outcome; it runs in
    the same transaction as the ledger write.
    """
    calendar_id = str(payload.get("calendar_id") or "")
    doc = _event_document(conn, source, payload) or _document_from_payload(payload)
    rev = read_revision(doc, calendar_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        owners = _owners(conn, rev)
        mine = next((o[0] for o in owners if o[1] == contact_id), None)
        others = sorted({o[1] for o in owners if o[1] != contact_id})
        # The commitment holds this event only through an alias (it was
        # recorded from an invitation email first): apply to it directly, and
        # let the event's own key name it too (a deletion stub carries no UID).
        linked = mine is not None and _named(conn, mine) != rev.key
        if linked:
            add_alias(conn, rev.key, mine, "provider_key", rev.doc_id, now)
        step = decide(rev, _current(conn, mine) if mine else None)
        commitment_id: Optional[str] = mine if step.outcome == OUTCOME_NO_CHANGE else None
        version: Optional[int] = None
        reason = step.reason
        outcome = step.outcome
        if step.action is not None:
            event = LedgerEvent(
                event_type="meeting", contact_id=contact_id, business_key=rev.key,
                source_doc_id=rev.doc_id, source_kind=kind, occurred_at=rev.updated or "",
                actor_role="business", action=step.action,
                confirmed_date=step.confirmed_date,
                prior_confirmed_date=step.prior_confirmed_date,
                thread_id=rev.key, notes=f"sig:{signal_id}",
                confirmed_start=step.confirmed_start,
            )
            waits = waiting_on(conn, event) if mine is None else []
            clash = (_same_day_from_another_source(conn, contact_id, step.confirmed_date)
                     if mine is None and step.action == "commit" and not waits else [])
            if waits:
                # An earlier revision of this event is still undecided.
                reason = (f"an earlier revision of {rev.key} awaits an operator "
                          f"({', '.join(waits)})")
                record_unresolved(conn, event, [], now, reason, waits)
                outcome = OUTCOME_LEDGER_UNRESOLVED
            elif others and mine is None:
                # The event's customer changed: an operator decides whether it
                # is a new commitment for this customer; never a silent fork.
                reason = (f"calendar event {rev.key} is recorded for another customer "
                          f"({', '.join(others)}); its customer changed")
                record_unresolved(conn, event, [], now, reason)
                outcome = OUTCOME_LEDGER_UNRESOLVED
            elif clash:
                # A meeting recorded from email is on the same day and nothing
                # authoritative (an invitation UID) ties it to this event: a
                # second commitment could duplicate it, a link would be a guess.
                reason = (f"{len(clash)} meeting commitment(s) recorded from email are on "
                          f"{step.confirmed_date} with no invitation UID linking them to "
                          f"{rev.key}; an operator links or separates them")
                record_unresolved(conn, event, clash, now, reason)
                outcome = OUTCOME_LEDGER_UNRESOLVED
            else:
                # A linked commitment now holds the key as an alias, which
                # the matcher resolves exactly like a name.
                applied = apply_event(conn, event, recorded_at=now)
                outcome = _LEDGER_OUTCOME[applied.outcome]
                reason = f"{step.reason}; {applied.outcome.value}: {applied.message}"
                commitment_id, version = applied.commitment_id, applied.version
                if commitment_id and rev.link_uid and outcome == OUTCOME_APPLIED:
                    if not add_alias(conn, f"ical:{rev.link_uid}", commitment_id, "ical_uid",
                                     rev.doc_id, now):
                        reason += "; invitation UID already names another commitment"
                if (commitment_id and rev.series_key and rev.original_start
                        and outcome == OUTCOME_APPLIED):
                    add_alias(conn, f"series:{rev.series_key}@{rev.original_start}",
                              commitment_id, "series_occurrence", rev.doc_id, now)
        record(conn, source, signal_id, STATUS_OF[outcome], outcome, reason, now,
               commitment_id=commitment_id, version=version, attempted=False)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return outcome
