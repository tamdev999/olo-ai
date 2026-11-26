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
  bound, same date                      no_change
  bound, new date                       confirm_change (prior = old date)
  ====================================  =========================================

Recurring series.  A series master has no single date, so it is not a dated
commitment.  Google returns each moved or cancelled occurrence as its own
event (``recurringEventId`` + ``originalStartTime``), tracked as its own
commitment under the occurrence's id.

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
    record_unresolved,
    apply_event,
    get_current_state,
    utc_instant,
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

    @property
    def doc_id(self) -> str:
        return f"{self.key}@{self.updated}"


@dataclass(frozen=True)
class Current:
    """The ledger's state of the event's commitment."""
    commitment_id: str
    status: Optional[str]
    date: Optional[str]
    latest: Optional[datetime]      # last applied calendar revision


@dataclass(frozen=True)
class Step:
    outcome: str
    reason: str
    action: Optional[str] = None
    confirmed_date: Optional[str] = None
    prior_confirmed_date: Optional[str] = None


def _local_date(when: object) -> Optional[str]:
    """The date of a Calendar start, in the event's own offset or all-day."""
    if not isinstance(when, dict):
        return None
    raw = when.get("dateTime") or when.get("date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
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
    return Revision(
        key=event_key(calendar_id, str(doc.get("id") or "")),
        status=str(doc.get("status") or "confirmed"),
        updated=format_utc(updated) if updated else None,
        date=_local_date(doc.get("start")),
        original_date=_local_date(doc.get("originalStartTime")),
        bound=organiser_self or response == "accepted",
        declined=not organiser_self and response == "declined",
        series=bool(doc.get("recurrence")) and not doc.get("recurringEventId"),
    )


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
                    action="commit", confirmed_date=rev.date, prior_confirmed_date=moved)
    if cur.date == rev.date:
        return Step(OUTCOME_NO_CHANGE,
                    "revision does not change the meeting date (attendees, title or notes only)")
    return Step(OUTCOME_APPLIED, "event rescheduled", action="confirm_change",
                confirmed_date=rev.date, prior_confirmed_date=cur.date)


def _current(conn: sqlite3.Connection, commitment_id: str) -> Current:
    state = get_current_state(conn, commitment_id) or {}
    marks = ",".join("?" * len(CALENDAR_EVENT_KINDS))
    instants = [utc_instant(r[0]) for r in conn.execute(
        f"SELECT occurred_at FROM commitment_versions WHERE commitment_id=? AND source_kind IN ({marks})",
        (commitment_id, *sorted(CALENDAR_EVENT_KINDS)))]
    instants = [i for i in instants if i is not None]
    return Current(commitment_id, state.get("status"), state.get("scheduled_date"),
                   max(instants) if instants else None)


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
        owners = conn.execute(
            "SELECT id, contact_id FROM commitments WHERE event_type='meeting' AND business_key=?",
            (rev.key,)).fetchall()
        mine = next((r[0] for r in owners if r[1] == contact_id), None)
        others = sorted({r[1] for r in owners if r[1] != contact_id})
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
            )
            if others and mine is None:
                # The event's customer changed: an operator decides whether it
                # is a new commitment for this customer; never a silent fork.
                reason = (f"calendar event {rev.key} is recorded for another customer "
                          f"({', '.join(others)}); its customer changed")
                record_unresolved(conn, event, [], now, reason)
                outcome = OUTCOME_LEDGER_UNRESOLVED
            else:
                applied = apply_event(conn, event, recorded_at=now)
                outcome = _LEDGER_OUTCOME[applied.outcome]
                reason = f"{step.reason}; {applied.outcome.value}: {applied.message}"
                commitment_id, version = applied.commitment_id, applied.version
        record(conn, source, signal_id, STATUS_OF[outcome], outcome, reason, now,
               commitment_id=commitment_id, version=version, attempted=False)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return outcome
