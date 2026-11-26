"""olo/operator.py — authorized operator decisions on what Olo will not guess.

Olo leaves three things for a person: a ledger event with no unique,
evidenced commitment (``unresolved_matches``), a signal with more than one
plausible customer (``identity_status='ambiguous'``) and an identifier two
contacts claim (``identifier_conflicts``).  Until post-audit batch 3 the only
resolution entry points were DenyAll stubs taking an ``OperatorToken`` anyone
could construct.  Those stay DenyAll.  This module is the concrete,
authorized mechanism beside them.

Authorization.  An operator enrols a passphrase for this business
(``hermes olo operator enroll``); olo.db keeps only a salted PBKDF2-SHA256
hash.  The first operator of a business may enrol on their own (bootstrap);
every later enrolment and every revocation needs a current operator's grant.
``authorize`` exchanges the passphrase for an ``OperatorGrant``: a random
secret, stored hashed, valid for GRANT_TTL.  Each decision re-verifies the
grant against olo.db, so a directly constructed ``OperatorGrant``, an
expired or revoked grant, or a grant of a revoked operator is refused.  The
model, the Hermes agent and the cron worker hold no passphrase.

Boundary.  This protects against automated and model-driven paths and
against mistakes.  It is not a defence against someone with the business's
OS account, who can edit olo.db directly.

Audit.  Every decision, every refusal and every failed authorization is a
row in ``operator_decisions``, append-only (SQLite triggers reject UPDATE and
DELETE), written in the same transaction as its effect.

Decisions on an unresolved ledger event (``resolve_commitment``):
  link      apply it to an existing commitment of the same customer and type
  distinct  record it as a new commitment ('commit' events only)
  dismiss   settle it with no ledger change (the evidence stays)
The ledger's own rules still apply (olo.ledger.apply_operator_resolution).
Events that waited behind the decided one (``blocked_by``) are then
re-applied through the normal matcher; whatever is still ambiguous stays
unresolved.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from olo.ledger import (
    AuthorizationError,
    OutcomeKind,
    apply_event,
    apply_operator_resolution,
    event_from_json,
    utc_instant,
)
from olo.ledger import migrate as migrate_ledger

__all__ = [
    "GRANT_TTL",
    "OperatorGrant",
    "Resolution",
    "assign_signal_identity",
    "authorize",
    "enroll",
    "migrate",
    "operators",
    "resolve_commitment",
    "resolve_identifier",
    "revoke",
]

GRANT_TTL = timedelta(minutes=15)
MIN_PASSPHRASE = 12
_ITERATIONS = 600_000

_DDL = """\
CREATE TABLE IF NOT EXISTS operator_credentials (
    actor        TEXT PRIMARY KEY,
    salt         BLOB NOT NULL,
    key_hash     BLOB NOT NULL,
    iterations   INTEGER NOT NULL,
    enrolled_at  TEXT NOT NULL,
    enrolled_by  TEXT NOT NULL,
    revoked_at   TEXT
);
CREATE TABLE IF NOT EXISTS operator_grants (
    grant_id     TEXT PRIMARY KEY,
    actor        TEXT NOT NULL,
    secret_hash  TEXT NOT NULL,
    issued_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_decisions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    decided_at   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    grant_id     TEXT NOT NULL DEFAULT '',
    subject_kind TEXT NOT NULL,   -- operator | commitment_match | signal_identity | identifier
    subject      TEXT NOT NULL,
    decision     TEXT NOT NULL,
    target       TEXT NOT NULL DEFAULT '',
    reason       TEXT NOT NULL,
    result       TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS operator_decisions_no_update
    BEFORE UPDATE ON operator_decisions
    BEGIN SELECT RAISE(ABORT, 'operator_decisions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS operator_decisions_no_delete
    BEFORE DELETE ON operator_decisions
    BEGIN SELECT RAISE(ABORT, 'operator_decisions is append-only'); END;
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Create the operator tables (and the ledger's).  Idempotent."""
    migrate_ledger(conn)
    conn.executescript(_DDL)
    conn.commit()


@dataclass(frozen=True)
class OperatorGrant:
    """Proof of a recent successful ``authorize``; checked on every use."""
    actor: str
    grant_id: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class Resolution:
    decision_id: int
    outcome: str                     # applied | dismissed | refused
    commitment_id: Optional[str]
    message: str
    replayed: tuple[str, ...] = ()   # blocked events applied afterwards


def _hash(passphrase: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _audit(conn: sqlite3.Connection, now: str, actor: str, grant_id: str, subject_kind: str,
           subject: str, decision: str, target: str, reason: str, result: str) -> int:
    return conn.execute(
        "INSERT INTO operator_decisions (decided_at, actor, grant_id, subject_kind, subject,"
        " decision, target, reason, result) VALUES (?,?,?,?,?,?,?,?,?)",
        (now, actor, grant_id, subject_kind, subject, decision, target or "", reason,
         result[:500])).lastrowid


def operators(conn: sqlite3.Connection) -> list[str]:
    """Actors with an active credential."""
    migrate(conn)
    return [r[0] for r in conn.execute(
        "SELECT actor FROM operator_credentials WHERE revoked_at IS NULL ORDER BY actor")]


def _verify(conn: sqlite3.Connection, grant: object, now: str) -> OperatorGrant:
    if not isinstance(grant, OperatorGrant):
        raise AuthorizationError(f"an OperatorGrant from authorize() is required, got {type(grant).__name__}")
    row = conn.execute(
        "SELECT g.secret_hash, g.expires_at, c.revoked_at FROM operator_grants g"
        " JOIN operator_credentials c ON c.actor = g.actor"
        " WHERE g.grant_id=? AND g.actor=?", (grant.grant_id, grant.actor)).fetchone()
    if row is None or not hmac.compare_digest(row[0], _sha(grant.secret)):
        raise AuthorizationError("operator grant not recognised")
    if row[2] is not None:
        raise AuthorizationError(f"operator {grant.actor!r} has been revoked")
    if utc_instant(now) >= utc_instant(row[1]):
        raise AuthorizationError("operator grant expired; authorize again")
    return grant


def _valid_actor(actor: str) -> str:
    actor = actor.strip()
    if not actor or len(actor) > 64 or not all(ch.isalnum() or ch in "._-@" for ch in actor):
        raise ValueError("operator name: 1-64 letters, digits or . _ - @")
    return actor


def enroll(conn: sqlite3.Connection, actor: str, passphrase: str, *, now: str,
           grant: Optional[OperatorGrant] = None) -> None:
    """Register *actor*'s passphrase.  The first operator needs no grant."""
    migrate(conn)
    actor = _valid_actor(actor)
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE} characters")
    conn.execute("BEGIN IMMEDIATE")
    try:
        active = conn.execute(
            "SELECT COUNT(*) FROM operator_credentials WHERE revoked_at IS NULL").fetchone()[0]
        if active:
            by = _verify(conn, grant, now).actor
        else:
            by = "bootstrap"
        if conn.execute("SELECT 1 FROM operator_credentials WHERE actor=?", (actor,)).fetchone():
            raise ValueError(f"operator {actor!r} already exists")
        salt = secrets.token_bytes(16)
        conn.execute(
            "INSERT INTO operator_credentials (actor, salt, key_hash, iterations, enrolled_at,"
            " enrolled_by) VALUES (?,?,?,?,?,?)",
            (actor, salt, _hash(passphrase, salt, _ITERATIONS), _ITERATIONS, now, by))
        _audit(conn, now, by, grant.grant_id if grant else "", "operator", actor, "enroll",
               "", "operator enrolled", "applied")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def revoke(conn: sqlite3.Connection, grant: OperatorGrant, actor: str, *, now: str,
           reason: str) -> None:
    """Revoke *actor*'s credential (and so every grant they hold)."""
    migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        by = _verify(conn, grant, now)
        n = conn.execute("UPDATE operator_credentials SET revoked_at=? WHERE actor=?"
                         " AND revoked_at IS NULL", (now, actor)).rowcount
        _audit(conn, now, by.actor, by.grant_id, "operator", actor, "revoke", "", reason,
               "applied" if n else "refused: no active operator")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def authorize(conn: sqlite3.Connection, actor: str, passphrase: str, *, now: str) -> OperatorGrant:
    """Exchange a passphrase for a short-lived grant; failures are audited."""
    migrate(conn)
    row = conn.execute(
        "SELECT salt, key_hash, iterations FROM operator_credentials"
        " WHERE actor=? AND revoked_at IS NULL", (actor,)).fetchone()
    ok = row is not None and hmac.compare_digest(_hash(passphrase, row[0], row[2]), row[1])
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not ok:
            _audit(conn, now, actor, "", "operator", actor, "authorize", "",
                   "authorization attempt", "refused: unknown operator or wrong passphrase")
            conn.execute("COMMIT")
            raise AuthorizationError("unknown operator or wrong passphrase")
        grant = OperatorGrant(actor, secrets.token_hex(8), secrets.token_urlsafe(32))
        expires = utc_instant(now) + GRANT_TTL
        conn.execute(
            "INSERT INTO operator_grants (grant_id, actor, secret_hash, issued_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            (grant.grant_id, actor, _sha(grant.secret), now, expires.isoformat()))
        _audit(conn, now, actor, grant.grant_id, "operator", actor, "authorize", "",
               "authorization", "applied")
        conn.execute("COMMIT")
    except AuthorizationError:
        raise
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return grant


# ── Ledger: unresolved events ─────────────────────────────────────────────────

_DECISIONS = ("link", "distinct", "dismiss")
_SETTLED = (OutcomeKind.CREATED, OutcomeKind.UPDATED, OutcomeKind.UNCHANGED)


def _settle_signal(conn: sqlite3.Connection, notes: str, outcome: str, reason: str,
                   commitment_id: Optional[str], version: Optional[int], now: str) -> None:
    """Move the event's signal (``notes`` = ``sig:<id>``) out of 'unresolved'."""
    if not notes.startswith("sig:"):
        return
    conn.execute(
        "UPDATE signal_resolution SET commitment_status='resolved', commitment_outcome=?,"
        " commitment_reason=?, commitment_id=?, commitment_version=?, updated_at=?"
        " WHERE signal_id=? AND commitment_status='unresolved'",
        (outcome, reason[:500], commitment_id, version, now, notes[4:]))


def _replay_blocked(conn: sqlite3.Connection, decided: str, decision_id: int,
                    now: str) -> list[str]:
    """Re-apply events that waited behind *decided*, cascading; return them."""
    replayed: list[str] = []
    frontier = [decided]
    while frontier:
        doc = frontier.pop(0)
        rows = conn.execute(
            "SELECT u.id, u.source_doc_id, u.event_json FROM unresolved_matches u"
            " WHERE u.resolved_at IS NULL AND EXISTS"
            "  (SELECT 1 FROM json_each(u.blocked_by) WHERE json_each.value = ?)"
            " ORDER BY u.id", (doc,)).fetchall()
        for row_id, source_doc_id, raw in rows:
            event = event_from_json(raw)
            result = apply_event(conn, event, recorded_at=now)
            if result.outcome not in _SETTLED:
                continue                  # still ambiguous: its row was refreshed
            conn.execute(
                "UPDATE unresolved_matches SET resolved_at=?, resolved_by=?, resolved_to=?,"
                " decision_id=? WHERE id=?",
                (now, f"replay:{decision_id}", result.commitment_id, decision_id, row_id))
            _settle_signal(conn, event.notes, "applied",
                           f"applied after operator decision {decision_id}: {result.message}",
                           result.commitment_id, result.version, now)
            replayed.append(source_doc_id)
            frontier.append(source_doc_id)
    return replayed


def resolve_commitment(conn: sqlite3.Connection, grant: OperatorGrant, source_doc_id: str,
                       decision: str, *, reason: str, now: str,
                       target: Optional[str] = None) -> Resolution:
    """Decide an open unresolved ledger event (see the module docstring)."""
    migrate(conn)
    if decision not in _DECISIONS:
        raise ValueError(f"decision must be one of {_DECISIONS}")
    if not reason.strip():
        raise ValueError("a reason is required")
    if (decision == "link") != (target is not None):
        raise ValueError("'link' needs a target commitment; 'distinct' and 'dismiss' take none")
    conn.execute("BEGIN IMMEDIATE")
    try:
        g = _verify(conn, grant, now)
        row = conn.execute(
            "SELECT id, event_json FROM unresolved_matches"
            " WHERE source_doc_id=? AND resolved_at IS NULL", (source_doc_id,)).fetchone()
        if row is None:
            raise LookupError(f"no open unresolved event {source_doc_id!r}")
        event = event_from_json(row[1])
        commitment_id: Optional[str] = None
        version: Optional[int] = None
        if decision == "dismiss":
            outcome, message = "dismissed", "dismissed; no ledger change"
        else:
            applied = apply_operator_resolution(conn, event, target, recorded_at=now)
            if applied.outcome not in _SETTLED:
                did = _audit(conn, now, g.actor, g.grant_id, "commitment_match", source_doc_id,
                             decision, target or "", reason, f"refused: {applied.message}")
                conn.execute("COMMIT")
                return Resolution(did, "refused", None, applied.message)
            outcome, message = "applied", applied.message
            commitment_id, version = applied.commitment_id, applied.version
        did = _audit(conn, now, g.actor, g.grant_id, "commitment_match", source_doc_id,
                     decision, target or commitment_id or "", reason, f"{outcome}: {message}")
        conn.execute(
            "UPDATE unresolved_matches SET resolved_at=?, resolved_by=?, resolved_to=?,"
            " decision_id=? WHERE id=?",
            (now, g.actor, commitment_id or "", did, row[0]))
        _settle_signal(conn, event.notes, f"operator_{decision}",
                       f"operator {g.actor} ({decision}, decision {did}): {reason}",
                       commitment_id, version, now)
        replayed = _replay_blocked(conn, source_doc_id, did, now)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return Resolution(did, outcome, commitment_id, message, tuple(replayed))


# ── Identity ──────────────────────────────────────────────────────────────────

def _candidate_contacts(conn: sqlite3.Connection, source: str, kind: str,
                        payload: dict) -> set[str]:
    """Contacts an operator may assign this signal to: the owners of its
    plausible customer addresses (and, for a calendar stub, the customers of
    the event's earlier revisions)."""
    from olo.adapters.contacts import load_own_identifiers, plausible_customer_emails
    from olo.calendar_commitments import CALENDAR_EVENT_KINDS
    emails = plausible_customer_emails(kind, payload, load_own_identifiers(conn))
    found = {r[0] for r in conn.execute(
        "SELECT contact_id FROM contact_identifiers WHERE identifier_type='email'"
        f" AND revoked_at IS NULL AND identifier_value IN ({','.join('?' * len(emails))})",
        emails)} if emails else set()
    if kind in CALENDAR_EVENT_KINDS and payload.get("event_id"):
        ids = sorted({payload["event_id"], payload.get("recurring_event_id") or payload["event_id"]})
        found |= {r[0] for r in conn.execute(
            "SELECT sr.contact_id FROM signals s JOIN signal_resolution sr"
            "  ON sr.signal_id=s.external_id AND sr.source=s.source"
            " WHERE s.source=? AND sr.identity_status='resolved'"
            f"  AND json_extract(s.payload,'$.event_id') IN ({','.join('?' * len(ids))})",
            (source, *ids))}
    return found


def assign_signal_identity(conn: sqlite3.Connection, grant: OperatorGrant, source: str,
                           signal_id: str, contact_id: str, *, reason: str, now: str) -> int:
    """Settle an ambiguous signal's customer; returns the decision id.

    The contact must be one the signal itself names (see
    _candidate_contacts); the commitment stage then processes the signal on
    the next run as for any verified identity.
    """
    migrate(conn)
    if not reason.strip():
        raise ValueError("a reason is required")
    conn.execute("BEGIN IMMEDIATE")
    try:
        g = _verify(conn, grant, now)
        row = conn.execute(
            "SELECT s.kind, s.payload, sr.identity_status FROM signal_resolution sr"
            " JOIN signals s ON s.external_id=sr.signal_id AND s.source=sr.source"
            " WHERE sr.signal_id=? AND sr.source=?", (signal_id, source)).fetchone()
        if row is None:
            raise LookupError(f"no signal {signal_id!r} in {source!r}")
        if row[2] not in ("ambiguous", "unresolved"):
            raise ValueError(f"signal identity is {row[2]!r}; only ambiguous or unresolved "
                             "identities are decided by an operator")
        payload = json.loads(row[1]) if isinstance(row[1], str) else dict(row[1] or {})
        if contact_id not in _candidate_contacts(conn, source, row[0], payload):
            did = _audit(conn, now, g.actor, g.grant_id, "signal_identity", f"{source}/{signal_id}",
                         "assign", contact_id, reason,
                         "refused: the contact is not a party the signal names")
            conn.execute("COMMIT")
            raise AuthorizationError(
                f"{contact_id!r} is not a party signal {signal_id!r} names (decision {did})")
        did = _audit(conn, now, g.actor, g.grant_id, "signal_identity", f"{source}/{signal_id}",
                     "assign", contact_id, reason, "applied")
        conn.execute(
            "UPDATE signal_resolution SET identity_status='resolved', contact_id=?,"
            " identity_reason=?, processed_at=?, updated_at=? WHERE signal_id=? AND source=?",
            (contact_id, f"operator {g.actor} (decision {did}): {reason}"[:500], now, now,
             signal_id, source))
        conn.execute("COMMIT")
    except AuthorizationError:
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return did


def _transfer_conflicted_identifier(
    conn: sqlite3.Connection,
    identifier_type: str,
    identifier_value: str,
    resolved_contact_id: str,
    resolved_by: str,
    now: str,
) -> int:
    """Assign a conflicted identifier to one of its claimants.

    The write half of identifier-conflict resolution, without authorization
    or a transaction of its own: resolve_identifier has verified the grant
    and holds the transaction (contacts.resolve_identifier_conflict, the
    OperatorToken path, stays DenyAll).  ``resolved_contact_id`` must
    be a claimant in an unresolved conflict for the identifier
    (IdentityResolutionError otherwise).  Losers' identifiers are revoked,
    never deleted, and every move is recorded in the ownership history.

    Returns the number of conflict rows resolved (0 when none was open).
    """
    from olo.adapters.contacts import IdentityResolutionError, _record_ownership_event
    # Find all unresolved conflicts for this identifier
    unresolved = conn.execute(
        "SELECT existing_contact_id, new_contact_id"
        " FROM identifier_conflicts"
        " WHERE identifier_type = ? AND identifier_value = ?"
        "   AND resolved_contact_id IS NULL",
        (identifier_type, identifier_value),
    ).fetchall()

    if not unresolved:
        return 0  # Idempotent: nothing to resolve

    # P3 check 2: resolved_contact_id must be a legitimate claimant
    claimants: set[str] = set()
    for row in unresolved:
        claimants.add(row[0])
        claimants.add(row[1])

    if resolved_contact_id not in claimants:
        raise IdentityResolutionError(
            f"resolve_identifier_conflict: resolved_contact_id="
            f"{resolved_contact_id!r} is not a claimant in any unresolved "
            f"dispute for ({identifier_type}, {identifier_value!r}). "
            f"Valid claimants: {sorted(claimants)}."
        )

    # Mark conflicts as resolved
    updated = conn.execute(
        "UPDATE identifier_conflicts"
        "  SET resolved_contact_id = ?,"
        "      resolved_at = ?,"
        "      resolved_by = ?"
        " WHERE identifier_type = ?"
        "   AND identifier_value = ?"
        "   AND resolved_contact_id IS NULL",
        (resolved_contact_id, now, resolved_by, identifier_type, identifier_value),
    ).rowcount

    if updated > 0:
        # Fix 2: revoke losers' identifiers instead of hard-deleting them.
        # Preserves original source and provenance; revocation_source records
        # who authorized the transfer.
        loser_rows = conn.execute(
            "SELECT contact_id, source FROM contact_identifiers"
            " WHERE identifier_type = ? AND identifier_value = ?"
            "   AND contact_id != ? AND revoked_at IS NULL",
            (identifier_type, identifier_value, resolved_contact_id),
        ).fetchall()
        for loser_cid, loser_source in loser_rows:
            conn.execute(
                "UPDATE contact_identifiers"
                "  SET revoked_at = ?, revocation_source = ?"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ? AND revoked_at IS NULL",
                (now, resolved_by, identifier_type, identifier_value, loser_cid),
            )
            # Record 'transferred_out' with original source preserved.
            _record_ownership_event(
                conn, identifier_type, identifier_value, loser_cid,
                "transferred_out", loser_source, resolved_by, now,
            )

        # Assign the identifier to the winner.
        # Fix 2: use the winner's ORIGINAL CRM source, not resolved_by,
        # so the ownership provenance reflects the authentic registration.
        winner_active = conn.execute(
            "SELECT source FROM contact_identifiers"
            " WHERE identifier_type = ? AND identifier_value = ?"
            "   AND contact_id = ? AND revoked_at IS NULL",
            (identifier_type, identifier_value, resolved_contact_id),
        ).fetchone()
        if winner_active:
            # Winner already has an active entry — just refresh timestamp.
            winner_source = winner_active[0]
            conn.execute(
                "UPDATE contact_identifiers SET verified_at = ?"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ? AND revoked_at IS NULL",
                (now, identifier_type, identifier_value, resolved_contact_id),
            )
        else:
            # Winner's entry was revoked or never existed.
            # Find their original source from contact_identifiers (revoked)
            # or from the conflict record.
            revoked_row = conn.execute(
                "SELECT source FROM contact_identifiers"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ? AND revoked_at IS NOT NULL"
                " ORDER BY revoked_at DESC LIMIT 1",
                (identifier_type, identifier_value, resolved_contact_id),
            ).fetchone()
            conflict_row = conn.execute(
                "SELECT source FROM identifier_conflicts"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND new_contact_id = ? LIMIT 1",
                (identifier_type, identifier_value, resolved_contact_id),
            ).fetchone()
            winner_source = (
                (revoked_row[0] if revoked_row else None)
                or (conflict_row[0] if conflict_row else None)
                or resolved_by  # last resort
            )
            conn.execute(
                "INSERT INTO contact_identifiers"
                " (identifier_type, identifier_value, contact_id,"
                "  source, verified_at,"
                "  revoked_at, revocation_source)"
                " VALUES (?, ?, ?, ?, ?, NULL, NULL)"
                " ON CONFLICT(identifier_type, identifier_value, source)"
                " DO UPDATE SET"
                "  contact_id = excluded.contact_id,"
                "  verified_at = excluded.verified_at,"
                "  revoked_at = NULL,"
                "  revocation_source = NULL",
                (identifier_type, identifier_value, resolved_contact_id,
                 winner_source, now),
            )

        # Record 'transferred_in' for the winner.
        # No idempotency check here: each resolution is a distinct event.
        # The old "already_in" guard incorrectly suppressed events on
        # repeated A→B→A→B transfers; replay safety is provided by the
        # "if not unresolved: return 0" guard earlier in this function.
        _record_ownership_event(
            conn, identifier_type, identifier_value, resolved_contact_id,
            "transferred_in", winner_source, resolved_by, now,
        )

    return updated


def resolve_identifier(conn: sqlite3.Connection, grant: OperatorGrant, identifier_type: str,
                       identifier_value: str, contact_id: str, *, reason: str, now: str) -> int:
    """Assign a conflicted identifier to one claimant; returns the decision id.

    Signals left ambiguous return to 'pending' so the next run re-resolves
    them with the settled identifier (still fail-closed).
    """
    from olo.adapters.contacts import IdentityResolutionError
    migrate(conn)
    if not reason.strip():
        raise ValueError("a reason is required")
    subject = f"{identifier_type}:{identifier_value}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        g = _verify(conn, grant, now)
        try:
            n = _transfer_conflicted_identifier(
                conn, identifier_type, identifier_value, contact_id, f"operator:{g.actor}", now)
        except IdentityResolutionError as exc:
            conn.execute("ROLLBACK")
            conn.execute("BEGIN IMMEDIATE")
            _audit(conn, now, g.actor, g.grant_id, "identifier", subject, "assign", contact_id,
                   reason, f"refused: {exc}")
            conn.execute("COMMIT")
            raise
        did = _audit(conn, now, g.actor, g.grant_id, "identifier", subject, "assign", contact_id,
                     reason, f"applied: {n} conflict(s) resolved")
        conn.execute(
            "UPDATE signal_resolution SET identity_status='pending', identity_reason=NULL,"
            " updated_at=? WHERE identity_status='ambiguous'", (now,))
        conn.execute("COMMIT")
    except IdentityResolutionError:
        raise
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return did
