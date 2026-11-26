"""olo/commitments.py — the worker's commitment stage (S5 part 2).

For every signal whose identity is verified:

    load_prepared_signal → extract_prepared → apply_event

and the outcome is persisted in ``signal_resolution`` in the same transaction
as the ledger write, so a crash can never leave a ledger version without its
status (and a replay is UNCHANGED by the ledger's idempotency).

Outcomes (``commitment_outcome``) and their ``commitment_status``:

  applied             resolved    ledger CREATED / UPDATED / UNCHANGED
  no_commitment       resolved    the model found nothing relevant
  no_input            skipped     the signal carries no text
  superseded          skipped     metadata copy of a Gmail message whose full
                                  content (email.content) is authoritative
  content_unavailable skipped     metadata copy of a message deleted before
                                  its full content was fetched
  (awaiting_content)  pending     metadata copy while full content is enabled
                                  but the content copy has not arrived yet
  no_change           resolved    calendar revision leaves the meeting date as is
  stale_revision      skipped     calendar revision older than the one applied
  recurring_series    skipped     calendar series master (no single date)
  rejected            unresolved  evidence failed verification (never written)
  ledger_unresolved   unresolved  no unique, evidenced commitment to update
  ledger_invalid      unresolved  event failed ledger validation
  model_error         failed      retried on later runs, up to max_attempts
  content_error       failed      stored content missing/corrupt; retried
  budget_exhausted    pending     no call made; the run stops extracting

Only signals with ``identity_status='resolved'`` are processed: the ledger
key is the canonical Rolodex contact, never a guess.  Ambiguous or unresolved
identities keep the commitment pending until the identity is resolved.

Calendar events (calendar.event.changed / .cancelled) take the deterministic
path in olo.calendar_commitments — no model call, no budget — keyed by the
event itself.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from olo.calendar_commitments import CALENDAR_EVENT_KINDS, process_calendar_signal
from olo.extractor import (
    Budget,
    ExtractionStatus,
    extract_prepared,
    migrate_extraction_cache,
)
from olo.ledger import LedgerEvent, OutcomeKind, apply_event
from olo.ledger import migrate as migrate_ledger
from olo.signal_content import EMAIL_KINDS, PROSE_KINDS, load_prepared_signal
from olo.signals import format_utc

__all__ = [
    "CommitmentStageConfig",
    "message_doc_id",
    "run_commitment_stage",
]

OUTCOME_APPLIED           = "applied"
OUTCOME_NO_COMMITMENT     = "no_commitment"
OUTCOME_NO_INPUT          = "no_input"
OUTCOME_SUPERSEDED        = "superseded"
OUTCOME_AWAITING_CONTENT  = "awaiting_content"   # counted only; row stays pending
OUTCOME_CONTENT_UNAVAILABLE = "content_unavailable"  # deleted before content arrived
OUTCOME_REJECTED          = "rejected"
OUTCOME_LEDGER_UNRESOLVED = "ledger_unresolved"
OUTCOME_LEDGER_INVALID    = "ledger_invalid"
OUTCOME_MODEL_ERROR       = "model_error"
OUTCOME_CONTENT_ERROR     = "content_error"

_METADATA_EMAIL_KINDS: frozenset[str] = frozenset({"email.sent", "email.received"})

#: ExtractionStatus → (commitment_status, outcome) for non-extracted results.
_STATUS_MAP: dict[ExtractionStatus, tuple[str, str]] = {
    ExtractionStatus.NO_COMMITMENT: ("resolved", OUTCOME_NO_COMMITMENT),
    ExtractionStatus.NO_INPUT:      ("skipped", OUTCOME_NO_INPUT),
    ExtractionStatus.REJECTED:      ("unresolved", OUTCOME_REJECTED),
    ExtractionStatus.MODEL_ERROR:   ("failed", OUTCOME_MODEL_ERROR),
    ExtractionStatus.CONTENT_ERROR: ("failed", OUTCOME_CONTENT_ERROR),
}

_LEDGER_MAP: dict[OutcomeKind, tuple[str, str]] = {
    OutcomeKind.CREATED:    ("resolved", OUTCOME_APPLIED),
    OutcomeKind.UPDATED:    ("resolved", OUTCOME_APPLIED),
    OutcomeKind.UNCHANGED:  ("resolved", OUTCOME_APPLIED),
    OutcomeKind.UNRESOLVED: ("unresolved", OUTCOME_LEDGER_UNRESOLVED),
    OutcomeKind.INVALID:    ("unresolved", OUTCOME_LEDGER_INVALID),
}


@dataclass(frozen=True)
class CommitmentStageConfig:
    """Enables the commitment stage in run_source.

    ``model_fn`` is the extraction backend (e.g. make_deepseek_model_fn()).
    ``budget`` caps the run's model calls/tokens; ``max_attempts`` caps
    retries of failed extractions per signal.
    """
    model_fn: Callable[[str], tuple]
    model_id: Optional[str] = None
    budget: Optional[Budget] = None
    max_attempts: int = 5


def message_doc_id(kind: str, payload: dict, source: str, signal_id: str) -> str:
    """Ledger source_doc_id: one id per real-world message.

    Both representations of a Gmail message (metadata and email.content)
    map to ``gmail:<message_id>``, so even a race between them cannot add a
    second ledger version for the same message (the ledger is idempotent per
    source document).
    """
    mid = payload.get("message_id")
    if kind in EMAIL_KINDS and isinstance(mid, str) and mid.strip():
        return f"gmail:{mid.strip()}"
    return f"{source}:{signal_id}"


def _full_content_enabled(conn: sqlite3.Connection) -> bool:
    """True once the business ingests full Gmail content (email.content)."""
    return conn.execute(
        "SELECT 1 FROM signals WHERE kind='email.content' LIMIT 1"
    ).fetchone() is not None


def _has_content_copy(conn: sqlite3.Connection, message_id: object) -> bool:
    """True when this business stored the email.content form of the message."""
    if not isinstance(message_id, str) or not message_id.strip():
        return False
    return conn.execute(
        "SELECT 1 FROM signals WHERE kind='email.content'"
        " AND json_extract(payload, '$.message_id') = ? LIMIT 1",
        (message_id.strip(),),
    ).fetchone() is not None


def reopen_calendar_signals(conn: sqlite3.Connection, source: str, now: str) -> int:
    """Queue calendar events ingested before they had a commitment path.

    Until post-audit batch 3 calendar signals were tracked as 'skipped' and
    never processed; those rows (no outcome recorded) become pending once.
    """
    marks = ",".join("?" * len(CALENDAR_EVENT_KINDS))
    conn.execute("BEGIN IMMEDIATE")
    n = conn.execute(
        "UPDATE signal_resolution SET commitment_status='pending', updated_at=?"
        " WHERE source=? AND commitment_status='skipped' AND commitment_outcome IS NULL"
        f"   AND signal_id IN (SELECT external_id FROM signals WHERE source=? AND kind IN ({marks}))",
        (now, source, source, *sorted(CALENDAR_EVENT_KINDS))).rowcount
    conn.execute("COMMIT")
    return n


def _candidates(conn: sqlite3.Connection, source: str, max_attempts: int) -> list[sqlite3.Row]:
    kinds = sorted(PROSE_KINDS | CALENDAR_EVENT_KINDS)
    marks = ",".join("?" * len(kinds))
    return conn.execute(
        "SELECT sr.signal_id, sr.contact_id, sr.commitment_attempts,"
        "       s.kind, s.payload"
        " FROM signal_resolution sr"
        " JOIN signals s ON s.external_id = sr.signal_id AND s.source = sr.source"
        " WHERE sr.source = ? AND sr.identity_status = 'resolved'"
        f"   AND s.kind IN ({marks})"
        "   AND (sr.commitment_status = 'pending'"
        "        OR (sr.commitment_status = 'failed' AND sr.commitment_attempts < ?))"
        " ORDER BY s.occurred_at, s.id",
        (source, *kinds, max_attempts),
    ).fetchall()


def _record(
    conn: sqlite3.Connection,
    source: str,
    signal_id: str,
    status: str,
    outcome: str,
    reason: str,
    now: str,
    *,
    commitment_id: Optional[str] = None,
    version: Optional[int] = None,
    attempted: bool = False,
) -> None:
    conn.execute(
        "UPDATE signal_resolution"
        " SET commitment_status=?, commitment_outcome=?, commitment_reason=?,"
        "     commitment_id=?, commitment_version=?,"
        "     commitment_attempts = commitment_attempts + ?,"
        "     processed_at=?, updated_at=?"
        " WHERE signal_id=? AND source=?",
        (status, outcome, (reason or "")[:500], commitment_id, version,
         1 if attempted else 0, now, now, signal_id, source),
    )


def run_commitment_stage(
    conn: sqlite3.Connection,
    source: str,
    cfg: CommitmentStageConfig,
    clock: Callable[[], datetime],
    *,
    renew: Optional[Callable[[], None]] = None,
    _fault: Optional[Callable[[str], None]] = None,
) -> dict[str, int]:
    """Process every eligible signal of *source*; return counts by outcome.

    Idempotent and resumable: a signal is only re-processed while pending or
    failed (below max_attempts); the extraction cache in olo.db makes a
    re-run after a crash free; the ledger write and its status commit
    together.  ``renew`` keeps the source lease alive between signals.
    """
    migrate_ledger(conn)
    migrate_extraction_cache(conn)
    content_enabled = _full_content_enabled(conn)
    counts: dict[str, int] = {}
    reopen_calendar_signals(conn, source, format_utc(clock()))

    for row in _candidates(conn, source, cfg.max_attempts):
        if renew is not None:
            renew()
        sid, kind = row["signal_id"], row["kind"]
        raw = row["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        now = format_utc(clock())

        if kind in CALENDAR_EVENT_KINDS:
            label = process_calendar_signal(conn, source, sid, row["contact_id"], kind,
                                            payload, now, _record)
            counts[label] = counts.get(label, 0) + 1
            continue

        if kind in _METADATA_EMAIL_KINDS and content_enabled:
            # Full content is the authoritative representation of a Gmail
            # message.  Its metadata copy is superseded once the content copy
            # exists; until then it waits (never extracted twice, never lost).
            if not _has_content_copy(conn, payload.get("message_id")):
                from olo.message_state import DELETED, state_of
                if state_of(conn, source, payload.get("message_id") or "") == DELETED:
                    # Deleted before its full content was fetched: it can no
                    # longer arrive, so stop waiting (the metadata stays).
                    conn.execute("BEGIN IMMEDIATE")
                    _record(conn, source, sid, "skipped", OUTCOME_CONTENT_UNAVAILABLE,
                            "message deleted from the mailbox before its full content "
                            "was fetched", now)
                    conn.execute("COMMIT")
                    counts[OUTCOME_CONTENT_UNAVAILABLE] = counts.get(OUTCOME_CONTENT_UNAVAILABLE, 0) + 1
                    continue
                counts[OUTCOME_AWAITING_CONTENT] = counts.get(OUTCOME_AWAITING_CONTENT, 0) + 1
                continue
            conn.execute("BEGIN IMMEDIATE")
            _record(conn, source, sid, "skipped", OUTCOME_SUPERSEDED,
                    "full content (email.content) is the authoritative representation", now)
            conn.execute("COMMIT")
            counts[OUTCOME_SUPERSEDED] = counts.get(OUTCOME_SUPERSEDED, 0) + 1
            continue

        prepared = load_prepared_signal(conn, source, sid)
        outcome = extract_prepared(
            prepared, source, budget=cfg.budget, cache_conn=conn,
            model_fn=cfg.model_fn, model_id=cfg.model_id,
        )
        if outcome.status == ExtractionStatus.BUDGET_EXHAUSTED:
            counts["budget_exhausted"] = counts.get("budget_exhausted", 0) + 1
            break                      # left pending; no attempt is charged
        if _fault:
            _fault("after_extraction")

        conn.execute("BEGIN IMMEDIATE")
        try:
            if outcome.status == ExtractionStatus.EXTRACTED and outcome.result is not None:
                res = outcome.result
                event = LedgerEvent(
                    event_type=res.event_type,
                    contact_id=row["contact_id"],
                    business_key=res.business_key or "",
                    source_doc_id=message_doc_id(kind, payload, source, sid),
                    source_kind=kind,
                    occurred_at=prepared.occurred_at or "",
                    actor_role=res.actor_role,
                    action=res.action,
                    proposed_date=res.proposed_date,
                    confirmed_date=res.confirmed_date,
                    prior_confirmed_date=res.prior_confirmed_date,
                    thread_id=res.thread_id or "",
                    notes=f"sig:{sid}",
                )
                applied = apply_event(conn, event, recorded_at=now)
                status, label = _LEDGER_MAP[applied.outcome]
                _record(conn, source, sid, status, label,
                        f"{applied.outcome.value}: {applied.message}", now,
                        commitment_id=applied.commitment_id, version=applied.version,
                        attempted=True)
            else:
                status, label = _STATUS_MAP[outcome.status]
                _record(conn, source, sid, status, label, outcome.reason, now,
                        attempted=True)
            if _fault:
                _fault("before_commitment_commit")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        counts[label] = counts.get(label, 0) + 1

    return counts
