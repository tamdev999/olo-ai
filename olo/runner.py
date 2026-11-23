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
    finish_run,
    get_checkpoint,
    ingest_batch,
    migrate,
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


# ── Default owner ─────────────────────────────────────────────────────────────

def _default_owner() -> str:
    from olo.store import _make_owner
    return _make_owner()


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

    Returns a RunResult; never raises (all exceptions are caught and recorded).
    """
    source = adapter.source
    effective_owner = owner or _default_owner()

    # 1. Migrate (idempotent)
    conn = connect(db_path)
    migrate(conn)

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

            # 3.5 Atomic commit
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

            # 3.5b Extract entity contacts from ingested signals.
            # Best-effort: entity extraction failure does not abort ingestion.
            if batch.signals:
                try:
                    from olo.adapters.entity_extractor import upsert_entities
                    upsert_entities(batch.signals, source, conn, clock=clock)
                except Exception:
                    pass  # entity extraction is enrichment, not critical path

            # 3.6 Done?
            if not batch.has_more:
                final_cursor = get_checkpoint(source, conn)
                updated = finish_run(source, run_id, "succeeded", conn, clock=clock)
                conn.close()
                # If finish_run was a no-op the run was already abandoned by a
                # successor (lease expired mid-run). Report the actual outcome.
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
        )

    except Exception as exc:
        # 5. Any exception after acquire_run → failed
        error_str = f"{type(exc).__name__}: {exc}"
        try:
            finish_run(source, run_id, "failed", conn, clock=clock, error=error_str)
        except Exception:
            pass  # already abandoned by a successor; that's OK
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
            f"cursor={result.final_cursor!r}",
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
