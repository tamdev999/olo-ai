"""olo/requeue.py — give permanently failed work another chance, on purpose.

Two kinds of work stop retrying on their own (olo.health counts them as
needing an operator): an extraction that failed ``max_attempts`` times, and
a Hindsight document whose retains failed ``max_attempts_per_doc`` times.
After fixing the cause (a model outage, a server misconfiguration), an
operator re-queues them with ``hermes olo retry``.  Nothing is deleted: the
earlier failures stay in olo.db, and every re-queue is recorded
(``hindsight_requeues``, olo.store).
"""
from __future__ import annotations

import sqlite3

__all__ = ["requeue_extractions", "requeue_projection"]


def requeue_extractions(conn: sqlite3.Connection, now: str, *, max_attempts: int = 5) -> int:
    """Exhausted extractions back to pending, attempts reset; returns count."""
    conn.execute("BEGIN IMMEDIATE")
    n = conn.execute(
        "UPDATE signal_resolution SET commitment_status='pending', commitment_attempts=0,"
        " commitment_reason='re-queued by operator at ' || ? || ' after: ' ||"
        " COALESCE(commitment_reason, ''), updated_at=?"
        " WHERE commitment_status='failed' AND commitment_attempts >= ?",
        (now, now, max_attempts)).rowcount
    conn.execute("COMMIT")
    return n


def requeue_projection(conn: sqlite3.Connection, bank_id: str, now: str, *,
                       max_attempts: int = 3) -> int:
    """Documents out of retain attempts get a fresh attempt budget."""
    from olo.projection import exhausted_documents
    docs = exhausted_documents(conn, bank_id, max_attempts)
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("INSERT OR IGNORE INTO hindsight_requeues VALUES (?,?,?)",
                     [(d, bank_id, now) for d in docs])
    conn.execute("COMMIT")
    return len(docs)
