"""olo/message_state.py — what the mailbox currently says about each message.

Policy (post-audit batch 2):

* Evidence is immutable.  A message's signals and its full original stay in
  olo.db whatever happens to it in Gmail; nothing here deletes or rewrites
  them.
* Changes are recorded, not inferred.  Deleting, trashing, spamming,
  archiving or restoring a message records its current mailbox state here
  (``gmail_message_state``), derived from the signals Gmail's history
  produced: ``email.deleted`` → ``deleted``; labels → ``trash`` / ``spam``
  / ``active``.
* Nothing is cancelled because mail was deleted.  The commitment ledger only
  changes on extracted, authorised events; a deleted confirmation email does
  not undo the confirmation.  Downstream consumers see the state instead:
  ``olo.current_state`` marks memories and ledger versions whose source
  message is no longer in the mailbox, and the commitment stage stops waiting
  for full content a deleted message can no longer provide.
* Ordering is by Gmail history id.  A state is applied only when its history
  id is at least the recorded one, so duplicated, replayed or reordered
  history records cannot roll a newer state back.  Signals without a history
  id (backfill) only fill in a message with no recorded state.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

__all__ = ["ACTIVE", "DELETED", "SPAM", "TRASH", "apply_signals", "message_states", "state_of"]

ACTIVE, TRASH, SPAM, DELETED = "active", "trash", "spam", "deleted"

_LABEL_KINDS = ("email.labels_added", "email.labels_removed")
_MESSAGE_KINDS = ("email.received", "email.sent", "email.content")


def _state_from_labels(labels: Iterable[str]) -> str:
    labels = set(labels)
    if "TRASH" in labels:
        return TRASH
    if "SPAM" in labels:
        return SPAM
    return ACTIVE


def _history_id(payload: dict) -> Optional[int]:
    try:
        return int(str(payload.get("history_id") or ""))
    except ValueError:
        return None


def _observation(kind: str, payload: dict) -> Optional[tuple[str, str, Optional[int], list]]:
    """(message id, state, history id, labels) one signal reports, or None."""
    msg = payload.get("message_id")
    if not msg:
        return None
    if kind == "email.deleted":
        return msg, DELETED, _history_id(payload), []
    if kind in _LABEL_KINDS:
        labels = list(payload.get("current_label_ids") or [])
        return msg, _state_from_labels(labels), _history_id(payload), labels
    if kind in _MESSAGE_KINDS and payload.get("label_ids") is not None:
        labels = list(payload.get("label_ids") or [])
        return msg, _state_from_labels(labels), _history_id(payload), labels
    return None


def apply_signals(conn: sqlite3.Connection, source: str, signals, now: str) -> int:
    """Record the mailbox state the *signals* report; returns rows changed.

    The caller commits.  ``signals`` are NormalizedSignal-like (kind, payload).
    """
    changed = 0
    for sig in signals:
        obs = _observation(sig.kind, sig.payload or {})
        if obs is None:
            continue
        msg, state, hid, labels = obs
        row = conn.execute(
            "SELECT history_id FROM gmail_message_state WHERE source=? AND message_id=?",
            (source, msg)).fetchone()
        if row is not None:
            recorded = row[0]
            if hid is None or (recorded is not None and hid < recorded):
                continue                  # older, replayed or unordered: never roll back
        conn.execute(
            "INSERT INTO gmail_message_state (source, message_id, state, label_ids, history_id,"
            " changed_at) VALUES (?,?,?,?,?,?) ON CONFLICT(source, message_id) DO UPDATE SET"
            "  state=excluded.state, label_ids=excluded.label_ids,"
            "  history_id=excluded.history_id, changed_at=excluded.changed_at",
            (source, msg, state, json.dumps(labels), hid, now))
        changed += 1
    return changed


def state_of(conn: sqlite3.Connection, source: str, message_id: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT state FROM gmail_message_state WHERE source=? AND message_id=?",
                           (source, message_id)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def message_states(conn: sqlite3.Connection) -> dict[str, str]:
    """message id -> state for every message whose state is not ``active``."""
    try:
        rows = conn.execute(
            "SELECT message_id, state FROM gmail_message_state WHERE state != 'active'").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]: r[1] for r in rows}
