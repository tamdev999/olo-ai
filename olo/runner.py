"""Olo AI — run_source(): the full ingest loop.

§5.8: runner algorithm, RunResult, main(argv).

Rules:
- No retry loop; partial/failed runs resume at next scheduled tick (Hermes cron).
- Empty stdout = Hermes no_agent silent success (F1).
- Non-zero exit = Hermes records a failed execution (F1/F9).
- Never print payloads or secrets.
- _fault is for test injection only; never wired to config or env.

No side effects at import time.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from olo.adapters import (
    AdapterConfigError,
    FetchBatch,
    MonitorAdapter,
    SourceError,
    get_adapter,
)
from olo.store import (
    DEFAULT_LEASE_TTL,
    AdapterContractError,
    CheckpointMoved,
    CursorStalled,
    LeaseLost,
    SourceBusy,
    acquire_run,
    connect,
    ensure_signal_resolution,
    finish_run,
    get_checkpoint,
    get_pending_identity_signals,
    get_resolution_summary,
    ingest_batch,
    migrate,
    record_identity_failed,
    record_identity_resolved,
    record_identity_unresolved,
    utc_now,
)


# ── RunResult ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunResult:
    """Frozen result of one run_source() invocation."""

    run_id: Optional[str]
    status: str          # succeeded | partial | failed | skipped_busy
    batches: int
    fetched: int
    inserted: int
    duplicates: int
    conflicts: int
    final_cursor: Optional[str]
    error: Optional[str]
    # S2: identity resolution counts (0 when identity stage was not run)
    identity_resolved:   int = 0
    identity_unresolved: int = 0
    identity_failed:     int = 0


# ── Default owner ─────────────────────────────────────────────────────────────

def _default_owner() -> str:
    from olo.store import _make_owner
    return _make_owner()


# ── Identity resolution stage (S2) ───────────────────────────────────────────

def _run_identity_stage(
    conn: sqlite3.Connection,
    signals: list,
    source: str,
    identifier_map: dict,
    clock: Callable[[], datetime],
) -> tuple[int, int, int]:
    """Attempt identity resolution for signals with identity_status='pending'.

    For each signal in *signals*:
    - Calls resolve_contact_from_signal (read-only Rolodex lookup; never creates contacts).
    - Records the outcome in signal_resolution.
    - Errors are caught per-signal, recorded, and never re-raised.

    Returns (resolved_count, unresolved_count, failed_count).

    NOTE (S2 customer-cancel flag):
    ``actor_role='customer', action='cancel'`` currently creates a confirmed
    cancellation (is_confirmed=1) in the ledger.  A customer cancellation request
    should instead remain a proposal pending business confirmation.  This is a
    known defect: the fix requires either removing 'cancel' from ACTOR_ACTION_ALLOWED
    for customers or introducing a 'request_cancel' action.  Deferred to S3/S4.
    """
    from olo.adapters.contacts import resolve_contact_from_signal
    from olo.signals import format_utc

    resolved = unresolved = failed = 0
    now_str = format_utc(clock())

    conn.execute("BEGIN IMMEDIATE")
    try:
        for sig in signals:
            # Only process signals still at 'pending' identity status
            row = conn.execute(
                "SELECT identity_status FROM signal_resolution"
                " WHERE signal_id=? AND source=?",
                (sig.external_id, source),
            ).fetchone()
            if row is None or row["identity_status"] != "pending":
                continue

            try:
                contact_id = resolve_contact_from_signal(
                    sig.kind, sig.payload, identifier_map,
                )
            except Exception as exc:
                record_identity_failed(conn, sig.external_id, source, str(exc), now_str)
                failed += 1
                continue

            if contact_id:
                record_identity_resolved(conn, sig.external_id, source, contact_id, now_str)
                resolved += 1
            else:
                record_identity_unresolved(
                    conn, sig.external_id, source,
                    reason=f"no CRM match for {sig.kind}",
                    now_str=now_str,
                )
                unresolved += 1

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return resolved, unresolved, failed


# ── run_source ────────────────────────────────────────────────────────────────

def run_source(
    adapter: MonitorAdapter,
    *,
    limit: int = 100,
    max_batches: int = 50,
    lease_ttl: int = DEFAULT_LEASE_TTL,
    clock: Callable[[], datetime] = utc_now,
    owner: Optional[str] = None,
    db_path: Optional[Path] = None,
    _fault: Optional[Callable[[str], None]] = None,
) -> RunResult:
    """Ingest signals from *adapter* into the Olo store.

    Runs in stages per batch:
      1. Ingest (ingest_batch) — append-only evidence layer.
      2. Ensure resolution rows (ensure_signal_resolution) — idempotent tracking.
      3. Identity stage (_run_identity_stage) — CRM lookup, no contact creation.
      4. Commitment stage — pending until S4 (extraction not yet implemented).

    Returns a RunResult; never raises (all exceptions are caught and recorded).
    """
    source = adapter.source
    effective_owner = owner or _default_owner()

    # 1. Migrate (idempotent)
    conn = connect(db_path)
    migrate(conn)

    # Load contact index once per run for the identity stage.
    # Best-effort: if the contacts table doesn't exist yet, identifier_map is empty.
    try:
        from olo.adapters.contacts import load_contact_index
        identifier_map, _contact_info = load_contact_index(conn)
    except Exception:
        identifier_map = {}

    # 2. Acquire lease
    try:
        run_id = acquire_run(source, effective_owner, conn, clock=clock, ttl=lease_ttl)
    except SourceBusy as exc:
        conn.close()
        return RunResult(
            run_id=None,
            status="skipped_busy",
            batches=0, fetched=0, inserted=0, duplicates=0, conflicts=0,
            final_cursor=None,
            error=str(exc),
        )

    batches = fetched = inserted = duplicates = conflicts = 0
    id_resolved = id_unresolved = id_failed = 0

    try:
        # 3. Main loop
        for _ in range(max_batches):
            # 3.1 Renew lease before each fetch
            from olo.store import renew_lease
            renew_lease(source, run_id, conn, clock=clock, ttl=lease_ttl)

            # 3.2 Get committed cursor
            cursor = get_checkpoint(source, conn)

            # 3.3 Fetch
            batch: FetchBatch = adapter.fetch(cursor, limit=limit)
            if _fault:
                _fault("after_fetch")

            # 3.4 Contract checks (before any write)
            if batch.has_more and batch.next_cursor == cursor:
                raise CursorStalled(
                    f"adapter returned has_more=True but cursor did not advance "
                    f"(cursor={cursor!r})"
                )
            if not batch.signals and batch.has_more:
                raise CursorStalled(
                    "adapter returned empty batch with has_more=True"
                )
            if len(batch.signals) > limit:
                raise AdapterContractError(
                    f"adapter returned {len(batch.signals)} signals, limit={limit}"
                )
            for sig in batch.signals:
                if sig.source != source:
                    raise AdapterContractError(
                        f"signal source {sig.source!r} != adapter source {source!r}"
                    )

            # 3.5 Atomic ingest commit
            b_ins, b_dup, b_con = ingest_batch(
                source, run_id, batch.signals, batch.next_cursor, cursor,
                conn, clock, _fault=_fault,
                pending_audit_events=getattr(batch, "pending_audit_events", ()),
            )
            batches += 1
            fetched += len(batch.signals)
            inserted += b_ins
            duplicates += b_dup
            conflicts += b_con

            # 3.5b Extract entity contacts from ingested signals (legacy enrichment).
            # Best-effort: failure does not abort ingestion.
            if batch.signals:
                try:
                    from olo.adapters.entity_extractor import upsert_entities
                    upsert_entities(batch.signals, source, conn, clock=clock)
                except Exception:
                    pass  # entity extraction is enrichment, not critical path

            # 3.5c S2: Signal resolution tracking — idempotent, never raises.
            # Stage 1 (ensure rows) and Stage 2 (identity) run per batch.
            # Stage 3 (commitment extraction) is deferred to S4.
            if batch.signals:
                try:
                    from olo.signals import format_utc
                    now_str = format_utc(clock())
                    conn.execute("BEGIN IMMEDIATE")
                    ensure_signal_resolution(conn, batch.signals, source, now_str)
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    # row creation failure is non-fatal; identity stage skipped

                try:
                    r, u, f = _run_identity_stage(
                        conn, batch.signals, source, identifier_map, clock,
                    )
                    id_resolved   += r
                    id_unresolved += u
                    id_failed     += f
                except Exception:
                    pass  # identity stage errors are recorded per-signal; batch continues

                if _fault:
                    _fault("after_resolution")

            # 3.6 Done?
            if not batch.has_more:
                final_cursor = get_checkpoint(source, conn)
                updated = finish_run(source, run_id, "succeeded", conn, clock=clock)
                conn.close()
                actual_status = "succeeded" if updated else "failed"
                actual_error = None if updated else "run was abandoned by a successor before finish_run"
                return RunResult(
                    run_id=run_id,
                    status=actual_status,
                    batches=batches,
                    fetched=fetched,
                    inserted=inserted,
                    duplicates=duplicates,
                    conflicts=conflicts,
                    final_cursor=final_cursor,
                    error=actual_error,
                    identity_resolved=id_resolved,
                    identity_unresolved=id_unresolved,
                    identity_failed=id_failed,
                )

        # 4. Loop exhausted with has_more still True → partial
        final_cursor = get_checkpoint(source, conn)
        updated = finish_run(source, run_id, "partial", conn, clock=clock)
        conn.close()
        actual_status = "partial" if updated else "failed"
        actual_error = None if updated else "run was abandoned by a successor before finish_run"
        return RunResult(
            run_id=run_id,
            status=actual_status,
            batches=batches,
            fetched=fetched,
            inserted=inserted,
            duplicates=duplicates,
            conflicts=conflicts,
            final_cursor=final_cursor,
            error=actual_error,
            identity_resolved=id_resolved,
            identity_unresolved=id_unresolved,
            identity_failed=id_failed,
        )

    except Exception as exc:
        # 5. Any exception after acquire_run → failed
        error_str = f"{type(exc).__name__}: {exc}"
        try:
            finish_run(source, run_id, "failed", conn, clock=clock, error=error_str)
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    """Entry point for cron shim: ``python -m olo.runner --source synthetic ...``

    Exit codes:
        0  succeeded | partial | skipped_busy
        1  failed / any exception
    """
    parser = argparse.ArgumentParser(prog="olo.runner")
    parser.add_argument("--source", required=True, help="Adapter name")
    parser.add_argument(
        "--option", metavar="key=value", action="append", default=[],
        help="Adapter option (may be repeated)",
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=50)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    # Parse options
    options: dict[str, str] = {}
    for item in args.option:
        if "=" not in item:
            print(f"olo: bad --option {item!r} (expected key=value)", file=sys.stderr)
            return 1
        k, _, v = item.partition("=")
        options[k] = v

    try:
        adapter = get_adapter(args.source, options)
    except AdapterConfigError as exc:
        print(f"olo: adapter config error: {exc}", file=sys.stderr)
        return 1

    try:
        result = run_source(adapter, limit=args.limit, max_batches=args.max_batches)
    except Exception as exc:
        # run_source re-raises after recording; print and exit 1
        run_id_str = "unknown"
        print(
            f"olo: run {run_id_str} failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.verbose:
        print(
            f"olo: {result.status} run={result.run_id} "
            f"batches={result.batches} inserted={result.inserted} "
            f"duplicates={result.duplicates} conflicts={result.conflicts} "
            f"cursor={result.final_cursor!r} "
            f"id_resolved={result.identity_resolved} "
            f"id_unresolved={result.identity_unresolved} "
            f"id_failed={result.identity_failed}",
            file=sys.stderr,
        )

    if result.status == "partial":
        print(
            f"olo: partial run {result.run_id}, "
            f"more data pending at cursor {result.final_cursor!r}",
            file=sys.stderr,
        )
    elif result.status == "skipped_busy":
        print(
            f"olo: skipped (source busy): {result.error}",
            file=sys.stderr,
        )
    elif result.status == "failed":
        print(
            f"olo: run {result.run_id} failed: {result.error}",
            file=sys.stderr,
        )
        return 1
    # succeeded → empty stdout and stderr (Hermes silent success)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
