"""Olo AI — SQLite store: schema, leases, ingest_batch, reads.

§5.5–5.7: schema v1, per-source ownership (lease), atomic ingest.
§2.1 (M2b): content_blobs table, schema v2, blob handling in ingest_batch.

Rules:
- isolation_level=None (explicit transactions only).
- Every write uses BEGIN IMMEDIATE … COMMIT; ROLLBACK on any exception.
- One connection per runner; never share across threads.
- File permissions: directory 0o700, file 0o600.

No side effects at import time.
"""
from __future__ import annotations

import os
import socket
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Optional

import zlib

from olo.paths import db_path
from olo.signals import (
    NormalizedSignal, ContentBlob,
    MAX_BATCH_BLOB_BYTES,
    canonical_json, format_utc,
)

# ── Schema version ────────────────────────────────────────────────────────────
_SCHEMA_VERSION = 2

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_LEASE_TTL = 900  # seconds


# ── Exceptions ───────────────────────────────────────────────────────────────

class SourceBusy(Exception):
    """Another runner holds the lease for this source and it has not expired."""


class LeaseLost(Exception):
    """The runner's lease was taken by another process (expired and superseded)."""


class CheckpointMoved(Exception):
    """The committed checkpoint changed between the fetch and the ingest_batch call."""


class CursorStalled(Exception):
    """The adapter returned has_more=True but did not advance the cursor, or returned
    an empty batch with has_more=True."""


class AdapterContractError(Exception):
    """The adapter violated its contract (oversized batch, wrong source, etc.)."""


# ── Clock ─────────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(UTC)


# ── Owner string ──────────────────────────────────────────────────────────────

def _make_owner() -> str:
    """Stable per-process identity: ``<hostname>:<pid>:<process start epoch>``."""
    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = "unknown"
    pid = os.getpid()
    # process start time (best-effort; fall back to import time)
    try:
        import psutil  # optional
        start_epoch = int(psutil.Process(pid).create_time())
    except Exception:
        start_epoch = int(time.time())
    return f"{hostname}:{pid}:{start_epoch}"


# ── Connection ────────────────────────────────────────────────────────────────

def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Return a new SQLite connection to the Olo database.

    Sets WAL mode, foreign keys, busy timeout, and synchronous=FULL.
    Creates the DB file at mode 0o600 and restricts an existing parent
    directory to 0o700 on POSIX.
    """
    db = path or db_path()
    db.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(str(db.parent), 0o700)

    conn = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=FULL")

    # Set file permissions after creation
    if os.name != "nt":
        os.chmod(str(db), 0o600)

    return conn


# ── Schema DDL (each statement separate to avoid trigger-body semicolon splits)
_DDL_STATEMENTS = [
    "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS ingest_runs (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    owner         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL CHECK (status IN
                    ('running','succeeded','partial','failed','abandoned')),
    batches       INTEGER NOT NULL DEFAULT 0,
    fetched       INTEGER NOT NULL DEFAULT 0,
    inserted      INTEGER NOT NULL DEFAULT 0,
    duplicates    INTEGER NOT NULL DEFAULT 0,
    conflicts     INTEGER NOT NULL DEFAULT 0,
    error         TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_runs_source_status ON ingest_runs(source, status)",
    """CREATE TABLE IF NOT EXISTS source_leases (
    source      TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES ingest_runs(id),
    owner       TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    kind            TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    subject         TEXT,
    payload         TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    ingested_at     TEXT NOT NULL,
    run_id          TEXT NOT NULL REFERENCES ingest_runs(id),
    UNIQUE (source, external_id)
)""",
    "CREATE INDEX IF NOT EXISTS idx_signals_kind_time ON signals(kind, occurred_at)",
    """CREATE TABLE IF NOT EXISTS sync_checkpoints (
    source      TEXT PRIMARY KEY,
    cursor      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    run_id      TEXT NOT NULL REFERENCES ingest_runs(id)
)""",
    """CREATE TABLE IF NOT EXISTS audit_events (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    subject_type  TEXT,
    subject_id    TEXT,
    detail        TEXT NOT NULL
)""",
    # Triggers — kept as full multi-statement strings; conn.execute handles them
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END",
    # M2b §2.1: content blobs (schema v2)
    """CREATE TABLE IF NOT EXISTS content_blobs (
    source      TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    media_type  TEXT NOT NULL,
    size        INTEGER NOT NULL,
    encoding    TEXT NOT NULL CHECK (encoding IN ('identity','zlib')),
    data        BLOB NOT NULL,
    created_at  TEXT NOT NULL,
    run_id      TEXT NOT NULL REFERENCES ingest_runs(id),
    PRIMARY KEY (source, sha256)
)""",
    "CREATE INDEX IF NOT EXISTS idx_blobs_run ON content_blobs(run_id)",
]


def migrate(conn: Optional[sqlite3.Connection] = None,
            path: Optional[Path] = None) -> None:
    """Create or upgrade schema; idempotent.

    v1 → v2: adds content_blobs table (M2b §2.1).
    Runs in one transaction. Opens a new connection if *conn* is None.
    """
    close_after = conn is None
    if conn is None:
        conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        # Ensure exactly one schema_version row; upgrade v1 → v2 in-place
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)",
                (_SCHEMA_VERSION,),
            )
        elif row["version"] < _SCHEMA_VERSION:
            # In-place upgrade: DDL above is already idempotent (CREATE IF NOT EXISTS)
            conn.execute(
                "UPDATE schema_version SET version = ?",
                (_SCHEMA_VERSION,),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if close_after:
            conn.close()


# ── Audit helper ─────────────────────────────────────────────────────────────

def _audit(conn: sqlite3.Connection, actor: str, action: str,
           detail: dict, *, subject_type: str | None = None,
           subject_id: str | None = None, now: datetime | None = None) -> None:
    """Append one audit event (within an open transaction)."""
    ts = format_utc(now or utc_now())
    conn.execute(
        "INSERT INTO audit_events(at, actor, action, subject_type, subject_id, detail)"
        " VALUES (?,?,?,?,?,?)",
        (ts, actor, action, subject_type, subject_id, canonical_json(detail)),
    )


# ── Lease operations ──────────────────────────────────────────────────────────

def acquire_run(
    source: str,
    owner: str,
    conn: sqlite3.Connection,
    clock: Callable[[], datetime] = utc_now,
    ttl: int = DEFAULT_LEASE_TTL,
) -> str:
    """Acquire the source lease and create an ingest_run row.

    Returns the new run_id.
    Raises SourceBusy if the source is already leased by a live run.
    """
    now = clock()
    run_id = str(uuid.uuid4())
    now_str = format_utc(now)
    expires_str = format_utc(
        datetime.fromtimestamp(now.timestamp() + ttl, tz=UTC)
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = conn.execute(
            "SELECT run_id, owner, expires_at FROM source_leases WHERE source = ?",
            (source,),
        ).fetchone()

        if lease is not None:
            # Is the existing lease still live?
            lease_expires = datetime.fromisoformat(
                lease["expires_at"]
            )
            if lease_expires > now:
                # Lease is live — this source is busy
                _audit(conn, f"runner:{source}", "run.skipped_busy", {
                    "holder_run_id": lease["run_id"],
                    "holder_owner": lease["owner"],
                    "expires_at": lease["expires_at"],
                }, subject_type="run", subject_id=lease["run_id"], now=now)
                conn.execute("COMMIT")
                raise SourceBusy(
                    f"source {source!r} is held by run {lease['run_id']} "
                    f"(owner={lease['owner']}, expires={lease['expires_at']})"
                )

            # Lease expired — abandon all running rows for this source
            running_rows = conn.execute(
                "SELECT id FROM ingest_runs WHERE source = ? AND status = 'running'",
                (source,),
            ).fetchall()
            for row in running_rows:
                conn.execute(
                    "UPDATE ingest_runs SET status='abandoned', finished_at=?, "
                    "error='lease expired' WHERE id = ? AND status='running'",
                    (now_str, row["id"]),
                )
                _audit(conn, f"runner:{source}", "run.abandoned", {
                    "abandoned_run_id": row["id"],
                    "reason": "lease expired",
                }, subject_type="run", subject_id=row["id"], now=now)

        # Insert the new run row
        conn.execute(
            "INSERT INTO ingest_runs(id, source, owner, started_at, status)"
            " VALUES (?,?,?,?,?)",
            (run_id, source, owner, now_str, "running"),
        )
        # Upsert the lease
        conn.execute(
            "INSERT INTO source_leases(source, run_id, owner, acquired_at, expires_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(source) DO UPDATE SET"
            "   run_id=excluded.run_id,"
            "   owner=excluded.owner,"
            "   acquired_at=excluded.acquired_at,"
            "   expires_at=excluded.expires_at",
            (source, run_id, owner, now_str, expires_str),
        )
        _audit(conn, f"runner:{source}", "run.started", {
            "run_id": run_id,
            "owner": owner,
        }, subject_type="run", subject_id=run_id, now=now)
        conn.execute("COMMIT")
        return run_id
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def renew_lease(
    source: str,
    run_id: str,
    conn: sqlite3.Connection,
    clock: Callable[[], datetime] = utc_now,
    ttl: int = DEFAULT_LEASE_TTL,
) -> None:
    """Extend the lease for *run_id*; raises LeaseLost if superseded or expired."""
    now = clock()
    new_expires = format_utc(
        datetime.fromtimestamp(now.timestamp() + ttl, tz=UTC)
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        lease = conn.execute(
            "SELECT run_id, expires_at FROM source_leases WHERE source = ?",
            (source,),
        ).fetchone()
        if lease is None or lease["run_id"] != run_id:
            conn.execute("ROLLBACK")
            raise LeaseLost(
                f"lease for source {source!r} no longer belongs to run {run_id!r}"
            )
        lease_expires = datetime.fromisoformat(
            lease["expires_at"]
        )
        if lease_expires <= now:
            conn.execute("ROLLBACK")
            raise LeaseLost(
                f"lease for source {source!r} run {run_id!r} has expired"
            )
        conn.execute(
            "UPDATE source_leases SET expires_at = ? WHERE source = ?",
            (new_expires, source),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def finish_run(
    source: str,
    run_id: str,
    status: str,
    conn: sqlite3.Connection,
    clock: Callable[[], datetime] = utc_now,
    error: Optional[str] = None,
) -> bool:
    """Mark the run as terminal and release the lease.

    Returns True if the row was updated (run was still 'running' when called).
    Returns False if the run was already abandoned by a successor — in that
    case the caller must NOT report success to its own caller.
    """
    now_str = format_utc(clock())
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = conn.execute(
            "UPDATE ingest_runs SET status=?, finished_at=?, error=?"
            " WHERE id=? AND status='running'",
            (status, now_str, error, run_id),
        )
        updated = result.rowcount > 0
        if updated:
            conn.execute(
                "DELETE FROM source_leases WHERE source=? AND run_id=?",
                (source, run_id),
            )
            action_map = {
                "succeeded": "run.succeeded",
                "partial": "run.partial",
                "failed": "run.failed",
            }
            action = action_map.get(status, f"run.{status}")
            detail: dict = {"run_id": run_id}
            if error:
                detail["error"] = error
            _audit(conn, f"runner:{source}", action, detail,
                   subject_type="run", subject_id=run_id,
                   now=datetime.fromisoformat(now_str))
        conn.execute("COMMIT")
        return updated
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ── Blob helpers (§2.1) ───────────────────────────────────────────────────────

_ZLIB_MIN_SAVING = 0.10  # compress only when it saves ≥ 10 %


def _compress_blob(data: bytes) -> tuple[bytes, str]:
    """Return (stored_bytes, encoding).  Uses zlib when it saves ≥ 10 %."""
    compressed = zlib.compress(data, level=6)
    if len(compressed) < len(data) * (1 - _ZLIB_MIN_SAVING):
        return compressed, "zlib"
    return data, "identity"


def _insert_blob(
    conn: sqlite3.Connection,
    source: str,
    blob: ContentBlob,
    run_id: str,
    now_str: str,
) -> None:
    """INSERT OR IGNORE one blob row keyed (source, sha256)."""
    stored_data, encoding = _compress_blob(blob.data)
    conn.execute(
        "INSERT OR IGNORE INTO content_blobs"
        "(source, sha256, media_type, size, encoding, data, created_at, run_id)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            source,
            blob.sha256,
            blob.media_type,
            len(blob.data),          # always store uncompressed size
            encoding,
            stored_data,
            now_str,
            run_id,
        ),
    )


# ── Atomic ingest ──────────────────────────────────────────────────────────────

def ingest_batch(
    source: str,
    run_id: str,
    batch_signals: tuple[NormalizedSignal, ...],
    next_cursor: Optional[str],
    expected_cursor: Optional[str],
    conn: sqlite3.Connection,
    clock: Callable[[], datetime] = utc_now,
    _fault: Optional[Callable[[str], None]] = None,
) -> tuple[int, int, int]:
    """Atomically commit one batch: blobs + signals + checkpoint + counter update.

    Returns (inserted, duplicates, conflicts).
    Raises LeaseLost, CheckpointMoved, AdapterContractError, or any exception
    from _fault injection.

    _fault is called at named checkpoints for failure injection tests only.
    Production callers never pass _fault.
    """
    now = clock()
    now_str = format_utc(now)
    inserted = duplicates = conflicts = 0

    # Pre-flight: batch blob byte cap (§2.2)
    total_blob_bytes = sum(
        len(b.data) for sig in batch_signals for b in (sig.blobs or ())
    )
    if total_blob_bytes > MAX_BATCH_BLOB_BYTES:
        raise AdapterContractError(
            f"batch blob bytes {total_blob_bytes} exceed MAX_BATCH_BLOB_BYTES "
            f"({MAX_BATCH_BLOB_BYTES})"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 0. Verify lease is still ours
        lease = conn.execute(
            "SELECT run_id, expires_at FROM source_leases WHERE source = ?",
            (source,),
        ).fetchone()
        if lease is None or lease["run_id"] != run_id:
            conn.execute("ROLLBACK")
            raise LeaseLost(
                f"lease for {source!r} no longer belongs to run {run_id!r}"
            )
        lease_expires = datetime.fromisoformat(
            lease["expires_at"]
        )
        if lease_expires <= now:
            conn.execute("ROLLBACK")
            raise LeaseLost(
                f"lease for {source!r} run {run_id!r} expired at {lease['expires_at']}"
            )

        # 1. Verify checkpoint hasn't moved
        cp_row = conn.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source = ?",
            (source,),
        ).fetchone()
        committed_cursor = cp_row["cursor"] if cp_row else None
        if committed_cursor != expected_cursor:
            conn.execute("ROLLBACK")
            raise CheckpointMoved(
                f"expected cursor {expected_cursor!r} but committed is "
                f"{committed_cursor!r} for source {source!r}"
            )

        # 1b. Insert blobs (before signals, same transaction; INSERT OR IGNORE)
        #     Also validate that every content_ref sha256 is satisfiable.
        for signal in batch_signals:
            if signal.source != source:
                conn.execute("ROLLBACK")
                raise AdapterContractError(
                    f"signal source {signal.source!r} != run source {source!r}"
                )
            # Insert each blob
            for blob in (signal.blobs or ()):
                _insert_blob(conn, source, blob, run_id, now_str)

            # Validate refs: every content_ref sha256 must exist in this batch
            # or already be stored for this source.
            refs = signal.payload.get("content_refs")
            if refs:
                batch_sha256s = {b.sha256 for b in (signal.blobs or ())}
                for ref in refs:
                    ref_sha256 = ref.get("sha256") if isinstance(ref, dict) else None
                    if not ref_sha256:
                        continue
                    if ref_sha256 in batch_sha256s:
                        continue
                    # Check whether it's already stored for this source
                    stored = conn.execute(
                        "SELECT 1 FROM content_blobs WHERE source=? AND sha256=?",
                        (source, ref_sha256),
                    ).fetchone()
                    if stored is None:
                        conn.execute("ROLLBACK")
                        raise AdapterContractError(
                            f"content_ref sha256 {ref_sha256!r} in signal "
                            f"{signal.external_id!r} is not in this batch and "
                            f"not stored for source {source!r}"
                        )

        if _fault:
            _fault("after_blobs")

        # 2. Insert signals
        for signal in batch_signals:
            sha = signal.content_sha256()
            result = conn.execute(
                "INSERT INTO signals"
                "(source, external_id, kind, occurred_at, subject, payload,"
                " content_sha256, schema_version, ingested_at, run_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(source, external_id) DO NOTHING",
                (
                    signal.source,
                    signal.external_id,
                    signal.kind,
                    format_utc(signal.occurred_at),
                    signal.subject,
                    canonical_json(dict(signal.payload)),
                    sha,
                    signal.schema_version,
                    now_str,
                    run_id,
                ),
            )
            if result.rowcount == 0:
                # Duplicate or conflict
                stored = conn.execute(
                    "SELECT content_sha256 FROM signals"
                    " WHERE source=? AND external_id=?",
                    (signal.source, signal.external_id),
                ).fetchone()
                if stored and stored["content_sha256"] == sha:
                    duplicates += 1
                else:
                    conflicts += 1
                    stored_sha = stored["content_sha256"] if stored else None
                    _audit(conn, f"runner:{source}", "signal.conflict", {
                        "external_id": signal.external_id,
                        "stored_sha256": stored_sha,
                        "new_sha256": sha,
                    }, subject_type="signal",
                           subject_id=signal.external_id, now=now)
            else:
                inserted += 1

        if _fault:
            _fault("after_signals")

        # 3. Update checkpoint
        if next_cursor is not None:
            conn.execute(
                "INSERT INTO sync_checkpoints(source, cursor, updated_at, run_id)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(source) DO UPDATE SET"
                "   cursor=excluded.cursor,"
                "   updated_at=excluded.updated_at,"
                "   run_id=excluded.run_id",
                (source, next_cursor, now_str, run_id),
            )

        if _fault:
            _fault("after_checkpoint")

        # 4. Update run counters
        conn.execute(
            "UPDATE ingest_runs SET"
            "  batches  = batches  + 1,"
            "  fetched  = fetched  + ?,"
            "  inserted = inserted + ?,"
            "  duplicates = duplicates + ?,"
            "  conflicts  = conflicts  + ?"
            " WHERE id = ?",
            (len(batch_signals), inserted, duplicates, conflicts, run_id),
        )

        # 5. Audit batch commit
        _audit(conn, f"runner:{source}", "batch.committed", {
            "run_id": run_id,
            "cursor_from": expected_cursor,
            "cursor_to": next_cursor,
            "inserted": inserted,
            "duplicates": duplicates,
            "conflicts": conflicts,
        }, now=now)

        if _fault:
            _fault("before_commit")

        conn.execute("COMMIT")

        if _fault:
            _fault("after_commit")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return inserted, duplicates, conflicts


# ── Read helpers ──────────────────────────────────────────────────────────────

def get_checkpoint(source: str, conn: sqlite3.Connection) -> Optional[str]:
    """Return the committed cursor for *source*, or None."""
    row = conn.execute(
        "SELECT cursor FROM sync_checkpoints WHERE source = ?", (source,)
    ).fetchone()
    return row["cursor"] if row else None


def count_signals(source: str, conn: sqlite3.Connection) -> int:
    """Return the total number of ingested signals for *source*."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM signals WHERE source = ?", (source,)
    ).fetchone()
    return row["n"]


def get_run(run_id: str, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Return the ingest_run row for *run_id*, or None."""
    return conn.execute(
        "SELECT * FROM ingest_runs WHERE id = ?", (run_id,)
    ).fetchone()


def get_lease(source: str, conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """Return the lease row for *source*, or None."""
    return conn.execute(
        "SELECT * FROM source_leases WHERE source = ?", (source,)
    ).fetchone()


# ── Content blob reads (§2.2) ─────────────────────────────────────────────────

def get_blob_raw(source: str, sha256: str, conn: sqlite3.Connection) -> Optional[bytes]:
    """Return the uncompressed bytes of a stored blob, or None.

    *source* is required; there is no cross-source read.
    """
    row = conn.execute(
        "SELECT encoding, data FROM content_blobs WHERE source=? AND sha256=?",
        (source, sha256),
    ).fetchone()
    if row is None:
        return None
    if row["encoding"] == "zlib":
        return zlib.decompress(row["data"])
    return bytes(row["data"])


def purge_content(source: str, conn: sqlite3.Connection,
                  clock: Callable[[], datetime] = utc_now) -> int:
    """Delete all content blobs for *source*; return the number deleted.

    Writes an audit event inside a transaction.
    Signals and checkpoints are NOT touched.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute(
            "SELECT COUNT(*) n FROM content_blobs WHERE source=?", (source,)
        ).fetchone()["n"]
        conn.execute("DELETE FROM content_blobs WHERE source=?", (source,))
        _audit(conn, "cli:content", "content.purged", {
            "source": source,
            "deleted_count": n,
        }, subject_type="source", subject_id=source, now=clock())
        conn.execute("COMMIT")
        return n
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
