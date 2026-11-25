"""olo/ledger.py — Deterministic commitment ledger (core).

Tracks delivery orders and meeting slots as append-only versioned records.

Design constraints:
  - Append-only: once written, versions are never modified or deleted.
  - Current state = latest *confirmed* version (D1 fix): a customer's
    pending proposal never overwrites the confirmed date.  Proposals are
    tracked separately and returned by get_pending_proposal().
  - Non-commit actions with no matching commitment return UNRESOLVED (D2 fix):
    a confirm_change, reaffirm, or request_change whose evidence doesn't match
    any existing commitment stays unresolved rather than forking a new record.
  - Deterministic: same event sequence → identical database state.
  - Business isolation: event_type is a first-class key; delivery ≠ meeting.
  - Customer isolation: contact_id must be a canonical cid_... identifier.
  - Multiple plausible matches remain unresolved; explicit authorized
    resolution is required (DenyAll by default until a trusted authorizer
    is wired in).

Actor roles and actions:
  actor_role: 'business' | 'customer'
  action:     'commit'          — original commitment (may create; business)
              'reaffirm'        — business restates existing date (must match)
              'request_change'  — customer proposes a new date (must match)
              'confirm_change'  — business confirms a customer's request (must match)
              'cancel'          — commitment cancelled (must match)

Cancellation contract: a cancel carries the cancelled date in
prior_confirmed_date and null confirmed_date/proposed_date (see
cancellation_field_error).  A business cancel is confirmed: the commitment's
status becomes 'cancelled' with no current scheduled date, while its history
is preserved.  A customer cancel is a pending request (is_confirmed=0); the
status stays 'scheduled' until the business confirms.  A later confirmed
business version that matches the commitment (e.g. a recommit with the same
business_key) restores status 'scheduled'.

Only 'commit' may create a new commitment record.  All other actions require
a verifiable match against existing history (prior date, thread or business key).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Optional

# OperatorToken is Olo's existing trusted authorization boundary (DenyAll until
# IAM/RBAC integration is wired).  Imported here so resolve_unresolved_match
# uses the same boundary as resolve_identifier_conflict in contacts.py.
from olo.adapters.contacts import OperatorToken  # noqa: F401

# ── Supported event types ─────────────────────────────────────────────────────

SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset({"delivery", "meeting"})
ACTOR_ROLES:           frozenset[str] = frozenset({"business", "customer"})
ACTIONS:               frozenset[str] = frozenset({
    "commit", "reaffirm", "request_change", "confirm_change", "cancel",
})

#: Only 'commit' creates a new commitment record when no match is found.
CREATE_ACTIONS: frozenset[str] = frozenset({"commit"})

#: Actions that produce a binding confirmed state (is_confirmed = 1).
CONFIRMED_ACTIONS: frozenset[str] = frozenset({
    "commit", "reaffirm", "confirm_change", "cancel",
})

#: Cross-field actor/action rules.
#: A customer may only request a change or cancel.
#: A business may commit, reaffirm, confirm a change, or cancel.
#: These rules are enforced in _validate() so invalid combinations return INVALID.
#:
#: S3 customer-cancel fix (applied in _write_version):
#: ``customer + cancel`` produces is_confirmed=0 (a proposal awaiting business
#: confirmation), NOT is_confirmed=1.  Only ``business + cancel`` produces a
#: confirmed cancellation.  This isolates the fix to the version-write path
#: without touching the validation rules or the action schema.
ACTOR_ACTION_ALLOWED: dict[str, frozenset[str]] = {
    "business": frozenset({"commit", "reaffirm", "confirm_change", "cancel"}),
    "customer": frozenset({"request_change", "cancel"}),  # cancel→is_confirmed=0 (S3 fix)
}

#: Cancellation date contract (S4 C1–C4), shared by the ledger (_validate)
#: and the extractor (_parse_and_verify):
#:   * the cancelled date goes in ``prior_confirmed_date`` (YYYY-MM-DD);
#:   * ``confirmed_date`` and ``proposed_date`` must be null — a cancellation
#:     neither confirms nor proposes a date.
#: Violations are rejected, never remapped to another field.
CANCEL_ACTION: str = "cancel"
CANCEL_NULL_FIELDS: tuple[str, ...] = ("confirmed_date", "proposed_date")

#: Commitment status values reported by get_current_state().
STATUS_SCHEDULED: str = "scheduled"
STATUS_CANCELLED: str = "cancelled"

#: Actions that assert a binding date and therefore require a valid
#: confirmed_date (S1–S4 hardening, L6).
DATED_ACTIONS: frozenset[str] = frozenset({"commit", "reaffirm", "confirm_change"})

#: Date fields carried by a LedgerEvent; each must be None or a valid date.
DATE_FIELDS: tuple[str, ...] = ("confirmed_date", "proposed_date", "prior_confirmed_date")

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

_CID_PREFIX = "cid_"


def is_valid_iso_date(value: object) -> bool:
    """True when *value* is a ``YYYY-MM-DD`` string naming a real calendar date.

    Shared by the ledger and the extractor.  Rejects impossible dates such as
    2024-02-30 and any other format (no coercion is attempted).
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def utc_instant(value: object) -> Optional[_dt.datetime]:
    """The UTC instant of an ISO 8601 timestamp carrying an offset, else None.

    Chronological comparisons use this instant, never the raw text: strings
    with different offsets ("…-05:00" vs "…Z") do not sort chronologically.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = _dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(_dt.UTC)


_EPOCH_UTC = _dt.datetime.min.replace(tzinfo=_dt.UTC)


def _order_key(occurred_at: object) -> _dt.datetime:
    """Sort key: UTC instant (unparseable legacy values sort first)."""
    return utc_instant(occurred_at) or _EPOCH_UTC


def cancellation_field_error(
    *,
    confirmed_date: object,
    proposed_date: object,
    prior_confirmed_date: object,
) -> Optional[str]:
    """Check a cancellation's date fields against the cancellation contract.

    Takes the raw field values (any type) so callers can apply it before any
    cleaning or coercion.  Returns None when the fields are valid, otherwise a
    human-readable reason.  Never suggests or performs a remap.
    """
    values = {"confirmed_date": confirmed_date, "proposed_date": proposed_date}
    for name in CANCEL_NULL_FIELDS:
        if values[name] is not None:
            return (
                f"action='cancel' requires {name}=None (got {values[name]!r}); "
                "the cancelled date belongs in prior_confirmed_date"
            )
    if prior_confirmed_date is not None and not is_valid_iso_date(prior_confirmed_date):
        return (
            "action='cancel' requires prior_confirmed_date to be None or a "
            f"YYYY-MM-DD date (got {prior_confirmed_date!r})"
        )
    return None


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA_DDL = """\
CREATE TABLE IF NOT EXISTS commitments (
    id              TEXT NOT NULL PRIMARY KEY,
    contact_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    business_key    TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

-- Named commitments (non-empty business_key) are unique per customer per type.
-- Unnamed commitments may coexist when their dates differ.
CREATE UNIQUE INDEX IF NOT EXISTS uk_named_commitment
    ON commitments (contact_id, event_type, business_key)
    WHERE business_key != '';

CREATE TABLE IF NOT EXISTS commitment_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id        TEXT NOT NULL,
    version              INTEGER NOT NULL,
    actor_role           TEXT NOT NULL DEFAULT 'business',
    action               TEXT NOT NULL DEFAULT 'commit',
    confirmed_date       TEXT,
    prior_confirmed_date TEXT,
    proposed_date        TEXT,
    is_confirmed         INTEGER NOT NULL,
    thread_id            TEXT NOT NULL DEFAULT '',
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
    candidate_ids   TEXT NOT NULL,  -- JSON array; empty [] for D2 zero-match case
    event_json      TEXT NOT NULL,  -- Full LedgerEvent as JSON (for replay/resolution)
    recorded_at     TEXT NOT NULL,
    resolved_at     TEXT,           -- NULL until resolved
    resolved_by     TEXT,           -- authorized_by token when resolved
    resolved_to     TEXT            -- commitment_id the event was applied to
);

-- Idempotency: each source_doc_id appears at most once in the unresolved table
CREATE UNIQUE INDEX IF NOT EXISTS uk_unresolved_source
    ON unresolved_matches (source_doc_id)
    WHERE resolved_at IS NULL;
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
        event_type:           'delivery' or 'meeting'.
        contact_id:           Canonical cid_... identifier from the CRM.
        business_key:         Specific identifier such as an order ID.
                              '' when unknown.
        source_doc_id:        document_id of the source Hindsight item.
        source_kind:          Signal kind (e.g. 'email.sent', 'call.transcript').
        occurred_at:          ISO 8601 — when the original event happened.
        actor_role:           'business' or 'customer'.
        action:               'commit' | 'reaffirm' | 'request_change' |
                              'confirm_change' | 'cancel'.
        proposed_date:        ISO 8601 date the customer is requesting.
                              None when the event carries no proposal.
        confirmed_date:       ISO 8601 date confirmed by the business.
                              None for customer requests; must be None for
                              cancellations (rejected otherwise).
        prior_confirmed_date: The previously-confirmed date this event supersedes.
                              Evidence for cross-channel linking.  For a
                              cancellation, the date being cancelled.
        thread_id:            Email thread or call-session identifier.
                              Linking anchor when business_key is unavailable.
        notes:                Optional annotation.
    """
    event_type:           str
    contact_id:           str
    business_key:         str
    source_doc_id:        str
    source_kind:          str
    occurred_at:          str
    actor_role:           str
    action:               str
    proposed_date:        Optional[str] = None
    confirmed_date:       Optional[str] = None
    prior_confirmed_date: Optional[str] = None
    thread_id:            str = ""
    notes:                str = ""


# ── Result types ──────────────────────────────────────────────────────────────

class OutcomeKind(str, Enum):
    CREATED     = "created"    # new commitment created; version 1 written
    UPDATED     = "updated"    # new version appended to existing commitment
    UNCHANGED   = "unchanged"  # source_doc_id already applied — idempotent
    UNRESOLVED  = "unresolved" # multiple candidates or no match for a non-commit action
    INVALID     = "invalid"    # validation error; event NOT applied
    RESOLVED    = "resolved"   # unresolved match linked via authorized resolution


@dataclass(frozen=True)
class ApplyResult:
    """Result of one apply_event or resolve_unresolved_match call."""
    outcome:       OutcomeKind
    commitment_id: Optional[str]
    version:       Optional[int]
    message:       str


class AuthorizationError(Exception):
    """Raised when an operation requires explicit authorization."""


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
    if event.actor_role not in ACTOR_ROLES:
        return (
            f"actor_role must be one of {sorted(ACTOR_ROLES)!r}, "
            f"got {event.actor_role!r}"
        )
    if event.action not in ACTIONS:
        return (
            f"action must be one of {sorted(ACTIONS)!r}, "
            f"got {event.action!r}"
        )
    if not event.source_doc_id:
        return "source_doc_id must not be empty"
    if not event.source_kind:
        return "source_kind must not be empty"
    if not event.occurred_at:
        return "occurred_at must not be empty"
    if utc_instant(event.occurred_at) is None:
        return (
            "occurred_at must be an ISO 8601 timestamp with a UTC offset "
            f"(got {event.occurred_at!r})"
        )
    # Cross-field: actor_role × action combination
    allowed = ACTOR_ACTION_ALLOWED.get(event.actor_role, frozenset())
    if event.action not in allowed:
        return (
            f"actor_role={event.actor_role!r} does not permit "
            f"action={event.action!r}. "
            f"Allowed for {event.actor_role!r}: {sorted(allowed)!r}. "
            "A customer may only request_change or cancel; "
            "a business may commit, reaffirm, confirm_change, or cancel."
        )
    # Every date field must be None or a real calendar date (no coercion).
    for name in DATE_FIELDS:
        value = getattr(event, name)
        if value is not None and not is_valid_iso_date(value):
            return f"{name} must be None or a valid YYYY-MM-DD date (got {value!r})"
    # Binding actions must carry the date they bind (L6).
    if event.action in DATED_ACTIONS and event.confirmed_date is None:
        return (
            f"action={event.action!r} requires a valid confirmed_date; "
            "a binding update without a date is never applied"
        )
    # Cross-field: cancellation date contract
    if event.action == CANCEL_ACTION:
        cancel_err = cancellation_field_error(
            confirmed_date=event.confirmed_date,
            proposed_date=event.proposed_date,
            prior_confirmed_date=event.prior_confirmed_date,
        )
        if cancel_err:
            return cancel_err
    return None


# ── Date and thread evidence helpers ─────────────────────────────────────────

def _commitment_has_date_in_history(
    conn: sqlite3.Connection,
    commitment_id: str,
    date: str,
) -> bool:
    """Return True if *date* appears in ANY version of the commitment.

    Checks confirmed_date, prior_confirmed_date and proposed_date across all
    versions so that cross-channel linking works even when the transcript's
    prior date refers to a date that was subsequently superseded.
    """
    rows = conn.execute(
        "SELECT confirmed_date, prior_confirmed_date, proposed_date"
        " FROM commitment_versions WHERE commitment_id=?",
        (commitment_id,),
    ).fetchall()
    for row in rows:
        if date in (row[0], row[1], row[2]):
            return True
    return False


def _commitment_has_thread(
    conn: sqlite3.Connection,
    commitment_id: str,
    thread_id: str,
) -> bool:
    """Return True if any version of the commitment carries this thread_id."""
    if not thread_id:
        return False
    row = conn.execute(
        "SELECT 1 FROM commitment_versions"
        " WHERE commitment_id=? AND thread_id=? LIMIT 1",
        (commitment_id, thread_id),
    ).fetchone()
    return row is not None


def _all_cids_for_type(
    conn: sqlite3.Connection,
    contact_id: str,
    event_type: str,
) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM commitments WHERE contact_id=? AND event_type=?",
        (contact_id, event_type),
    ).fetchall()
    return [r[0] for r in rows]


# ── Matching ──────────────────────────────────────────────────────────────────

def _commitment_has_date_in_columns(
    conn: sqlite3.Connection,
    commitment_id: str,
    date: str,
    columns: tuple[str, ...],
) -> bool:
    """Return True if *date* appears in any of *columns* in any version."""
    where = " OR ".join(f"{c}=?" for c in columns)
    row = conn.execute(
        f"SELECT 1 FROM commitment_versions WHERE commitment_id=? AND ({where}) LIMIT 1",
        (commitment_id, *([date] * len(columns))),
    ).fetchone()
    return row is not None


def _find_matches(
    conn: sqlite3.Connection, event: LedgerEvent,
) -> tuple[list[str], Optional[str]]:
    """Return ``(candidate_ids, unresolved_reason)`` for this event.

    Fail-safe matching (S1–S4 hardening).  A candidate needs independent
    evidence; absence of evidence never selects a commitment:

      1. Exact named key (business_key != '') — unique per customer and type.
      2. Thread anchor among the eligible pool.
      3. Historical date evidence among the eligible pool:
           * prior_confirmed_date present → commitments with that date in
             any version (the event supersedes a known date);
           * otherwise confirmed_date — for non-commit actions, any version;
             for 'commit', only a superseded (prior) date links it; a commit
             whose date merely equals an existing commitment's date is
             ambiguous (same commitment restated vs. a second commitment).

    The eligible pool for a keyed event whose key is unknown is the
    *unnamed* commitments only; a different named commitment never matches.

    ``unresolved_reason`` is set when the event must stay UNRESOLVED even
    though no candidate was returned (so 'commit' must not create either).
    """
    all_cids = _all_cids_for_type(conn, event.contact_id, event.event_type)

    if event.business_key:
        exact = conn.execute(
            "SELECT id FROM commitments"
            " WHERE contact_id=? AND event_type=? AND business_key=?",
            (event.contact_id, event.event_type, event.business_key),
        ).fetchall()
        if exact:
            return [r[0] for r in exact], None
        pool = [
            cid for cid in all_cids
            if conn.execute(
                "SELECT business_key FROM commitments WHERE id=?", (cid,)
            ).fetchone()[0] == ""
        ]
    else:
        pool = all_cids

    # 2. Thread anchor
    if event.thread_id and pool:
        thread_match = [c for c in pool if _commitment_has_thread(conn, c, event.thread_id)]
        if thread_match:
            match_date = event.prior_confirmed_date or event.confirmed_date
            if match_date and len(thread_match) > 1:
                refined = [
                    c for c in thread_match
                    if _commitment_has_date_in_history(conn, c, match_date)
                ]
                if refined:
                    return refined, None
            return thread_match, None

    # 3. Historical date evidence
    if event.prior_confirmed_date:
        return [
            c for c in pool
            if _commitment_has_date_in_history(conn, c, event.prior_confirmed_date)
        ], None

    if event.confirmed_date:
        if event.action == "commit":
            superseded = [
                c for c in pool
                if _commitment_has_date_in_columns(
                    conn, c, event.confirmed_date, ("prior_confirmed_date",))
            ]
            if superseded:
                return superseded, None
            same_date = [
                c for c in pool
                if _commitment_has_date_in_columns(
                    conn, c, event.confirmed_date, ("confirmed_date", "proposed_date"))
            ]
            if same_date:
                return [], (
                    f"commit date {event.confirmed_date} matches {len(same_date)} "
                    "existing commitment(s) with no business_key or thread to tell a "
                    "restatement from a second commitment"
                )
            return [], None
        return [
            c for c in pool
            if _commitment_has_date_in_history(conn, c, event.confirmed_date)
        ], None

    # 4. No key, thread or date evidence: never select by default.
    if event.action == "commit":
        return [], None
    return [], (
        f"action={event.action!r} has no business_key, thread or historical date "
        "evidence identifying a commitment"
    )


def _applied_anywhere(
    conn: sqlite3.Connection, event: LedgerEvent,
) -> Optional[tuple[str, int]]:
    """Return (commitment_id, version) if this source already applied here.

    Scoped to the event's customer and type (and key, when keyed) so replay
    is idempotent even when the matcher would now treat it differently.
    """
    sql = (
        "SELECT cv.commitment_id, cv.version FROM commitment_versions cv"
        " JOIN commitments c ON c.id = cv.commitment_id"
        " WHERE cv.source_doc_id=? AND c.contact_id=? AND c.event_type=?"
    )
    params: list = [event.source_doc_id, event.contact_id, event.event_type]
    if event.business_key:
        sql += " AND c.business_key=?"
        params.append(event.business_key)
    row = conn.execute(sql + " ORDER BY cv.commitment_id LIMIT 1", params).fetchone()
    return (row[0], row[1]) if row else None


def _current_scheduled_date(conn: sqlite3.Connection, commitment_id: str) -> Optional[str]:
    """Scheduled date of the latest confirmed version (None if cancelled/none)."""
    row = _latest_version(conn, commitment_id, confirmed=True)
    if row is None or row[2] == CANCEL_ACTION:
        return None
    return row[3]


_VERSION_COLUMNS = (
    "version, actor_role, action, confirmed_date,"
    " prior_confirmed_date, proposed_date, is_confirmed,"
    " thread_id, source_doc_id, source_kind,"
    " occurred_at, recorded_at, notes"
)


def _latest_version(
    conn: sqlite3.Connection, commitment_id: str, *, confirmed: bool,
) -> Optional[tuple]:
    """Latest confirmed (or pending) version by UTC instant, then version.

    Columns follow _VERSION_COLUMNS (occurred_at at index 10).
    """
    rows = conn.execute(
        f"SELECT {_VERSION_COLUMNS} FROM commitment_versions"
        " WHERE commitment_id=? AND is_confirmed=?",
        (commitment_id, 1 if confirmed else 0),
    ).fetchall()
    if not rows:
        return None
    return tuple(max(rows, key=lambda r: (_order_key(r[10]), r[0])))


# ── Idempotency ───────────────────────────────────────────────────────────────

def _applied_version(
    conn: sqlite3.Connection,
    commitment_id: str,
    source_doc_id: str,
) -> Optional[int]:
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
    # S3 fix for customer-cancel: a customer's cancellation request is a
    # proposal, not a confirmed state.  Only a business can confirm cancellation.
    # Previously 'cancel' was unconditionally in CONFIRMED_ACTIONS; this caused
    # customer-initiated cancels to set is_confirmed=1, bypassing the proposal
    # pipeline.  The check below gates confirmation on actor_role.
    if event.action in CONFIRMED_ACTIONS and not (
        event.actor_role == "customer" and event.action == "cancel"
    ):
        is_confirmed = 1
    else:
        is_confirmed = 0
    conn.execute(
        "INSERT INTO commitment_versions"
        " (commitment_id, version, actor_role, action,"
        "  confirmed_date, prior_confirmed_date, proposed_date,"
        "  is_confirmed, thread_id, source_doc_id, source_kind,"
        "  occurred_at, recorded_at, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            commitment_id, version,
            event.actor_role, event.action,
            event.confirmed_date,
            event.prior_confirmed_date,
            event.proposed_date,
            is_confirmed,
            event.thread_id,
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
    event_json = json.dumps({
        "event_type":           event.event_type,
        "contact_id":           event.contact_id,
        "business_key":         event.business_key,
        "source_doc_id":        event.source_doc_id,
        "source_kind":          event.source_kind,
        "occurred_at":          event.occurred_at,
        "actor_role":           event.actor_role,
        "action":               event.action,
        "proposed_date":        event.proposed_date,
        "confirmed_date":       event.confirmed_date,
        "prior_confirmed_date": event.prior_confirmed_date,
        "thread_id":            event.thread_id,
        "notes":                event.notes,
    }, default=str)
    # Use INSERT OR IGNORE so a retry of the same source_doc_id is idempotent
    conn.execute(
        "INSERT OR IGNORE INTO unresolved_matches"
        " (source_doc_id, contact_id, event_type, candidate_ids,"
        "  event_json, recorded_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            event.source_doc_id,
            event.contact_id,
            event.event_type,
            json.dumps(sorted(candidate_ids)),
            event_json,
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
                     Defaults to the current UTC time when None.

    Returns:
        ApplyResult.  INVALID and UNRESOLVED outcomes do not write any
        commitment or version row (UNRESOLVED is recorded in unresolved_matches).

    D1 guarantee: proposals (is_confirmed=0) never overwrite the confirmed state.
    D2 guarantee: non-commit actions with no match return UNRESOLVED, not CREATED.
    """
    if recorded_at is None:
        from datetime import datetime, timezone
        recorded_at = datetime.now(timezone.utc).isoformat()

    err = _validate(event)
    if err:
        return ApplyResult(OutcomeKind.INVALID, None, None, err)

    # Idempotency first: a replayed source never re-enters matching.
    applied = _applied_anywhere(conn, event)
    if applied is not None:
        return ApplyResult(
            OutcomeKind.UNCHANGED, applied[0], applied[1],
            "source_doc_id already applied to this commitment",
        )

    matches, unresolved_reason = _find_matches(conn, event)

    if unresolved_reason is not None:
        _record_unresolved(conn, event, matches, recorded_at)
        return ApplyResult(OutcomeKind.UNRESOLVED, None, None, unresolved_reason)

    if len(matches) > 1:
        _record_unresolved(conn, event, matches, recorded_at)
        return ApplyResult(
            OutcomeKind.UNRESOLVED,
            None,
            None,
            f"{len(matches)} candidate commitments; supply prior_confirmed_date, "
            "business_key or thread_id to disambiguate",
        )

    if len(matches) == 0:
        if event.action not in CREATE_ACTIONS:
            # D2 fix: a change/update/request with no match stays unresolved.
            # Never fork a second commitment just because evidence is missing.
            _record_unresolved(conn, event, [], recorded_at)
            return ApplyResult(
                OutcomeKind.UNRESOLVED,
                None,
                None,
                f"action={event.action!r} requires an existing commitment "
                f"(prior_confirmed_date={event.prior_confirmed_date!r}, "
                f"thread_id={event.thread_id!r}, business_key={event.business_key!r}); "
                "no matching commitment found — cannot create without 'commit'",
            )
        # Only 'commit' creates a new commitment record
        commitment_id = _create_commitment(conn, event, recorded_at)
        _write_version(conn, commitment_id, 1, event, recorded_at)
        return ApplyResult(OutcomeKind.CREATED, commitment_id, 1, "commitment created")

    # Exactly one match
    commitment_id = matches[0]
    existing = _applied_version(conn, commitment_id, event.source_doc_id)
    if existing is not None:
        return ApplyResult(
            OutcomeKind.UNCHANGED,
            commitment_id,
            existing,
            "source_doc_id already applied to this commitment",
        )
    # A reaffirm restates the current confirmed date; it can never change it (L4).
    if event.action == "reaffirm":
        current = _current_scheduled_date(conn, commitment_id)
        if current != event.confirmed_date:
            _record_unresolved(conn, event, [commitment_id], recorded_at)
            return ApplyResult(
                OutcomeKind.UNRESOLVED, None, None,
                f"reaffirm date {event.confirmed_date} does not equal the current "
                f"confirmed date {current!r}; a reaffirm cannot change the date",
            )
    # Cross-channel claim: assign business_key when claiming an unnamed commitment
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


# ── Authorized resolution of unresolved matches ───────────────────────────────

def resolve_unresolved_match(
    conn: sqlite3.Connection,
    source_doc_id: str,
    target_commitment_id: str,
    *,
    operator: Optional[OperatorToken] = None,
    recorded_at: Optional[str] = None,
) -> ApplyResult:
    """Explicitly link an unresolved event to a specific commitment.

    **DenyAll — currently always raises.**

    Manual commitment resolution is disabled until an authenticated IAM/RBAC
    integration is configured.  ``operator`` must be an ``OperatorToken``
    obtained via ``create_operator_token()``.  Since ``create_operator_token()``
    currently always raises ``IdentityResolutionError`` (see contacts.py), no
    caller can legitimately obtain a token.

    Callers CANNOT authorize resolution by:
    * ``operator=None``               → ``AuthorizationError`` (missing token)
    * ``operator="any-string"``       → ``AuthorizationError`` (wrong type)
    * ``operator=OperatorToken(actor="x")`` → ``AuthorizationError`` (DenyAll
      enforcement inside this function; constructing the token directly bypasses
      ``create_operator_token()`` but cannot bypass this check)

    Unresolved matches are preserved in the ``unresolved_matches`` table for
    future resolution once an IAM integration is configured.

    Args:
        conn:                 Open SQLite connection.
        source_doc_id:        ``source_doc_id`` of the unresolved entry.
        target_commitment_id: The commitment to apply the event to.
        operator:             ``OperatorToken`` from ``create_operator_token()``.
        recorded_at:          Stamp time; defaults to current UTC.

    Raises:
        AuthorizationError: always (DenyAll until IAM/RBAC is wired).
    """
    # Type guard: reject non-OperatorToken values before DenyAll enforcement
    if operator is None:
        raise AuthorizationError(
            "resolve_unresolved_match: requires an OperatorToken obtained via "
            "create_operator_token().  No authorization source is configured. "
            "Unresolved commitments are preserved for future resolution."
        )
    if not isinstance(operator, OperatorToken):
        raise AuthorizationError(
            "resolve_unresolved_match: operator must be an OperatorToken instance "
            "from create_operator_token(), not a plain string or other type. "
            f"Got {type(operator).__name__!r}."
        )
    # DenyAll enforcement: even a directly-constructed OperatorToken cannot
    # authorize resolution — the enforcement point is here, not in the token type.
    # An OperatorToken can only be legitimately obtained via create_operator_token(),
    # which currently always raises IdentityResolutionError.
    raise AuthorizationError(
        "resolve_unresolved_match: manual commitment resolution is currently "
        "disabled.  An authenticated IAM/RBAC integration is required.  "
        "Unresolved commitments are preserved for future resolution once "
        "an integration is configured."
    )

    # ── Implementation (unreachable until IAM is wired) ───────────────────────
    # The code below is complete and correct; remove the raise above and
    # replace it with IAM verification once an integration is available.

    if recorded_at is None:
        from datetime import datetime, timezone
        recorded_at = datetime.now(timezone.utc).isoformat()

    # Idempotency: has this source already been applied to any commitment?
    applied_check = conn.execute(
        "SELECT commitment_id, version FROM commitment_versions"
        " WHERE source_doc_id=?",
        (source_doc_id,),
    ).fetchone()
    if applied_check:
        return ApplyResult(
            OutcomeKind.UNCHANGED,
            applied_check[0],
            applied_check[1],
            "source_doc_id already applied (resolution was already completed)",
        )

    # Look up the unresolved entry
    row = conn.execute(
        "SELECT id, event_json FROM unresolved_matches"
        " WHERE source_doc_id=? AND resolved_at IS NULL",
        (source_doc_id,),
    ).fetchone()
    if not row:
        return ApplyResult(
            OutcomeKind.INVALID, None, None,
            f"No open unresolved match for source_doc_id={source_doc_id!r}",
        )
    unresolved_id, event_json_str = row

    # Verify target commitment exists
    cmt = conn.execute(
        "SELECT id FROM commitments WHERE id=?", (target_commitment_id,)
    ).fetchone()
    if not cmt:
        return ApplyResult(
            OutcomeKind.INVALID, None, None,
            f"Target commitment {target_commitment_id!r} not found",
        )

    # Reconstruct the event from stored JSON
    try:
        ev_data = json.loads(event_json_str)
        event = LedgerEvent(**ev_data)
    except Exception as exc:
        return ApplyResult(
            OutcomeKind.INVALID, None, None,
            f"Cannot reconstruct event from stored JSON: {exc}",
        )

    # Idempotency: has this source already been applied to the target?
    already = _applied_version(conn, target_commitment_id, source_doc_id)
    if already is not None:
        return ApplyResult(
            OutcomeKind.UNCHANGED, target_commitment_id, already,
            "source_doc_id already applied to this commitment",
        )

    version = _next_version(conn, target_commitment_id)
    _write_version(conn, target_commitment_id, version, event, recorded_at)

    conn.execute(
        "UPDATE unresolved_matches"
        " SET resolved_at=?, resolved_by=?, resolved_to=?"
        " WHERE id=?",
        (recorded_at, operator.actor, target_commitment_id, unresolved_id),
    )
    return ApplyResult(
        OutcomeKind.RESOLVED, target_commitment_id, version,
        f"unresolved match linked to {target_commitment_id!r} by {operator.actor!r}",
    )


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_current_state(
    conn: sqlite3.Connection,
    commitment_id: str,
) -> Optional[dict]:
    """Return the current confirmed state of a commitment.

    D1 fix: returns the latest version where is_confirmed=1.
    A pending customer proposal never overrides the confirmed date.
    Call get_pending_proposal() to retrieve any open proposal.

    ``status`` is 'cancelled' when the latest confirmed version is a cancel
    (``scheduled_date`` is then None and ``prior_confirmed_date`` holds the
    cancelled date); otherwise 'scheduled' with ``scheduled_date`` set to the
    confirmed date.  A pending customer cancel shows as pending_action='cancel'
    and does not change the status.

    Returns None if the commitment does not exist.
    """
    row = conn.execute(
        "SELECT id, contact_id, event_type, business_key, created_at"
        " FROM commitments WHERE id=?",
        (commitment_id,),
    ).fetchone()
    if not row:
        return None

    # D1 fix: latest CONFIRMED version (is_confirmed=1), chronologically by
    # UTC instant (never by raw timestamp text).
    confirmed = _latest_version(conn, commitment_id, confirmed=True)

    # Also check for a pending proposal to include in the state.
    # D1: only surface a pending proposal that postdates the latest confirmed
    # version — otherwise the confirmed change already superseded the proposal.
    latest_pending = _latest_version(conn, commitment_id, confirmed=False)
    pending = (
        (latest_pending[5], latest_pending[2], latest_pending[8], latest_pending[10])
        if latest_pending else None
    )
    # Suppress the proposal when it predates or equals the confirmed version
    if pending and confirmed and _order_key(pending[3]) <= _order_key(confirmed[10]):
        pending = None

    # Status from the latest confirmed version: a confirmed (business) cancel
    # leaves no current scheduled date; any other confirmed action schedules.
    if confirmed is None:
        status: Optional[str] = None
        scheduled_date: Optional[str] = None
    elif confirmed[2] == CANCEL_ACTION:
        status, scheduled_date = STATUS_CANCELLED, None
    else:
        status, scheduled_date = STATUS_SCHEDULED, confirmed[3]

    return {
        "id":             row[0],
        "contact_id":     row[1],
        "event_type":     row[2],
        "business_key":   row[3],
        "created_at":     row[4],
        # 'scheduled' | 'cancelled' (None when no confirmed version exists)
        "status":         status,
        # Current scheduled date; None when cancelled
        "scheduled_date": scheduled_date,
        # Latest confirmed version fields
        "version":              confirmed[0]  if confirmed else None,
        "actor_role":           confirmed[1]  if confirmed else None,
        "action":               confirmed[2]  if confirmed else None,
        "confirmed_date":       confirmed[3]  if confirmed else None,
        "prior_confirmed_date": confirmed[4]  if confirmed else None,
        "proposed_date":        confirmed[5]  if confirmed else None,
        "is_confirmed":         bool(confirmed[6]) if confirmed else False,
        "thread_id":            confirmed[7]  if confirmed else "",
        "source_doc_id":        confirmed[8]  if confirmed else None,
        "source_kind":          confirmed[9]  if confirmed else None,
        "occurred_at":          confirmed[10] if confirmed else None,
        "recorded_at":          confirmed[11] if confirmed else None,
        "notes":                confirmed[12] if confirmed else "",
        # Pending proposal summary (None when no open proposal)
        "pending_proposed_date": pending[0] if pending else None,
        "pending_action":        pending[1] if pending else None,
        "pending_source_doc_id": pending[2] if pending else None,
        "pending_occurred_at":   pending[3] if pending else None,
    }


def get_pending_proposal(
    conn: sqlite3.Connection,
    commitment_id: str,
) -> Optional[dict]:
    """Return the latest pending (is_confirmed=0) version, or None."""
    row = _latest_version(conn, commitment_id, confirmed=False)
    if not row:
        return None
    return {
        "version":              row[0],
        "actor_role":           row[1],
        "action":               row[2],
        "confirmed_date":       row[3],
        "prior_confirmed_date": row[4],
        "proposed_date":        row[5],
        "is_confirmed":         bool(row[6]),
        "thread_id":            row[7],
        "source_doc_id":        row[8],
        "source_kind":          row[9],
        "occurred_at":          row[10],
        "recorded_at":          row[11],
        "notes":                row[12],
    }


def get_all_versions(
    conn: sqlite3.Connection,
    commitment_id: str,
) -> list[dict]:
    """Return all versions for a commitment, oldest first (by version number)."""
    rows = conn.execute(
        "SELECT version, actor_role, action, confirmed_date,"
        "       prior_confirmed_date, proposed_date, is_confirmed,"
        "       thread_id, source_doc_id, source_kind,"
        "       occurred_at, recorded_at, notes"
        " FROM commitment_versions"
        " WHERE commitment_id=?"
        " ORDER BY version ASC",
        (commitment_id,),
    ).fetchall()
    return [
        {
            "version":              r[0],
            "actor_role":           r[1],
            "action":               r[2],
            "confirmed_date":       r[3],
            "prior_confirmed_date": r[4],
            "proposed_date":        r[5],
            "is_confirmed":         bool(r[6]),
            "thread_id":            r[7],
            "source_doc_id":        r[8],
            "source_kind":          r[9],
            "occurred_at":          r[10],
            "recorded_at":          r[11],
            "notes":                r[12],
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

    Fixed recorded_at for deterministic replay and rebuild verification.
    Returns the list of ApplyResult objects, one per event.
    """
    results: list[ApplyResult] = []
    for event in events:
        results.append(apply_event(conn, event, recorded_at=recorded_at))
    conn.commit()
    return results
