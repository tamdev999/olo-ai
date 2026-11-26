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
import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

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
    backfill_signal_resolution,
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

if TYPE_CHECKING:
    from olo.commitments import CommitmentStageConfig
    from olo.projection import ProjectionConfig

__all__ = [
    "RunResult",
    "run_source",
    "retry_identity_resolution",
]


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
    # S5: recovery at run start
    resolution_backfilled: int = 0   # tracking rows recreated after an interrupted ingest
    identity_resumed:      int = 0   # pending signals from earlier runs reprocessed
    # S5 part 2: downstream stages (empty when not enabled)
    commitment: dict = field(default_factory=dict)   # counts by commitment outcome
    projection: dict = field(default_factory=dict)   # Hindsight outbox counts
    # Post-audit batch 1: persisted stage health of this run (olo.health)
    degraded: tuple = ()             # what stopped this run's processing short
    attention_total: int = 0         # items awaiting an operator (S5 stages on)
    attention_new: int = 0           # of which added by this run


@dataclass(frozen=True)
class _StoredSignal:
    """A stored signal, shaped for _run_identity_stage (duck-typed)."""
    external_id: str
    kind: str
    payload: dict


def _load_stored_signals(
    conn: sqlite3.Connection, source: str, signal_ids: list[str],
) -> list[_StoredSignal]:
    """Load *signal_ids* of *source* from the signals table, in id order."""
    import json
    out: list[_StoredSignal] = []
    for sid in sorted(signal_ids):
        row = conn.execute(
            "SELECT kind, payload FROM signals WHERE source=? AND external_id=?",
            (source, sid),
        ).fetchone()
        if row is None:
            continue
        raw = row["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        out.append(_StoredSignal(sid, row["kind"], payload))
    return out


def _recover_pending(
    conn: sqlite3.Connection,
    source: str,
    identifier_map: dict,
    clock: Callable[[], datetime],
    own_identifiers: frozenset,
    _fault: Optional[Callable[[str], None]],
) -> tuple[int, tuple[int, int, int]]:
    """Backfill missing tracking rows, then resume every pending signal.

    Runs under the source lease before new fetching, so work interrupted by a
    crash (between ingest and tracking, or mid identity stage) is picked up by
    the next run instead of being silently skipped.  Idempotent.
    Returns (rows_backfilled, (resolved, unresolved, failed)).
    """
    from olo.signals import format_utc

    from olo.message_state import apply_signals as _apply_mailbox_state

    conn.execute("BEGIN IMMEDIATE")
    try:
        # The signals a crash left untracked also never had their mailbox
        # state recorded: apply it in the same transaction (idempotent).
        untracked = conn.execute(
            "SELECT s.kind, s.payload FROM signals s LEFT JOIN signal_resolution sr"
            " ON sr.signal_id = s.external_id AND sr.source = s.source"
            " WHERE s.source = ? AND sr.signal_id IS NULL ORDER BY s.id", (source,)).fetchall()
        _apply_mailbox_state(conn, source, [
            _StoredSignal(external_id="", kind=k, payload=json.loads(pl) if isinstance(pl, str) else pl)
            for k, pl in untracked], format_utc(clock()))
        backfilled = backfill_signal_resolution(conn, source, format_utc(clock()))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    if _fault:
        _fault("after_backfill")

    pending_ids = [r["signal_id"] for r in get_pending_identity_signals(conn, source)]
    if not pending_ids:
        return backfilled, (0, 0, 0)
    signals = _load_stored_signals(conn, source, pending_ids)
    return backfilled, _run_identity_stage(
        conn, signals, source, identifier_map, clock, own_identifiers,
    )


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
    own_identifiers: frozenset = frozenset(),
) -> tuple[int, int, int]:
    """Attempt identity resolution for signals with identity_status='pending'.

    S1–S4 hardening: identity comes from resolve_signal_identity — the
    business's own identifiers (``own_identifiers`` plus the From address of
    sent mail) are excluded, and a signal with more than one distinct
    plausible customer is recorded 'ambiguous' instead of taking the first.

    For each signal in *signals*:
    - Calls resolve_contact_from_signal (read-only Rolodex lookup; never creates contacts).
    - Records the outcome in signal_resolution.
    - Errors are caught per-signal, recorded, and never re-raised.

    Returns (resolved_count, unresolved_count, failed_count).

    S3: Creates contacts for genuine counterparties when no existing match is found.
    Automated senders (noreply@, bounce@, etc.) are skipped — no contact created.
    Identity conflicts raise IdentityResolutionError and are recorded as 'ambiguous'.
    Manual conflict resolution remains DenyAll (unchanged from S1/S2).

    NOTE: customer-cancel bug (now fixed in S3 — see ledger.py).
    """
    from olo.adapters.contacts import (
        IDENTITY_AMBIGUOUS,
        IDENTITY_NEW,
        IDENTITY_RESOLVED,
        IdentityResolutionError,
        create_verified_contact,
        resolve_signal_identity,
    )
    from olo.signals import format_utc
    resolved = unresolved = failed = 0
    now_str = format_utc(clock())

    # Process per-signal (no outer transaction) so create_verified_contact can
    # open its own BEGIN IMMEDIATE without nesting conflicts.
    for sig in signals:
        # Read status outside any transaction
        row = conn.execute(
            "SELECT identity_status FROM signal_resolution"
            " WHERE signal_id=? AND source=?",
            (sig.external_id, source),
        ).fetchone()
        if row is None or row["identity_status"] != "pending":
            continue

        contact_id = ""
        ambiguous = False
        error_reason: Optional[str] = None

        # Step 1: read-only, fail-closed Rolodex lookup
        identity = None
        try:
            identity = resolve_signal_identity(
                sig.kind, sig.payload, identifier_map, own_identifiers=own_identifiers,
            )
        except Exception as exc:
            error_reason = str(exc)

        if identity is not None and identity.status == IDENTITY_RESOLVED:
            contact_id = identity.contact_id
        elif identity is not None and identity.status == IDENTITY_AMBIGUOUS:
            # Several distinct plausible customers: never pick one.
            ambiguous = True
            error_reason = identity.reason

        # Step 2 (S3): create contact for the single genuine counterparty
        if identity is not None and identity.status == IDENTITY_NEW and error_reason is None:
            counterparty = identity.counterparty
            if counterparty is not None:
                id_type, id_value, display_name = counterparty
                try:
                    # create_verified_contact opens its own transaction
                    contact_id = create_verified_contact(
                        display_name,
                        "",                       # org_name: populated by CRM sync later
                        [(id_type, id_value)],
                        conn,
                        source,
                        clock,
                    )
                    if contact_id:
                        # Keep identifier_map current for subsequent signals
                        identifier_map[(id_type, id_value)] = contact_id
                except IdentityResolutionError as exc:
                    # Two identifiers map to different contacts — record as ambiguous.
                    # Manual resolution remains DenyAll; do not auto-merge.
                    ambiguous = True
                    error_reason = str(exc)
                except Exception as exc:
                    error_reason = str(exc)

        # Step 3: record outcome in its own transaction
        try:
            conn.execute("BEGIN IMMEDIATE")
            if error_reason and not ambiguous:
                record_identity_failed(conn, sig.external_id, source, error_reason, now_str)
                failed += 1
            elif ambiguous:
                conn.execute(
                    "UPDATE signal_resolution"
                    " SET identity_status='ambiguous', identity_reason=?,"
                    "     processed_at=?, updated_at=?"
                    " WHERE signal_id=? AND source=? AND identity_status='pending'",
                    (
                        (error_reason or "")[:500], now_str, now_str,
                        sig.external_id, source,
                    ),
                )
                failed += 1
            elif contact_id:
                record_identity_resolved(conn, sig.external_id, source, contact_id, now_str)
                resolved += 1
            else:
                record_identity_unresolved(
                    conn, sig.external_id, source,
                    reason=f"no CRM match or counterparty for {sig.kind}",
                    now_str=now_str,
                )
                unresolved += 1
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            # Not recorded: the row stays 'pending' (never marked complete) and
            # the next run's recovery resumes it.  Counted as a failure here.
            failed += 1

    return resolved, unresolved, failed


# ── Recoverable identity (S3) ─────────────────────────────────────────────────

def retry_identity_resolution(
    conn: sqlite3.Connection,
    source: str,
    *,
    statuses: tuple[str, ...] = ("pending", "unresolved", "failed"),
    identifier_map: Optional[dict] = None,
    clock: Callable[[], datetime] = utc_now,
    own_identifiers: Optional[frozenset] = None,
) -> dict:
    """Reprocess signals in retryable identity states for *source*.

    Safe to call after new contacts have been added to the Rolodex (e.g. after
    a CRM sync) so previously-unresolved signals can be re-linked.

    Idempotent: calling twice with no Rolodex changes gives the same outcome.
    Never bypasses identity conflicts or authorization checks.
    Already-resolved signals (identity_status='resolved') are never reset.

    Args:
        conn:            Open SQLite connection with ledger tables present.
        source:          Signal source name (matches signals.source).
        statuses:        Identity statuses eligible for retry.
        identifier_map:  Pre-loaded contact index; reloaded from DB when None.
        clock:           Clock callable for timestamps.

    Returns:
        ``{"retried": N, "resolved": N, "unresolved": N, "failed": N}``
    """
    import json
    from olo.signals import format_utc

    # Load fresh contact index (picks up any contacts added since last run)
    if identifier_map is None:
        try:
            from olo.adapters.contacts import load_contact_index
            identifier_map, _ = load_contact_index(conn)
        except Exception:
            identifier_map = {}
    if own_identifiers is None:
        try:
            from olo.adapters.contacts import load_own_identifiers
            own_identifiers = load_own_identifiers(conn)
        except Exception:
            own_identifiers = frozenset()

    # Find signals in retryable states
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT sr.signal_id, s.kind, s.payload"
        f" FROM signal_resolution sr"
        f" JOIN signals s"
        f"   ON s.external_id = sr.signal_id AND s.source = sr.source"
        f" WHERE sr.source = ? AND sr.identity_status IN ({placeholders})"
        f" ORDER BY sr.signal_id",
        (source, *statuses),
    ).fetchall()

    if not rows:
        return {"retried": 0, "resolved": 0, "unresolved": 0, "failed": 0}

    # Reset eligible rows to 'pending' so the identity stage processes them
    now_str = format_utc(clock())
    conn.execute("BEGIN IMMEDIATE")
    for row in rows:
        conn.execute(
            "UPDATE signal_resolution"
            " SET identity_status='pending', identity_reason=NULL, updated_at=?"
            " WHERE signal_id=? AND source=? AND identity_status != 'resolved'",
            (now_str, row["signal_id"], source),
        )
    conn.execute("COMMIT")

    proxies = [
        _StoredSignal(
            r["signal_id"], r["kind"],
            json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {}),
        )
        for r in rows
    ]

    r, u, f = _run_identity_stage(
        conn, proxies, source, identifier_map, clock, own_identifiers,
    )
    return {"retried": len(rows), "resolved": r, "unresolved": u, "failed": f}


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
    commitment: Optional[CommitmentStageConfig] = None,
    projection: Optional[ProjectionConfig] = None,
    own_identifiers: frozenset = frozenset(),
) -> RunResult:
    """Ingest signals from *adapter* into the Olo store.

    Before fetching, recovery (S5) backfills tracking rows lost to an
    interrupted ingest and resumes every signal still pending identity.

    Then runs in stages per batch:
      1. Ingest (ingest_batch) — append-only evidence layer.
      2. Ensure resolution rows (ensure_signal_resolution) — idempotent tracking.
      3. Identity stage (_run_identity_stage) — fail-closed Rolodex lookup and
         contact creation for a single genuine counterparty.

    After ingestion (S5 part 2), both opt-in and under the same lease:
      5. Commitment stage (``commitment``) — prepare → extract → ledger, with
         the outcome persisted in signal_resolution (olo.commitments).
      6. Projection stage (``projection``) — verified signals and ledger
         state to Hindsight through the hindsight_projection outbox
         (olo.projection).
    With neither config, behaviour is unchanged and nothing is written to
    the ledger or Hindsight.

    Returns a RunResult; never raises (all exceptions are caught and recorded).
    """
    source = adapter.source
    effective_owner = owner or _default_owner()

    # 1. Migrate (idempotent)
    conn = connect(db_path)
    migrate(conn)
    # S3: also create the contact identity tables (idempotent, same session).
    # run_source is the entry point for real signal processing; contacts tables
    # must exist before the identity stage creates counterparty contacts.
    try:
        from olo.adapters.contacts import migrate_contacts as _mc
        _mc(conn)
    except Exception:
        pass  # contacts module unavailable; identity stage handles gracefully

    # Load contact index once per run for the identity stage.
    # Best-effort: if the contacts table doesn't exist yet, identifier_map is empty.
    try:
        from olo.adapters.contacts import load_contact_index
        identifier_map, _contact_info = load_contact_index(conn)
    except Exception:
        identifier_map = {}
    # The business's own addresses (learned from stored sent mail) are never
    # counterparties; extended per batch from newly ingested sent mail.
    try:
        from olo.adapters.contacts import load_own_identifiers, own_identifiers_of
        own_identifiers = own_identifiers | load_own_identifiers(conn)
    except Exception:
        own_identifiers_of = None  # type: ignore[assignment]

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
    backfilled = resumed = 0

    ds: dict = {"stage": None, "commitment": {}, "projection": {}, "errors": {}}

    def _downstream() -> tuple[dict, dict]:
        from olo.store import renew_lease
        if commitment is not None:
            from olo.commitments import run_commitment_stage
            ds["stage"] = "commitment"
            ds["commitment"] = run_commitment_stage(
                conn, source, commitment, clock,
                renew=lambda: renew_lease(source, run_id, conn, clock=clock, ttl=lease_ttl),
                _fault=_fault,
            )
        if projection is not None:
            from olo.projection import run_projection_stage
            ds["stage"] = "projection"
            renew_lease(source, run_id, conn, clock=clock, ttl=lease_ttl)
            ds["projection"] = run_projection_stage(conn, source, projection, clock, _fault=_fault)
        ds["stage"] = None
        return ds["commitment"], ds["projection"]

    def _health(run_status: str, error: Optional[str]):
        """Persist stage health (olo.health); never changes the run's outcome."""
        from olo.health import RunHealth, record_run
        from olo.signals import format_utc as _format_utc
        try:
            return record_run(
                conn, source, _format_utc(clock()), run_status=run_status, error=error,
                fetched=fetched, inserted=inserted,
                identity=(id_resolved, id_unresolved, id_failed),
                commitment_enabled=commitment is not None, commitment=ds["commitment"],
                projection_enabled=projection is not None, projection=ds["projection"],
                stage_errors=ds["errors"],
                max_attempts=getattr(commitment, "max_attempts", 5))
        except Exception as exc:
            print(f"olo: could not record pipeline health: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return RunHealth(degraded=(f"health not recorded: {type(exc).__name__}",))

    try:
        # 2b. Recovery (S5): recreate tracking rows lost to an interrupted
        # ingest and resume every signal still pending from earlier runs.
        backfilled, (r, u, f) = _recover_pending(
            conn, source, identifier_map, clock, own_identifiers, _fault,
        )
        resumed = r + u + f
        id_resolved, id_unresolved, id_failed = r, u, f

        # 3. Main loop
        for _ in range(max_batches):
            # 3.1 Renew lease before each fetch
            from olo.store import renew_lease
            renew_lease(source, run_id, conn, clock=clock, ttl=lease_ttl)

            # 3.2 Get committed cursor
            cursor = get_checkpoint(source, conn)

            # 3.3 Fetch.  A failed fetch (outage, rate limit, revoked
            # credentials) must not block signals already ingested: the
            # downstream stages run before the run is recorded as failed.
            try:
                batch: FetchBatch = adapter.fetch(cursor, limit=limit)
            except Exception:
                try:
                    _downstream()
                except Exception as stage_exc:
                    ds["errors"][ds["stage"] or "downstream"] = f"{type(stage_exc).__name__}: {stage_exc}"
                raise
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
            if _fault:
                _fault("after_ingest")   # ingest committed; tracking rows not yet

            # 3.5b Extract entity contacts from ingested signals (legacy enrichment).
            # Best-effort: failure does not abort ingestion.
            if batch.signals:
                try:
                    from olo.adapters.entity_extractor import upsert_entities
                    upsert_entities(batch.signals, source, conn, clock=clock)
                except Exception:
                    pass  # entity extraction is enrichment, not critical path

            # 3.5c S2/S3: Signal resolution tracking — idempotent, never raises.
            # Stage 1 (ensure rows) and Stage 2 (identity+contact creation) run per batch.
            # Stage 3 (commitment extraction) is deferred to S4.
            if batch.signals:
                try:
                    from olo.signals import format_utc
                    now_str = format_utc(clock())
                    conn.execute("BEGIN IMMEDIATE")
                    ensure_signal_resolution(conn, batch.signals, source, now_str)
                    from olo.message_state import apply_signals as _apply_mailbox_state
                    _apply_mailbox_state(conn, source, batch.signals, now_str)
                    conn.execute("COMMIT")
                except Exception:
                    try:
                        conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    # Non-fatal here: the next run's recovery backfills the rows.

                if _fault:
                    _fault("before_identity")

                if own_identifiers_of is not None:
                    for s in batch.signals:
                        try:
                            own_identifiers = own_identifiers | own_identifiers_of(
                                s.kind, s.payload)
                        except Exception:
                            pass
                try:
                    r, u, f = _run_identity_stage(
                        conn, batch.signals, source, identifier_map, clock,
                        own_identifiers,
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
                c_counts, p_counts = _downstream()
                final_cursor = get_checkpoint(source, conn)
                updated = finish_run(source, run_id, "succeeded", conn, clock=clock)
                actual_status = "succeeded" if updated else "failed"
                actual_error = None if updated else "run was abandoned by a successor before finish_run"
                health = _health(actual_status, actual_error)
                conn.close()
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
                    resolution_backfilled=backfilled,
                    identity_resumed=resumed,
                    commitment=c_counts,
                    projection=p_counts,
                    degraded=health.degraded,
                    attention_total=health.attention_total,
                    attention_new=health.attention_new,
                )

        # 4. Loop exhausted with has_more still True → partial
        c_counts, p_counts = _downstream()
        final_cursor = get_checkpoint(source, conn)
        updated = finish_run(source, run_id, "partial", conn, clock=clock)
        actual_status = "partial" if updated else "failed"
        actual_error = None if updated else "run was abandoned by a successor before finish_run"
        health = _health(actual_status, actual_error)
        conn.close()
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
            resolution_backfilled=backfilled,
            identity_resumed=resumed,
            commitment=c_counts,
            projection=p_counts,
            degraded=health.degraded,
            attention_total=health.attention_total,
            attention_new=health.attention_new,
        )

    except Exception as exc:
        # 5. Any exception after acquire_run → failed
        error_str = f"{type(exc).__name__}: {exc}"
        if ds["stage"] is not None:
            ds["errors"][ds["stage"]] = error_str
        try:
            finish_run(source, run_id, "failed", conn, clock=clock, error=error_str)
        except Exception:
            pass
        _health("failed", error_str)
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
    # S5 stages: disabled unless explicitly enabled for one named business
    # (see olo.activation for every refusal condition).
    s5 = parser.add_argument_group("S5 stages (off by default)")
    s5.add_argument("--enable-commitments", action="store_true")
    s5.add_argument("--enable-projection", action="store_true")
    s5.add_argument("--business-id")
    s5.add_argument("--bank-id")
    s5.add_argument("--deepseek-key-file")
    s5.add_argument("--extract-max-calls", type=int)
    s5.add_argument("--extract-max-tokens", type=int)
    s5.add_argument("--projection-max-docs", type=int)
    s5.add_argument("--projection-max-prose-docs", type=int)
    s5.add_argument("--projection-budget-usd", type=float)
    s5.add_argument("--own-email", action="append", default=[])
    s5.add_argument("--hindsight-registry")
    s5.add_argument("--deepseek-base-url",
                    help="DeepSeek API base URL (https://api.deepseek.com/v1, or a loopback test server)")
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

    from olo.activation import ActivationError, ActivationRequest, activate
    request = ActivationRequest(
        business_id=args.business_id,
        enable_commitments=args.enable_commitments,
        enable_projection=args.enable_projection,
        bank_id=args.bank_id,
        deepseek_key_file=args.deepseek_key_file,
        extract_max_calls=args.extract_max_calls,
        extract_max_tokens=args.extract_max_tokens,
        projection_max_docs=args.projection_max_docs,
        projection_max_prose_docs=args.projection_max_prose_docs,
        projection_budget_usd=args.projection_budget_usd,
        own_emails=tuple(args.own_email),
        hindsight_registry=args.hindsight_registry,
        deepseek_base_url=args.deepseek_base_url,
    )
    stages = None
    if request.enabled:
        from olo.signals import format_utc
        conn = connect(None)
        try:
            migrate(conn)
            stages = activate(request, conn, format_utc(utc_now()))
        except ActivationError as exc:
            print(f"olo: S5 activation refused: {exc}", file=sys.stderr)
            return 1
        finally:
            conn.close()

    try:
        result = run_source(
            adapter, limit=args.limit, max_batches=args.max_batches,
            commitment=stages.commitment if stages else None,
            projection=stages.projection if stages else None,
            own_identifiers=stages.own_identifiers if stages else frozenset(),
        )
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
    # The scheduler must not see success when the authoritative pipeline is
    # degraded, or when this run left new items no retry will settle.
    if result.degraded:
        print("olo: degraded: " + "; ".join(result.degraded)
              + " (see `hermes olo status`)", file=sys.stderr)
        return 1
    if result.attention_new:
        print(f"olo: {result.attention_new} new item(s) need an operator "
              f"({result.attention_total} in total; see `hermes olo status`)", file=sys.stderr)
        return 1
    if result.attention_total:
        print(f"olo: {result.attention_total} item(s) still need an operator "
              "(see `hermes olo status`)", file=sys.stderr)
    # succeeded → empty stdout and stderr (Hermes silent success)

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
