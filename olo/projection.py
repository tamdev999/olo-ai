"""olo/projection.py — project verified Olo state into Hindsight (S5 part 2).

olo.db is authoritative; Hindsight is a derived, rebuildable projection.
This stage computes the documents Hindsight should hold and drives them
through the existing ``hindsight_projection`` outbox:

* Signal documents (``sig:<signal_id>``) — built by the one item builder
  (``olo.m3.feeder.build_signal_item``) from content read through
  ``olo.signal_content.load_content``.  The entity link is the *verified*
  canonical contact from ``signal_resolution`` (never a guess); the
  commitment link (id + version) is added to the metadata.  A signal is
  projected once its identity is settled and, when commitments are being
  extracted, once its commitment outcome is settled.  Metadata copies of
  Gmail messages superseded by full content are not projected.
* Ledger-state documents (``ledger:<commitment_id>:v<version>``) — one per
  confirmed ledger version: status and date as of that version, the version
  it supersedes, and its supporting documents.  Pending proposals are not
  state changes and get no state document.

Change detection: the outbox's ``content_sha256`` column holds a fingerprint
of the whole item, so a changed identity or commitment link re-projects the
document under the same document_id (verified live on Hindsight 1.2.1 for
structured documents: content, tags and metadata are replaced and the old
fact removed — see docs/olo/m3/S5_PART3_EVIDENCE.md).

Bounded work (S5 part 3): every submission is an operation in
``hindsight_operations`` driven to completed / failed / cancelled / lost;
in-flight documents are never resubmitted; stale operations are cancelled;
a document stops being retried after ``max_attempts_per_doc``.  Submissions
stop when the per-run or per-window document caps (persisted in olo.db) or
the ``usage_guard`` budget (server-reported LLM spend since a persisted
window baseline) are reached; the guard's stop cancels this bank's in-flight
operations.  These are client-side controls, not a server-enforced monetary
cap: work already accepted by Hindsight runs until it completes or is
cancelled.  Without readable usage statistics nothing is submitted.

Scope configuration is unchanged: items carry the same observation scopes as
the benchmark feed.  Isolation: one olo.db and one bank per business; the
database is bound to its bank on first projection and refuses any other.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from olo.signal_content import PROSE_KINDS, load_content
from olo.signals import format_utc

__all__ = [
    "ProjectionConfig",
    "ProjectionLimits",
    "ProjectionRefused",
    "bind_bank",
    "desired_documents",
    "run_projection_stage",
]

_IDENTITY_SETTLED = ("resolved", "unresolved", "ambiguous", "skipped")
_COMMITMENT_UNSETTLED = ("pending", "failed")
_OP_DONE = "completed"
#: Operation states that mean "still running": keep polling, never resubmit.
_OP_IN_FLIGHT = ("pending", "processing", "running", "queued")
_EARLIEST = datetime.min.replace(tzinfo=UTC)


class ProjectionRefused(Exception):
    """Projection must not run (wrong bank for this business, bad config)."""


@dataclass(frozen=True)
class ProjectionLimits:
    """Bounds on Hindsight work.  Window limits are persisted in olo.db, so a
    restart (or a crash loop) cannot reset them.

    Document caps are enforced locally before submission.  ``usage_guard``
    (ProjectionConfig) adds the bank's server-reported LLM spend; neither is a
    server-side monetary cap: work already submitted keeps running server-side
    until it completes or is cancelled.
    """
    max_docs_per_run: int = 200
    max_prose_docs_per_run: int = 50
    max_docs_per_window: int = 1_000
    max_prose_docs_per_window: int = 200
    window_hours: int = 24
    max_attempts_per_doc: int = 3
    operation_timeout_minutes: int = 60


@dataclass(frozen=True)
class ProjectionConfig:
    """Enables the projection stage in run_source.

    ``client``: HindsightClient-compatible (retain, get_operation_status,
    cancel_operation).  ``usage_guard``: UsageGuard-compatible (snapshot,
    check) — required: without readable usage statistics nothing is
    submitted.  ``await_commitment``: hold verified prose signals until their
    commitment outcome is settled, so each is projected once with its link.
    """
    client: Any
    bank_id: str
    usage_guard: Any
    limits: ProjectionLimits = field(default_factory=ProjectionLimits)
    batch_size: int = 50
    await_commitment: bool = True


def _fingerprint(item: dict) -> str:
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


# ── Signal documents ──────────────────────────────────────────────────────────

def _signal_documents(
    conn: sqlite3.Connection, source: str, await_commitment: bool,
) -> list[dict]:
    from olo.adapters.contacts import load_contact_index
    from olo.m3.feeder import _load_entity_meta_from_db, build_signal_item
    from olo.store import get_blob_raw

    _, contact_info = load_contact_index(conn)
    entity_cache = _load_entity_meta_from_db(conn)
    marks = ",".join("?" * len(_IDENTITY_SETTLED))
    rows = conn.execute(
        "SELECT s.external_id, s.kind, s.occurred_at, s.subject, s.payload,"
        "       sr.identity_status, sr.contact_id, sr.commitment_status,"
        "       sr.commitment_outcome, sr.commitment_id, sr.commitment_version"
        " FROM signals s JOIN signal_resolution sr"
        "   ON sr.signal_id = s.external_id AND sr.source = s.source"
        f" WHERE s.source = ? AND sr.identity_status IN ({marks})"
        " ORDER BY s.id",
        (source, *_IDENTITY_SETTLED),
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        if row["commitment_outcome"] == "superseded":
            continue
        # Only verified identities reach the commitment stage, so only they
        # wait for its outcome; others are projected without a customer link.
        if (await_commitment and row["kind"] in PROSE_KINDS
                and row["identity_status"] == "resolved"
                and row["commitment_status"] in _COMMITMENT_UNSETTLED):
            continue
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else dict(row["payload"])
        loaded = load_content(payload, lambda sha: get_blob_raw(source, sha, conn))
        if loaded.status == "no_ref":
            content, sha = (row["subject"] or ""), ""
        elif loaded.status == "ok":
            content, sha = loaded.rendered, loaded.sha256
        else:
            continue                   # evidence unreadable: never project a guess
        if not content.strip():
            continue
        contact = row["contact_id"] if row["identity_status"] == "resolved" else ""
        extra = {}
        if row["commitment_id"]:
            extra = {"commitment_id": row["commitment_id"],
                     "commitment_version": str(row["commitment_version"] or "")}
        items.append(build_signal_item(
            row, payload, content, sha,
            source=source, contact_id=contact or "", contact_info=contact_info,
            entity_cache=entity_cache, production=True, extra_metadata=extra,
        ))
    return items


# ── Ledger-state documents ────────────────────────────────────────────────────

def _ledger_documents(conn: sqlite3.Connection) -> list[dict]:
    from olo.ledger import CANCEL_ACTION, STATUS_CANCELLED, STATUS_SCHEDULED, utc_instant
    from olo.m3.feeder import STRUCTURED_STRATEGY, _ym_tag

    try:
        commitments = conn.execute(
            "SELECT id, contact_id, event_type, business_key FROM commitments ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return []                      # ledger not initialised yet
    items: list[dict] = []
    for c in commitments:
        cid, contact, event_type, key = c[0], c[1], c[2], c[3]
        versions = conn.execute(
            "SELECT version, action, actor_role, confirmed_date, prior_confirmed_date,"
            "       occurred_at, source_doc_id, notes"
            " FROM commitment_versions WHERE commitment_id=? AND is_confirmed=1",
            (cid,),
        ).fetchall()
        ordered = sorted(versions, key=lambda v: (utc_instant(v[5]) or _EARLIEST, v[0]))
        previous = None
        for v in ordered:
            version, action, actor, confirmed, prior, occurred, doc, notes = tuple(v)
            cancelled = action == CANCEL_ACTION
            status = STATUS_CANCELLED if cancelled else STATUS_SCHEDULED
            date = None if cancelled else confirmed
            supporting = [d for d in (notes, doc) if d]
            supersedes = (
                f"supersedes version {previous[0]} (date {previous[3] or 'none'})"
                if previous is not None else "first confirmed version"
            )
            content = (
                f"Commitment ledger state (authoritative record: olo.db).\n"
                f"Commitment {cid} · {event_type} · key {key or 'none'} · customer {contact}.\n"
                f"Status as of version {version}: {status}"
                + (f", date {date}" if date else ", no scheduled date")
                + (f" (cancelled date {prior})" if cancelled and prior else "")
                + f".\nVersion {version}: {action} by {actor} at {occurred}; {supersedes}.\n"
                f"Supporting documents: {', '.join(supporting) or 'none'}."
            )
            ent_tag = f"ent:{contact}"
            items.append({
                "content": content,
                "timestamp": occurred,
                "document_id": f"ledger:{cid}:v{version}",
                "entities": [{"text": contact, "type": "person"}],
                "resolve_entities": False,
                "tags": [ent_tag, _ym_tag(occurred), "kind:ledger.state"],
                "observation_scopes": [[ent_tag, "ym:*"], [ent_tag]],
                "metadata": {
                    "kind": "ledger.state",
                    "commitment_id": cid,
                    "version": str(version),
                    "status": status,
                    "scheduled_date": date or "",
                    "superseded_version": str(previous[0]) if previous is not None else "",
                    "supporting_documents": ",".join(supporting),
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "render_version": "1",
                },
                "strategy": STRUCTURED_STRATEGY,
            })
            previous = (version, action, actor, confirmed)
    return items


def desired_documents(
    conn: sqlite3.Connection, source: str, *, await_commitment: bool = True,
) -> list[dict]:
    """Every document Hindsight should hold for this source's business."""
    return _signal_documents(conn, source, await_commitment) + _ledger_documents(conn)


# ── Bank binding ──────────────────────────────────────────────────────────────

def bind_bank(conn: sqlite3.Connection, bank_id: str, now: str) -> None:
    """Bind this olo.db (one business) to one Hindsight bank, once.

    Raises ProjectionRefused when the database is already bound to another
    bank: projecting one business into another's bank is never allowed.
    """
    row = conn.execute(
        "SELECT value FROM olo_bindings WHERE name='hindsight_bank_id'"
    ).fetchone()
    if row is None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO olo_bindings (name, value, bound_at)"
            " VALUES ('hindsight_bank_id', ?, ?)", (bank_id, now))
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT value FROM olo_bindings WHERE name='hindsight_bank_id'").fetchone()
    if row[0] != bank_id:
        raise ProjectionRefused(
            f"this business database is bound to Hindsight bank {row[0]!r}, "
            f"not {bank_id!r}; refusing to project")


# ── Budget window (persisted) ─────────────────────────────────────────────────

def _window_baseline(conn, cfg: ProjectionConfig, now_dt: datetime, now: str):
    """(window_start, baseline TokenSnapshot) for this bank's current window.

    A new window takes a fresh server snapshot (fails closed when stats are
    unavailable); an open window reuses the stored baseline, so restarting
    the worker never resets spend accounting.
    """
    from olo.m3.hindsight.budget import TokenSnapshot

    row = conn.execute(
        "SELECT window_start, baseline_input_tokens, baseline_output_tokens"
        " FROM hindsight_budget_window WHERE bank_id=?", (cfg.bank_id,)).fetchone()
    if row is not None:
        start = datetime.fromisoformat(row[0])
        if now_dt - start < timedelta(hours=cfg.limits.window_hours):
            return row[0], TokenSnapshot(int(row[1]), int(row[2]))
    snap = cfg.usage_guard.snapshot()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO hindsight_budget_window"
        " (bank_id, window_start, baseline_input_tokens, baseline_output_tokens)"
        " VALUES (?,?,?,?) ON CONFLICT(bank_id) DO UPDATE SET"
        "  window_start=excluded.window_start,"
        "  baseline_input_tokens=excluded.baseline_input_tokens,"
        "  baseline_output_tokens=excluded.baseline_output_tokens",
        (cfg.bank_id, now, snap.input_tokens, snap.output_tokens))
    conn.execute("COMMIT")
    return now, snap


def _window_usage(conn, bank_id: str, window_start: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COALESCE(SUM(doc_count),0), COALESCE(SUM(prose_count),0)"
        " FROM hindsight_operations WHERE bank_id=? AND submitted_at >= ?",
        (bank_id, window_start)).fetchone()
    return int(row[0]), int(row[1])


# ── Operations ────────────────────────────────────────────────────────────────

def _settle(conn, bank_id: str, op: str, status: str, now: str, detail: str = "") -> int:
    """Record an operation's terminal status and its documents' outcome."""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE hindsight_operations SET status=?, settled_at=?, detail=?"
        " WHERE operation_id=? AND bank_id=?", (status, now, detail[:500], op, bank_id))
    if status == "completed":
        n = conn.execute(
            "UPDATE hindsight_projection SET status='confirmed', confirmed_at=?"
            " WHERE bank_id=? AND operation_id=? AND status='pending'",
            (now, bank_id, op)).rowcount
    else:
        n = conn.execute(
            "UPDATE hindsight_projection SET status='failed'"
            " WHERE bank_id=? AND operation_id=? AND status='pending'",
            (bank_id, op)).rowcount
    conn.execute("COMMIT")
    return n


def _poll_in_flight(conn, cfg: ProjectionConfig, now_dt: datetime, now: str) -> dict[str, int]:
    """Drive every submitted operation of this bank towards a terminal state.

    completed → documents confirmed; failed/partial → failed (retried within
    max_attempts_per_doc); unknown to the server (404/not_found) → lost and
    retried; still running past operation_timeout_minutes → cancelled through
    the client's cancellation API.  A transient polling error leaves it be.
    """
    out = {"confirmed": 0, "failed": 0, "cancelled": 0, "lost": 0}
    rows = conn.execute(
        "SELECT operation_id, submitted_at FROM hindsight_operations"
        " WHERE bank_id=? AND status='submitted'", (cfg.bank_id,)).fetchall()
    for op, submitted_at in rows:
        try:
            state = (cfg.client.get_operation_status(op, bank_id=cfg.bank_id) or {}).get("status")
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                continue                     # transient: poll again next run
            state = "not_found"
        if state in _OP_IN_FLIGHT:
            age = now_dt - datetime.fromisoformat(submitted_at)
            if age < timedelta(minutes=cfg.limits.operation_timeout_minutes):
                continue
            try:
                cfg.client.cancel_operation(op, bank_id=cfg.bank_id)
            except Exception:
                continue                     # cancel failed: try again next run
            out["cancelled"] += _settle(conn, cfg.bank_id, op, "cancelled", now, "timed out")
        elif state == _OP_DONE:
            out["confirmed"] += _settle(conn, cfg.bank_id, op, "completed", now)
        elif state == "not_found":
            out["lost"] += _settle(conn, cfg.bank_id, op, "lost", now, "unknown to server")
        else:
            out["failed"] += _settle(conn, cfg.bank_id, op, "failed", now, str(state))
    return out


def _cancel_in_flight(conn, cfg: ProjectionConfig, now: str, why: str) -> int:
    """Cancel this bank's submitted operations (budget exhausted)."""
    n = 0
    for (op,) in conn.execute(
            "SELECT operation_id FROM hindsight_operations WHERE bank_id=? AND status='submitted'",
            (cfg.bank_id,)).fetchall():
        try:
            cfg.client.cancel_operation(op, bank_id=cfg.bank_id)
        except Exception:
            continue
        n += _settle(conn, cfg.bank_id, op, "cancelled", now, why)
    return n


def _failed_attempts(conn, bank_id: str, document_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM hindsight_operation_docs d"
        " JOIN hindsight_operations o ON o.operation_id = d.operation_id"
        " WHERE o.bank_id=? AND d.document_id=?"
        "   AND o.status IN ('failed','cancelled','lost')",
        (bank_id, document_id)).fetchone()[0]


def _is_prose(item: dict) -> bool:
    from olo.m3.feeder import PROSE_STRATEGY
    return item.get("strategy") == PROSE_STRATEGY


def _enqueue(conn, cfg: ProjectionConfig, source: str, op: str, now: str,
             batch: list[tuple[dict, str]], prose_n: int) -> None:
    """Record a submission (operation, its documents, outbox rows); caller commits."""
    conn.execute(
        "INSERT INTO hindsight_operations (operation_id, bank_id, source, submitted_at,"
        " doc_count, prose_count, status) VALUES (?,?,?,?,?,?,'submitted')",
        (op, cfg.bank_id, source, now, len(batch), prose_n))
    for item, fp in batch:
        conn.execute("INSERT INTO hindsight_operation_docs VALUES (?,?)",
                     (op, item["document_id"]))
        conn.execute(
            "INSERT INTO hindsight_projection"
            " (signal_id, bank_id, content_sha256, operation_id, status)"
            " VALUES (?,?,?,?,'pending')"
            " ON CONFLICT(signal_id, bank_id) DO UPDATE SET"
            "  content_sha256=excluded.content_sha256, operation_id=excluded.operation_id,"
            "  status='pending', confirmed_at=NULL",
            (item["document_id"], cfg.bank_id, fp, op))


# ── Stage ─────────────────────────────────────────────────────────────────────

def run_projection_stage(
    conn: sqlite3.Connection,
    source: str,
    cfg: ProjectionConfig,
    clock: Callable[[], datetime],
    *,
    _fault: Optional[Callable[[str], None]] = None,
) -> dict[str, int]:
    """Bring Hindsight in line with olo.db for *source*, within budget.

    Order: bank binding check → settle submitted operations → persisted
    budget window → diff desired documents against the outbox → submit in
    batches while the run/window document caps and the usage guard allow.
    Idempotent and resumable; never resubmits an in-flight document.
    """
    from olo.m3.hindsight.budget import BudgetExceededError, StatsUnavailableError

    now_dt = clock()
    now = format_utc(now_dt)
    bind_bank(conn, cfg.bank_id, now)
    counts = {"submitted": 0, "unchanged": 0, "deferred": 0, "retries_exhausted": 0,
              "budget_exceeded": 0, "stats_unavailable": 0}
    counts.update(_poll_in_flight(conn, cfg, now_dt, now))

    try:
        window_start, baseline = _window_baseline(conn, cfg, now_dt, now)
    except StatsUnavailableError:
        counts["stats_unavailable"] = 1      # fail closed: nothing submitted
        return counts

    in_flight = {r[0] for r in conn.execute(
        "SELECT d.document_id FROM hindsight_operation_docs d"
        " JOIN hindsight_operations o ON o.operation_id = d.operation_id"
        " WHERE o.bank_id=? AND o.status='submitted'", (cfg.bank_id,))}
    existing = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT signal_id, content_sha256, status FROM hindsight_projection WHERE bank_id=?",
        (cfg.bank_id,))}
    todo: list[tuple[dict, str]] = []
    for item in desired_documents(conn, source, await_commitment=cfg.await_commitment):
        doc, fp = item["document_id"], _fingerprint(item)
        prior = existing.get(doc)
        if doc in in_flight or (prior is not None and prior == (fp, "confirmed")):
            counts["unchanged"] += 1
            continue
        if _failed_attempts(conn, cfg.bank_id, doc) >= cfg.limits.max_attempts_per_doc:
            counts["retries_exhausted"] += 1
            continue
        todo.append((item, fp))

    lim = cfg.limits
    run_docs = run_prose = 0
    win_docs, win_prose = _window_usage(conn, cfg.bank_id, window_start)
    queue = list(todo)
    while queue:
        try:
            cfg.usage_guard.check(baseline)
        except BudgetExceededError:
            counts["budget_exceeded"] = 1
            counts["cancelled"] += _cancel_in_flight(conn, cfg, now, "usage budget exceeded")
            break
        except StatsUnavailableError:
            counts["stats_unavailable"] = 1
            break
        batch: list[tuple[dict, str]] = []
        rest: list[tuple[dict, str]] = []
        for item, fp in queue:
            prose = _is_prose(item)
            fits = (len(batch) < cfg.batch_size
                    and run_docs + len(batch) < lim.max_docs_per_run
                    and win_docs + len(batch) < lim.max_docs_per_window)
            if fits and prose:
                p = sum(1 for i, _ in batch if _is_prose(i))
                fits = (run_prose + p < lim.max_prose_docs_per_run
                        and win_prose + p < lim.max_prose_docs_per_window)
            (batch if fits else rest).append((item, fp))
        if not batch:
            break
        prose_n = sum(1 for i, _ in batch if _is_prose(i))
        # A fresh id per submission: the outbox, not the id, prevents
        # duplicate submissions, and a retry must never collide with the
        # failed operation it replaces.
        op = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            _enqueue(conn, cfg, source, op, now, batch, prose_n)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        run_docs += len(batch)
        run_prose += prose_n
        win_docs += len(batch)
        win_prose += prose_n
        if _fault:
            _fault("after_projection_enqueue")
        try:
            cfg.client.retain([i for i, _ in batch], operation_id=op,
                              async_=True, bank_id=cfg.bank_id)
            counts["submitted"] += len(batch)
        except Exception as exc:
            counts["failed"] += _settle(conn, cfg.bank_id, op, "failed", now,
                                        f"submit failed: {type(exc).__name__}")
        queue = rest
    counts["deferred"] = len(queue)
    settled = _poll_in_flight(conn, cfg, now_dt, now)
    for k, v in settled.items():
        counts[k] += v
    return counts
