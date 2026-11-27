"""olo/health.py — persisted pipeline health and operator-visible backlog.

The audit found that the worker reported silent success while the
authoritative pipeline was degraded: budget stops, an unreachable or
unprepared Hindsight, unavailable usage statistics, extraction failures and
signals awaiting an operator never reached stdout or olo.db.

``record_run`` stores each stage's outcome per source (``pipeline_health``:
ingest, identity, commitment, projection, operator) on every run, so the
state survives restarts and ``hermes olo status`` can show it.  ``backlog``
counts what is waiting at each stage, straight from the tables that own it.
``assess`` turns both into the conditions an operator must see.

Degradation of a run is anything that stopped this run's processing short
(a failed fetch, a stage error, a budget or statistics stop, failed or lost
Hindsight work).  Items needing an operator — permanently failed
extractions, unresolved ledger matches, ambiguous identities, documents out
of retries — are counted separately; a run that adds new ones is reported as
not successful even if its own work completed.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

__all__ = ["Condition", "RunHealth", "assess", "backlog", "record_run"]

STAGES = ("ingest", "identity", "commitment", "projection", "operator")

#: Projection counters that mean the stage did not complete its work.
PROJECTION_DEGRADED = (
    "bank_unprepared", "stats_unavailable", "budget_exceeded", "unregistered_strategy",
    "failed", "lost", "cancelled", "cancelled_persisted", "consolidation_failed",
    "consolidation_lost", "consolidation_cancelled", "consolidation_retries_exhausted",
    "consolidation_capped", "consolidation_unknown", "consolidation_failed_memories",
    # Batch 4: the bank was lost and is being re-projected; a reconciliation failed.
    "bank_recreated", "reconcile_failed",
)
#: Commitment counters that mean extraction did not complete.
COMMITMENT_DEGRADED = ("budget_exhausted", "model_error", "content_error")
#: Ingest errors no retry will fix.
_OPERATOR_ERRORS = (
    ("auth revoked or expired", "oauth: reconnect the Google account"),
    ("scope missing", "oauth: reconnect with the required scopes"),
    ("CursorStalled", "sync stalled: inspect the source cursor"),
    ("sync appears blocked", "sync stalled: run `hermes olo checkpoint reset`"),
)


@dataclass(frozen=True)
class Condition:
    severity: str          # 'degraded' | 'attention' | 'stalled'
    stage: str
    message: str


@dataclass(frozen=True)
class RunHealth:
    degraded: tuple[str, ...] = ()
    attention_total: int = 0
    attention_new: int = 0


def _upsert(conn: sqlite3.Connection, source: str, stage: str, now: str, status: str,
            detail: dict, needs_operator: Optional[str]) -> None:
    prev = conn.execute(
        "SELECT consecutive_failures FROM pipeline_health WHERE source=? AND stage=?",
        (source, stage)).fetchone()
    failures = 0 if status in ("ok", "disabled") else (int(prev[0]) if prev else 0) + 1
    conn.execute(
        "INSERT INTO pipeline_health (source, stage, last_attempt_at, last_success_at, status,"
        " detail, consecutive_failures, needs_operator) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(source, stage) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,"
        "  last_success_at=COALESCE(excluded.last_success_at, pipeline_health.last_success_at),"
        "  status=excluded.status, detail=excluded.detail,"
        "  consecutive_failures=excluded.consecutive_failures,"
        "  needs_operator=excluded.needs_operator",
        (source, stage, now, now if status == "ok" else None, status,
         json.dumps(detail, sort_keys=True, default=str)[:4000], failures, needs_operator))


def _nonzero(counts: dict, keys) -> dict:
    return {k: counts[k] for k in keys if counts.get(k)}


def record_run(conn: sqlite3.Connection, source: str, now: str, *, run_status: str,
               error: Optional[str], fetched: int, inserted: int,
               identity: tuple[int, int, int], commitment_enabled: bool,
               commitment: dict, projection_enabled: bool, projection: dict,
               stage_errors: Optional[dict] = None, coverage: Optional[dict] = None,
               max_attempts: int = 5) -> RunHealth:
    """Persist this run's stage outcomes; return what made it not successful.

    ``coverage`` is the last batch's ``FetchBatch.coverage``: when the source
    still has streams to visit (a Calendar cycle cut short by its budget) the
    ingest stage is ``degraded`` with the coverage in its detail, not ``ok``.
    """
    stage_errors = stage_errors or {}
    degraded: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        ok = run_status in ("succeeded", "partial")
        incomplete = ok and bool(coverage) and not coverage.get("complete", True)
        operator = next((msg for marker, msg in _OPERATOR_ERRORS if error and marker in error), None)
        _upsert(conn, source, "ingest", now,
                "degraded" if incomplete else ("ok" if ok else "failed"),
                {"run_status": run_status, "fetched": fetched, "inserted": inserted,
                 "error": (error or "")[:300],
                 **({"coverage": coverage} if coverage else {})}, operator)
        if not ok:
            degraded.append(f"ingest {run_status}: {(error or '')[:160]}")
        elif incomplete:
            degraded.append(f"ingest incomplete: {coverage.get('pending')} of "
                            f"{coverage.get('calendars', '?')} streams still pending")

        resolved, unresolved, failed = identity
        _upsert(conn, source, "identity", now, "degraded" if failed else "ok",
                {"resolved": resolved, "unresolved": unresolved, "failed": failed}, None)
        if failed:
            degraded.append(f"identity: {failed} signals failed resolution")

        for stage, enabled, counts, keys in (
                ("commitment", commitment_enabled, commitment, COMMITMENT_DEGRADED),
                ("projection", projection_enabled, projection, PROJECTION_DEGRADED)):
            if not enabled:
                _upsert(conn, source, stage, now, "disabled", {}, None)
                continue
            bad = _nonzero(counts, keys)
            bad.update({k: v for k, v in counts.items() if k.endswith("_reason") and v})
            err = stage_errors.get(stage)
            status = "failed" if err else ("degraded" if bad else "ok")
            _upsert(conn, source, stage, now, status,
                    {"counts": counts, **({"error": err[:300]} if err else {})}, None)
            if err:
                degraded.append(f"{stage} failed: {err[:160]}")
            elif bad:
                degraded.append(f"{stage}: {bad}")

        attention_total, attention_new = 0, 0
        if commitment_enabled or projection_enabled:
            waiting = _operator_items(conn, max_attempts)
            attention_total = sum(waiting.values())
            prev = conn.execute(
                "SELECT detail FROM pipeline_health WHERE source=? AND stage='operator'",
                (source,)).fetchone()
            before = json.loads(prev[0]).get("total", 0) if prev else 0
            attention_new = max(0, attention_total - before)
            _upsert(conn, source, "operator", now, "degraded" if attention_total else "ok",
                    {"total": attention_total, **waiting},
                    "items awaiting an operator" if attention_total else None)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return RunHealth(tuple(degraded), attention_total, attention_new)


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0                      # table not created yet on this database


def _operator_items(conn: sqlite3.Connection, max_attempts: int) -> dict[str, int]:
    """Items no automatic retry will settle."""
    return {
        "identity_ambiguous": _scalar(
            conn, "SELECT COUNT(*) FROM signal_resolution WHERE identity_status='ambiguous'"),
        "extraction_exhausted": _scalar(
            conn, "SELECT COUNT(*) FROM signal_resolution WHERE commitment_status='failed'"
                  " AND commitment_attempts >= ?", (max_attempts,)),
        "ledger_unresolved": _scalar(
            conn, "SELECT COUNT(*) FROM signal_resolution WHERE commitment_status='unresolved'"),
        "identifier_conflicts": _scalar(
            conn, "SELECT COUNT(DISTINCT identifier_type || ':' || identifier_value)"
                  " FROM identifier_conflicts WHERE resolved_contact_id IS NULL"),
        "projection_retries_exhausted": _scalar(
            conn, "SELECT COUNT(*) FROM (SELECT d.document_id FROM hindsight_operation_docs d"
                  " JOIN hindsight_operations o ON o.operation_id=d.operation_id"
                  " JOIN hindsight_projection p ON p.signal_id=d.document_id AND p.bank_id=o.bank_id"
                  " WHERE o.status IN ('failed','lost','cancelled') AND p.status='failed'"
                  "   AND COALESCE(o.settled_at, o.submitted_at) > COALESCE((SELECT MAX(r.requeued_at)"
                  "       FROM hindsight_requeues r WHERE r.document_id=d.document_id"
                  "       AND r.bank_id=o.bank_id), '')"
                  " GROUP BY d.document_id HAVING COUNT(*) >= 3)"),
    }


def backlog(conn: sqlite3.Connection, *, max_attempts: int = 5) -> dict[str, Any]:
    """What is waiting at each stage, from the tables that own it."""
    def by(column: str) -> dict[str, int]:
        try:
            return {r[0]: int(r[1]) for r in conn.execute(
                f"SELECT {column}, COUNT(*) FROM signal_resolution GROUP BY {column}")}
        except sqlite3.OperationalError:
            return {}

    return {
        "identity": by("identity_status"),
        "commitment": by("commitment_status"),
        "commitment_retrying": _scalar(
            conn, "SELECT COUNT(*) FROM signal_resolution WHERE commitment_status='failed'"
                  " AND commitment_attempts < ?", (max_attempts,)),
        "projection_outbox": {r[0]: int(r[1]) for r in _rows(
            conn, "SELECT status, COUNT(*) FROM hindsight_projection GROUP BY status")},
        "operations_in_flight": _scalar(
            conn, "SELECT COUNT(*) FROM hindsight_operations WHERE status='submitted'"),
        "ledger_versions": _scalar(conn, "SELECT COUNT(*) FROM commitment_versions"),
        "ledger_unprojected": _scalar(
            conn, "SELECT COUNT(*) FROM commitment_versions v WHERE v.is_confirmed=1 AND NOT EXISTS"
                  " (SELECT 1 FROM hindsight_projection p WHERE p.status='confirmed'"
                  "  AND p.signal_id = 'ledger:' || v.commitment_id || ':v' || v.version)"),
        "operator": _operator_items(conn, max_attempts),
        "mailbox_states": {r[0]: int(r[1]) for r in _rows(
            conn, "SELECT state, COUNT(*) FROM gmail_message_state GROUP BY state")},
        # Open unresolved ledger events (olo.ledger): those waiting behind
        # another unresolved commitment are retried when it is decided.
        "unresolved_ledger": {
            "open": _scalar(conn, "SELECT COUNT(*) FROM unresolved_matches WHERE resolved_at IS NULL"),
            "waiting_behind_another": _scalar(
                conn, "SELECT COUNT(*) FROM unresolved_matches"
                      " WHERE resolved_at IS NULL AND blocked_by != '[]'"),
        },
        "operators": _scalar(
            conn, "SELECT COUNT(*) FROM operator_credentials WHERE revoked_at IS NULL"),
        "processing": processing(conn, max_attempts=max_attempts),
    }


def processing(conn: sqlite3.Connection, *, max_attempts: int = 5) -> dict[str, dict[str, int]]:
    """Every unfinished item, by what it waits for.

    ``incomplete`` settles on later runs by itself (pending, retrying, in
    flight); ``permanent`` never does until re-queued (``hermes olo retry``);
    ``awaiting_operator`` needs a decision (``hermes olo review`` / ``resolve``).
    """
    op = _operator_items(conn, max_attempts)
    return {
        "incomplete": {
            "identity_pending": _scalar(
                conn, "SELECT COUNT(*) FROM signal_resolution WHERE identity_status='pending'"),
            "commitment_pending": _scalar(
                conn, "SELECT COUNT(*) FROM signal_resolution WHERE commitment_status='pending'"),
            "commitment_retrying": _scalar(
                conn, "SELECT COUNT(*) FROM signal_resolution WHERE commitment_status='failed'"
                      " AND commitment_attempts < ?", (max_attempts,)),
            "projection_unconfirmed": _scalar(
                conn, "SELECT COUNT(*) FROM hindsight_projection WHERE status != 'confirmed'")
            - op["projection_retries_exhausted"],
            "operations_in_flight": _scalar(
                conn, "SELECT COUNT(*) FROM hindsight_operations WHERE status='submitted'"),
        },
        "permanent": {"extraction_exhausted": op["extraction_exhausted"],
                      "projection_retries_exhausted": op["projection_retries_exhausted"]},
        "awaiting_operator": {k: op[k] for k in ("ledger_unresolved", "identity_ambiguous",
                                                  "identifier_conflicts")},
    }


def _rows(conn: sqlite3.Connection, sql: str) -> list:
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []


def assess(conn: sqlite3.Connection, now: datetime, *,
           stale_after: timedelta = timedelta(hours=2)) -> list[Condition]:
    """Every condition an operator must see, worst first."""
    out: list[Condition] = []
    for r in _rows(conn, "SELECT source, stage, last_attempt_at, last_success_at, status,"
                         " detail, consecutive_failures, needs_operator FROM pipeline_health"
                         " ORDER BY source, stage"):
        source, stage, attempt, success, status, detail, fails, operator = tuple(r)
        if status in ("failed", "degraded") and stage != "operator":
            d = json.loads(detail or "{}")
            incomplete = not (d.get("coverage") or {}).get("complete", True)
            what = (d.get("error") or ({"incomplete coverage": d["coverage"]} if incomplete else None)
                    or _nonzero(d.get("counts") or {}, (*PROJECTION_DEGRADED, *COMMITMENT_DEGRADED))
                    or d)
            out.append(Condition("degraded", f"{source}/{stage}",
                                 f"{status} ({fails} consecutive): {str(what)[:200]}"))
        if operator:
            out.append(Condition("attention", f"{source}/{stage}", operator))
        if stage == "ingest":
            last_ok = datetime.fromisoformat(success) if success else None
            if last_ok is None or now - last_ok > stale_after:
                out.append(Condition(
                    "stalled", f"{source}/ingest",
                    f"no successful ingestion since {success or 'ever'} (last attempt {attempt})"))
    for name, n in backlog(conn)["operator"].items():
        if n:
            out.append(Condition("attention", "operator", f"{n} {name.replace('_', ' ')}"))
    from olo.hindsight_service import supervisor_conditions
    for severity, message in supervisor_conditions(conn, now):
        out.append(Condition(severity, "hindsight", message))
    order = {"degraded": 0, "stalled": 1, "attention": 2}
    return sorted(out, key=lambda c: order[c.severity])
