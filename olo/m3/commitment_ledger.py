"""olo/m3/commitment_ledger.py — Deterministic commitment ledger.

Tracks delivery orders and meeting slots as append-only versioned records.
Separates customer *requests* (proposals that do not change the confirmed date)
from business *confirmations* (which update the current state and preserve
the prior state in history).

Design constraints:
  - Append-only: once written, versions are never modified or deleted.
  - Deterministic: applying the same event sequence always yields identical
    state; ``recorded_at`` must be supplied by the caller for replay.
  - Business isolation: ``event_type`` is a first-class discriminator; a
    delivery commitment and a meeting commitment for the same customer are
    always separate records.
  - Customer isolation: ``contact_id`` must be a canonical ``cid_...``
    identifier from the contacts table.  Legacy ULID generator entity IDs
    are rejected at the boundary.
  - Multiple unnamed commitments may coexist for the same customer and event
    type (e.g. two delivery orders on different dates, neither with an order
    ID).  The unique constraint only applies to *named* (business_key != '')
    commitments.
  - Cross-channel linking is evidence-based: a transcript event that names a
    ``prior_confirmed_date`` links to the commitment whose current confirmed
    date matches that value.  When no date evidence is provided and a single
    candidate exists, that candidate is updated.  When multiple candidates
    qualify — or when there is no trustworthy connection — the event is left
    UNRESOLVED rather than guessing.
  - Idempotency: a ``source_doc_id`` already applied to a commitment returns
    UNCHANGED on the second call without writing any new row.

This module has no dependency on Hindsight, the LLM, or any network service.
All events must be supplied by the caller from already-validated source
documents.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Supported event types ─────────────────────────────────────────────────────

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset({"delivery", "meeting"})
_CID_PREFIX = "cid_"


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS commitments (
    id              TEXT NOT NULL PRIMARY KEY,
    contact_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    business_key    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- Named commitments (business_key != '') are unique per customer per type.
-- Unnamed commitments (business_key = '') may coexist: two delivery orders
-- without order IDs on different dates are distinct business objects.
-- SQLite 3.8.9+ partial-index syntax; both Linux (3.37.2) and Windows
-- (3.53.1) test-env SQLite versions satisfy this requirement.
CREATE UNIQUE INDEX IF NOT EXISTS uk_named_commitment
    ON commitments (contact_id, event_type, business_key)
    WHERE business_key != '';

CREATE TABLE IF NOT EXISTS commitment_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id        TEXT NOT NULL,
    version              INTEGER NOT NULL,
    confirmed_date       TEXT,
    prior_confirmed_date TEXT,
    proposed_date        TEXT,
    is_confirmed         INTEGER NOT NULL,
    source_doc_id        TEXT NOT NULL,
    source_kind          TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    recorded_at          TEXT NOT NULL,
    notes                TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (commitment_id) REFERENCES commitments (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uk_commitment_version
    ON commitment_versions (commitment_id, version);

CREATE UNIQUE INDEX IF NOT EXISTS uk_commitment_source
    ON commitment_versions (commitment_id, source_doc_id);

CREATE TABLE IF NOT EXISTS unresolved_matches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_doc_id   TEXT NOT NULL,
    contact_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    candidate_ids   TEXT NOT NULL,
    recorded_at     TEXT NOT NULL
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Create ledger tables if they do not exist.  Idempotent."""
    conn.executescript(_SCHEMA_DDL)
    conn.commit()


# ── Event dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LedgerEvent:
    """A business event to apply to the commitment ledger.

    Attributes:
        event_type:           ``'delivery'`` or ``'meeting'``.
        contact_id:           Canonical ``cid_...`` identifier from the CRM.
        business_key:         Specific identifier such as an order ID or
                              meeting reference.  ``''`` when unknown.
        source_doc_id:        ``document_id`` of the source Hindsight item.
                              Guarantees idempotency per commitment.
        source_kind:          Signal kind (e.g. ``'email.sent'``,
                              ``'call.transcript'``).
        occurred_at:          ISO 8601 datetime — when the original event
                              happened (from the source document timestamp).
        proposed_date:        ISO 8601 date that the customer is *requesting*.
                              ``None`` when the event carries no proposal.
        confirmed_date:       ISO 8601 date *confirmed* by the business.
                              ``None`` for customer requests.
        prior_confirmed_date: The previously-confirmed date this event is
                              superseding, expressed in ISO 8601.  Used as
                              cross-channel linking evidence when
                              ``business_key`` is unavailable: the matcher
                              finds commitments whose current confirmed date
                              (or previous prior_confirmed_date) matches this
                              value.  ``None`` = no prior-date restriction.
        is_confirmation:      ``True`` when this is a business confirmation.
                              ``False`` for customer-side proposals.
        notes:                Optional human-readable annotation.
    """
    event_type:           str
    contact_id:           str
    business_key:         str
    source_doc_id:        str
    source_kind:          str
    occurred_at:          str
    proposed_date:        Optional[str]
    confirmed_date:       Optional[str]
    prior_confirmed_date: Optional[str] = None
    is_confirmation:      bool = True
    notes:                str = ""


# ── Result type ───────────────────────────────────────────────────────────────

class OutcomeKind(str, Enum):
    CREATED     = "created"    # new commitment created; version 1 written
    UPDATED     = "updated"    # new version appended to existing commitment
    UNCHANGED   = "unchanged"  # source_doc_id already applied — idempotent
    UNRESOLVED  = "unresolved" # multiple candidate commitments; event NOT applied
    INVALID     = "invalid"    # validation error; event NOT applied


@dataclass(frozen=True)
class ApplyResult:
    """Result of one :func:`apply_event` call."""
    outcome:       OutcomeKind
    commitment_id: Optional[str]
    version:       Optional[int]
    message:       str


# ── Validation ────────────────────────────────────────────────────────────────

def _validate(event: LedgerEvent) -> Optional[str]:
    if event.event_type not in SUPPORTED_EVENT_TYPES:
        return (
            f"event_type must be one of {sorted(SUPPORTED_EVENT_TYPES)!r}, "
            f"got {event.event_type!r}"
        )
    if not event.contact_id.startswith(_CID_PREFIX):
        return (
            f"contact_id must start with {_CID_PREFIX!r} (canonical CRM identifier); "
            f"got {event.contact_id!r}. "
            "ULID generator entity IDs (ent_...) are not accepted."
        )
    if not event.source_doc_id:
        return "source_doc_id must not be empty"
    if not event.source_kind:
        return "source_kind must not be empty"
    if not event.occurred_at:
        return "occurred_at must not be empty"
    return None


# ── Date-based evidence helpers ───────────────────────────────────────────────

def _commitment_has_date(
    conn: sqlite3.Connection,
    commitment_id: str,
    date: str,
) -> bool:
    """Return True if the commitment's latest version contains *date*.

    Checks ``confirmed_date`` and ``prior_confirmed_date`` of the latest
    version.  This enables out-of-order arrival to produce consistent state:
    when a transcript (prior=D, confirmed=D') arrives before an email
    (confirmed=D), the email can still match the commitment via its stored
    ``prior_confirmed_date``.
    """
    row = conn.execute(
        "SELECT confirmed_date, prior_confirmed_date"
        " FROM commitment_versions"
        " WHERE commitment_id=?"
        " ORDER BY version DESC LIMIT 1",
        (commitment_id,),
    ).fetchone()
    if not row:
        return False
    return date in (row[0], row[1])


def _filter_by_date(
    conn: sqlite3.Connection,
    candidate_ids: list[str],
    date: str,
) -> list[str]:
    """Return the subset of *candidate_ids* whose latest version contains *date*."""
    return [cid for cid in candidate_ids if _commitment_has_date(conn, cid, date)]


# ── Matching ──────────────────────────────────────────────────────────────────

def _find_matches(conn: sqlite3.Connection, event: LedgerEvent) -> list[str]:
    """Return commitment IDs matching this event's key fields.

    Named events (``business_key != ''``):
      1. Try exact match on ``(contact_id, event_type, business_key)``.
      2. If no exact match, find unnamed (``business_key=''``) commitments.
         If ``prior_confirmed_date`` is set, filter those by date.
         0 → caller creates new named commitment.
         1 → caller claims (assigns business_key + appends version).
         2+ → UNRESOLVED.

    Unnamed events (``business_key == ''``):
      Determine a *match_date* as ``prior_confirmed_date ?? confirmed_date``.
      If match_date is set, filter all same-type commitments by date:
        0 → caller creates new unnamed commitment (distinct delivery).
        1 → caller updates the matching commitment.
        2+ → UNRESOLVED (ambiguous; caller must supply more evidence).
      If no match_date, return all same-type commitments:
        0 → create; 1 → update (single unambiguous candidate); 2+ → UNRESOLVED.

    The date filter uses ``_commitment_has_date``, which checks the latest
    version's ``confirmed_date`` and ``prior_confirmed_date`` so that
    out-of-order arrival produces the same final state.
    """
    if event.business_key:
        # 1. Exact match on named key
        exact = conn.execute(
            "SELECT id FROM commitments"
            " WHERE contact_id=? AND event_type=? AND business_key=?",
            (event.contact_id, event.event_type, event.business_key),
        ).fetchall()
        if exact:
            return [r[0] for r in exact]

        # 2. Claim candidates: unnamed commitments, optionally filtered by date
        unnamed = conn.execute(
            "SELECT id FROM commitments"
            " WHERE contact_id=? AND event_type=? AND business_key=''",
            (event.contact_id, event.event_type),
        ).fetchall()
        candidates = [r[0] for r in unnamed]
        if candidates and event.prior_confirmed_date:
            candidates = _filter_by_date(conn, candidates, event.prior_confirmed_date)
        return candidates

    else:
        # Unnamed event: find all same-type commitments
        all_rows = conn.execute(
            "SELECT id FROM commitments"
            " WHERE contact_id=? AND event_type=?",
            (event.contact_id, event.event_type),
        ).fetchall()
        candidates = [r[0] for r in all_rows]
        if not candidates:
            return []

        # Apply date-based evidence if available
        match_date = event.prior_confirmed_date or event.confirmed_date
        if match_date:
            return _filter_by_date(conn, candidates, match_date)
        # No date evidence: safe only for single candidate
        return candidates


# ── Idempotency ───────────────────────────────────────────────────────────────

def _applied_version(
    conn: sqlite3.Connection,
    commitment_id: str,
    source_doc_id: str,
) -> Optional[int]:
    """Return the version number if this source was already applied, else None."""
    row = conn.execute(
        "SELECT version FROM commitment_versions"
        " WHERE commitment_id=? AND source_doc_id=?",
        (commitment_id, source_doc_id),
    ).fetchone()
    return row[0] if row else None


# ── Write helpers ─────────────────────────────────────────────────────────────

def _next_version(conn: sqlite3.Connection, commitment_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM commitment_versions"
        " WHERE commitment_id=?",
        (commitment_id,),
    ).fetchone()
    return (row[0] if row else 0) + 1


def _create_commitment(
    conn: sqlite3.Connection, event: LedgerEvent, recorded_at: str
) -> str:
    cid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO commitments (id, contact_id, event_type, business_key, created_at)"
        " VALUES (?,?,?,?,?)",
        (cid, event.contact_id, event.event_type, event.business_key, recorded_at),
    )
    return cid


def _write_version(
    conn: sqlite3.Connection,
    commitment_id: str,
    version: int,
    event: LedgerEvent,
    recorded_at: str,
) -> None:
    conn.execute(
        "INSERT INTO commitment_versions"
        " (commitment_id, version, confirmed_date, prior_confirmed_date,"
        "  proposed_date, is_confirmed, source_doc_id, source_kind,"
        "  occurred_at, recorded_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            commitment_id,
            version,
            event.confirmed_date,
            event.prior_confirmed_date,
            event.proposed_date,
            1 if event.is_confirmation else 0,
            event.source_doc_id,
            event.source_kind,
            event.occurred_at,
            recorded_at,
            event.notes,
        ),
    )


def _record_unresolved(
    conn: sqlite3.Connection,
    event: LedgerEvent,
    candidate_ids: list[str],
    recorded_at: str,
) -> None:
    conn.execute(
        "INSERT INTO unresolved_matches"
        " (source_doc_id, contact_id, event_type, candidate_ids, recorded_at)"
        " VALUES (?,?,?,?,?)",
        (
            event.source_doc_id,
            event.contact_id,
            event.event_type,
            json.dumps(sorted(candidate_ids)),
            recorded_at,
        ),
    )


# ── Core state machine ────────────────────────────────────────────────────────

def apply_event(
    conn: sqlite3.Connection,
    event: LedgerEvent,
    *,
    recorded_at: Optional[str] = None,
) -> ApplyResult:
    """Apply one event to the commitment ledger.

    Does NOT commit the connection; the caller must commit (or roll back).

    Args:
        conn:        Open SQLite connection with ledger tables present.
        event:       The business event to apply.
        recorded_at: ISO 8601 datetime to stamp on the written rows.
                     Supply a fixed value for deterministic replay.
                     Defaults to the current UTC time when ``None``.

    Returns:
        :class:`ApplyResult` describing the outcome.  ``INVALID`` and
        ``UNRESOLVED`` outcomes do not write any commitment or version row.
    """
    if recorded_at is None:
        from datetime import datetime, timezone
        recorded_at = datetime.now(timezone.utc).isoformat()

    err = _validate(event)
    if err:
        return ApplyResult(OutcomeKind.INVALID, None, None, err)

    matches = _find_matches(conn, event)

    if len(matches) > 1:
        _record_unresolved(conn, event, matches, recorded_at)
        return ApplyResult(
            OutcomeKind.UNRESOLVED,
            None,
            None,
            f"{len(matches)} candidate commitments; supply prior_confirmed_date "
            "or business_key to disambiguate",
        )

    if len(matches) == 1:
        commitment_id = matches[0]
        existing = _applied_version(conn, commitment_id, event.source_doc_id)
        if existing is not None:
            return ApplyResult(
                OutcomeKind.UNCHANGED,
                commitment_id,
                existing,
                "source_doc_id already applied to this commitment",
            )
        # Cross-channel claim: if the event names a business_key but the matched
        # commitment is currently unnamed, assign the business_key now.
        matched_key_row = conn.execute(
            "SELECT business_key FROM commitments WHERE id=?", (commitment_id,)
        ).fetchone()
        matched_key = matched_key_row[0] if matched_key_row else ""
        if event.business_key and matched_key == "":
            conn.execute(
                "UPDATE commitments SET business_key=? WHERE id=?",
                (event.business_key, commitment_id),
            )
            outcome_msg = "commitment claimed (business_key assigned) and version appended"
        else:
            outcome_msg = "version appended"
        version = _next_version(conn, commitment_id)
        _write_version(conn, commitment_id, version, event, recorded_at)
        return ApplyResult(OutcomeKind.UPDATED, commitment_id, version, outcome_msg)

    # No match — create a new commitment
    commitment_id = _create_commitment(conn, event, recorded_at)
    _write_version(conn, commitment_id, 1, event, recorded_at)
    return ApplyResult(OutcomeKind.CREATED, commitment_id, 1, "commitment created")


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_current_state(
    conn: sqlite3.Connection,
    commitment_id: str,
) -> Optional[dict]:
    """Return the current state of a commitment as a plain dict, or ``None``."""
    row = conn.execute(
        "SELECT id, contact_id, event_type, business_key, created_at"
        " FROM commitments WHERE id=?",
        (commitment_id,),
    ).fetchone()
    if not row:
        return None

    latest = conn.execute(
        "SELECT version, confirmed_date, prior_confirmed_date, proposed_date,"
        "       is_confirmed, source_doc_id, source_kind, occurred_at,"
        "       recorded_at, notes"
        " FROM commitment_versions"
        " WHERE commitment_id=?"
        " ORDER BY occurred_at DESC, version DESC LIMIT 1",
        (commitment_id,),
    ).fetchone()

    return {
        "id":                   row[0],
        "contact_id":           row[1],
        "event_type":           row[2],
        "business_key":         row[3],
        "created_at":           row[4],
        "version":              latest[0] if latest else None,
        "confirmed_date":       latest[1] if latest else None,
        "prior_confirmed_date": latest[2] if latest else None,
        "proposed_date":        latest[3] if latest else None,
        "is_confirmed":         bool(latest[4]) if latest else False,
        "source_doc_id":        latest[5] if latest else None,
        "source_kind":          latest[6] if latest else None,
        "occurred_at":          latest[7] if latest else None,
        "recorded_at":          latest[8] if latest else None,
        "notes":                latest[9] if latest else "",
    }


def get_all_versions(
    conn: sqlite3.Connection,
    commitment_id: str,
) -> list[dict]:
    """Return all versions for a commitment, oldest first."""
    rows = conn.execute(
        "SELECT version, confirmed_date, prior_confirmed_date, proposed_date,"
        "       is_confirmed, source_doc_id, source_kind, occurred_at,"
        "       recorded_at, notes"
        " FROM commitment_versions"
        " WHERE commitment_id=?"
        " ORDER BY version ASC",
        (commitment_id,),
    ).fetchall()
    return [
        {
            "version":              r[0],
            "confirmed_date":       r[1],
            "prior_confirmed_date": r[2],
            "proposed_date":        r[3],
            "is_confirmed":         bool(r[4]),
            "source_doc_id":        r[5],
            "source_kind":          r[6],
            "occurred_at":          r[7],
            "recorded_at":          r[8],
            "notes":                r[9],
        }
        for r in rows
    ]


def replay_events(
    conn: sqlite3.Connection,
    events: list[LedgerEvent],
    *,
    recorded_at: str = "2026-01-01T00:00:00+00:00",
) -> list[ApplyResult]:
    """Apply a list of events in order and commit once.

    Uses a fixed ``recorded_at`` for deterministic replay.
    Returns the list of :class:`ApplyResult` objects, one per event.
    """
    results: list[ApplyResult] = []
    for event in events:
        results.append(apply_event(conn, event, recorded_at=recorded_at))
    conn.commit()
    return results
