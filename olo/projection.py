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

Bank preparation (S5 preflight): before anything is retained the bank is
created if missing and Olo's ``structured-no-llm`` retain strategy (chunks
extraction, observations off) is merged into its ``retain_strategies``.
Hindsight's config PATCH replaces a top-level key wholesale, so the current
strategies are read and only Olo's entry is added or corrected; every other
bank setting is left alone.  Repeating it is a read and no write.  A bank that
cannot be prepared receives nothing: ledger-state documents would otherwise
fall back to LLM extraction.

Consolidation: server auto-consolidation is off (setup.sh), so observations
form only when the stage asks for them.  Prose documents carry the
``olo:prose`` tag and consolidation is scoped to it: Hindsight 1.2.1 counts
and consolidates structured memories too (a retain strategy's
``enable_observations`` is not applied per memory — verified live), so a
bank-wide run would send ledger-state and structured signal memories to the
LLM and the bank-wide pending count never drains.  Once no retain of the
bank is in flight and the server reports prose memories pending
consolidation, one consolidation is triggered and tracked like a retain:
polled to completed / failed / cancelled / lost, cancelled when stale or when
the usage guard stops.  A completed retain is never taken as consolidated,
and neither is a completed consolidation (the server reports a skipped job
as completed): work is done only when the server reports no prose memory
pending (``consolidation_pending`` == 0).  At most
``max_consolidation_attempts`` consolidations run without new prose in
between, and at most ``max_consolidations_per_window`` per window.

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
    "BankNotPrepared",
    "ProjectionConfig",
    "ProjectionLimits",
    "ProjectionRefused",
    "bind_bank",
    "desired_documents",
    "prepare_bank",
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


class BankNotPrepared(Exception):
    """The bank's required configuration could not be applied or verified."""


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
    max_consolidation_attempts: int = 3
    max_consolidations_per_window: int = 24


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
        item = build_signal_item(
            row, payload, content, sha,
            source=source, contact_id=contact or "", contact_info=contact_info,
            entity_cache=entity_cache, production=True, extra_metadata=extra,
        )
        if _is_prose(item):
            item["tags"] = [*item["tags"], PROSE_SCOPE_TAG]
        items.append(item)
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


# ── Bank preparation ──────────────────────────────────────────────────────────

def _strategies(state: dict) -> dict:
    return dict((state.get("config") or {}).get("retain_strategies") or {})


def _has(strategies: dict, name: str, wanted: dict) -> bool:
    got = strategies.get(name)
    return isinstance(got, dict) and all(got.get(k) == v for k, v in wanted.items())


def prepare_bank(client: Any, bank_id: str) -> str:
    """Make *bank_id* ready for Olo documents; returns created/updated/unchanged.

    Creates the bank when the server does not know it, then ensures the
    resolved ``retain_strategies`` contain Olo's structured strategy with the
    required settings.  The PATCH carries the current resolved strategies
    plus Olo's entry, because Hindsight replaces a top-level config key
    wholesale; no other key is sent.  Raises BankNotPrepared when the server
    refuses or the result cannot be verified.
    """
    from olo.m3.feeder import _BANK_CONFIG_OVERRIDES, STRUCTURED_STRATEGY
    from olo.m3.hindsight.client import HindsightAPIError, HindsightError

    wanted = _BANK_CONFIG_OVERRIDES["retain_strategies"][STRUCTURED_STRATEGY]
    created = False
    try:
        try:
            state = client.get_bank_config(bank_id=bank_id)
        except HindsightAPIError as exc:
            if exc.status_code != 404:
                raise
            client.ensure_bank(bank_id)
            created = True
            state = client.get_bank_config(bank_id=bank_id)
        strategies = _strategies(state)
        if _has(strategies, STRUCTURED_STRATEGY, wanted):
            return "created" if created else "unchanged"
        strategies[STRUCTURED_STRATEGY] = {**(strategies.get(STRUCTURED_STRATEGY) or {}), **wanted}
        state = client.patch_bank_config({"retain_strategies": strategies}, bank_id=bank_id)
    except HindsightError as exc:
        raise BankNotPrepared(
            f"bank {bank_id!r}: {type(exc).__name__} "
            f"(HTTP {getattr(exc, 'status_code', None)})") from exc
    if not _has(_strategies(state), STRUCTURED_STRATEGY, wanted):
        raise BankNotPrepared(
            f"bank {bank_id!r}: {STRUCTURED_STRATEGY!r} strategy not in effect after update")
    return "created" if created else "updated"


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


#: Retain outcomes count settled documents; consolidation outcomes count
#: operations (a consolidation has no documents of its own).
_RETAIN_OUTCOME = {"completed": "confirmed", "failed": "failed",
                   "cancelled": "cancelled", "lost": "lost"}
_CONSOLIDATION_OUTCOME = {"completed": "consolidation_completed",
                          "failed": "consolidation_failed",
                          "cancelled": "consolidation_cancelled",
                          "lost": "consolidation_lost"}


def _record(out: dict[str, int], conn, bank_id: str, op: str, kind: str,
            status: str, now: str, detail: str = "") -> None:
    docs = _settle(conn, bank_id, op, status, now, detail)
    if kind == "consolidation":
        out[_CONSOLIDATION_OUTCOME[status]] += 1
    else:
        out[_RETAIN_OUTCOME[status]] += docs


def _outcome_counts() -> dict[str, int]:
    return {k: 0 for k in (*_RETAIN_OUTCOME.values(), *_CONSOLIDATION_OUTCOME.values())}


def _poll_in_flight(conn, cfg: ProjectionConfig, now_dt: datetime, now: str) -> dict[str, int]:
    """Drive every submitted operation of this bank (retain or consolidation)
    towards a terminal state.

    completed → documents confirmed; failed/partial → failed (retried within
    max_attempts_per_doc / max_consolidation_attempts); unknown to the server
    (404/not_found) → lost and retried; still running past
    operation_timeout_minutes → cancelled through the client's cancellation
    API.  A transient polling error leaves it be.
    """
    out = _outcome_counts()
    rows = conn.execute(
        "SELECT operation_id, submitted_at, kind FROM hindsight_operations"
        " WHERE bank_id=? AND status='submitted'", (cfg.bank_id,)).fetchall()
    for op, submitted_at, kind in rows:
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
            _record(out, conn, cfg.bank_id, op, kind, "cancelled", now, "timed out")
        elif state == _OP_DONE:
            _record(out, conn, cfg.bank_id, op, kind, "completed", now)
        elif state == "not_found":
            _record(out, conn, cfg.bank_id, op, kind, "lost", now, "unknown to server")
        else:
            _record(out, conn, cfg.bank_id, op, kind, "failed", now, str(state))
    return out


def _cancel_in_flight(conn, cfg: ProjectionConfig, now: str, why: str) -> dict[str, int]:
    """Cancel this bank's submitted operations (budget exhausted)."""
    out = _outcome_counts()
    for op, kind in conn.execute(
            "SELECT operation_id, kind FROM hindsight_operations"
            " WHERE bank_id=? AND status='submitted'", (cfg.bank_id,)).fetchall():
        try:
            cfg.client.cancel_operation(op, bank_id=cfg.bank_id)
        except Exception:
            continue
        _record(out, conn, cfg.bank_id, op, kind, "cancelled", now, why)
    return out


def _add(counts: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        counts[k] = counts.get(k, 0) + v


def _guard_stops(conn, cfg: ProjectionConfig, baseline, now: str, counts: dict) -> bool:
    """True when the usage guard forbids more work; on overspend this bank's
    in-flight operations (retains and consolidations) are cancelled."""
    from olo.m3.hindsight.budget import BudgetExceededError, StatsUnavailableError
    try:
        cfg.usage_guard.check(baseline)
    except BudgetExceededError:
        counts["budget_exceeded"] = 1
        _add(counts, _cancel_in_flight(conn, cfg, now, "usage budget exceeded"))
        return True
    except StatsUnavailableError:
        counts["stats_unavailable"] = 1
        return True
    return False


# ── Consolidation ─────────────────────────────────────────────────────────────

#: Tag carried by every prose document the stage projects, and the only scope
#: consolidation is asked to cover.  Ledger-state and structured signal
#: documents share entity tags with prose, so an entity scope would hand them
#: to the consolidation LLM; this tag never matches them.
PROSE_SCOPE_TAG = "olo:prose"
_CONSOLIDATION_SCOPES = [[PROSE_SCOPE_TAG]]


def _submitted(conn, bank_id: str, kind: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM hindsight_operations"
        " WHERE bank_id=? AND kind=? AND status='submitted'", (bank_id, kind)).fetchone()[0]


def _attempts_since_new_prose(conn, bank_id: str) -> int:
    """Consolidations triggered since the newest completed prose retain.

    Reaching the trigger again means prose memories are still pending, so each
    of these attempts — failed, cancelled, lost, or completed without draining
    them (the server reports a skipped job as completed) — was unproductive.
    New prose resets the count.  rowid is insertion order.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM hindsight_operations"
        " WHERE bank_id=? AND kind='consolidation' AND rowid > COALESCE("
        "   (SELECT MAX(rowid) FROM hindsight_operations WHERE bank_id=? AND kind='retain'"
        "      AND status='completed' AND prose_count > 0), 0)",
        (bank_id, bank_id)).fetchone()[0]


def _consolidations_in_window(conn, bank_id: str, window_start: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM hindsight_operations"
        " WHERE bank_id=? AND kind='consolidation' AND submitted_at >= ?",
        (bank_id, window_start)).fetchone()[0]


def _server_consolidation(client: Any, bank_id: str) -> Optional[str]:
    """A consolidation of this bank the server is still running, if any."""
    for status in ("pending", "processing"):
        for o in client.list_operations(limit=50, status=status, bank_id=bank_id).get("operations") or []:
            if (o.get("task_type") or o.get("operation_type")) == "consolidation":
                return str(o.get("id") or o.get("operation_id"))
    return None


def _prose_pending(cfg: ProjectionConfig, counts: dict, key: str = "consolidation_pending") -> Optional[int]:
    """Prose memories the server still has to consolidate (None: unknown)."""
    try:
        pending = cfg.client.count_memories(
            consolidation_state="pending", tags=[PROSE_SCOPE_TAG], bank_id=cfg.bank_id)
        failed = cfg.client.count_memories(
            consolidation_state="failed", tags=[PROSE_SCOPE_TAG], bank_id=cfg.bank_id)
    except Exception:
        counts["consolidation_unknown"] = 1
        return None
    counts[key] = pending
    counts["consolidation_failed_memories"] = failed
    return pending


def _record_consolidation(conn, cfg: ProjectionConfig, source: str, op: str, now: str,
                          status: str = "submitted", detail: str = "") -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR IGNORE INTO hindsight_operations (operation_id, bank_id, source,"
        " submitted_at, doc_count, prose_count, status, settled_at, detail, kind)"
        " VALUES (?,?,?,?,0,0,?,?,?,'consolidation')",
        (op, cfg.bank_id, source, now, status,
         now if status != "submitted" else None, detail or None))
    conn.execute("COMMIT")


def _consolidate(conn, cfg: ProjectionConfig, source: str, baseline, window_start: str,
                 now: str, counts: dict) -> None:
    """Trigger one prose-scoped consolidation when the server reports prose
    memories pending consolidation.

    Waits while any retain of the bank is in flight (its facts are not there
    yet) or a consolidation is running — one this database recorded, or one
    the server is running that a crash left unrecorded (adopted, not
    duplicated).  The server's pending count, not a completed retain or a
    completed consolidation operation, decides whether work remains.
    """
    if _submitted(conn, cfg.bank_id, "retain"):
        counts["consolidation_deferred"] = 1
        return
    if _submitted(conn, cfg.bank_id, "consolidation"):
        counts["consolidation_in_flight"] = 1
        return
    pending = _prose_pending(cfg, counts)
    if not pending:
        return
    lim = cfg.limits
    if _attempts_since_new_prose(conn, cfg.bank_id) >= lim.max_consolidation_attempts:
        counts["consolidation_retries_exhausted"] = 1
        return
    if _consolidations_in_window(conn, cfg.bank_id, window_start) >= lim.max_consolidations_per_window:
        counts["consolidation_capped"] = 1
        return
    try:
        running = _server_consolidation(cfg.client, cfg.bank_id)
    except Exception:
        counts["consolidation_unknown"] = 1
        return
    if running is not None:
        _record_consolidation(conn, cfg, source, running, now, detail="adopted")
        counts["consolidation_in_flight"] = 1
        return
    if _guard_stops(conn, cfg, baseline, now, counts):
        return
    try:
        reply = cfg.client.trigger_consolidation(
            observation_scopes=_CONSOLIDATION_SCOPES, bank_id=cfg.bank_id) or {}
        op = str(reply["operation_id"])
    except Exception as exc:
        # Recorded as a failed attempt so it counts towards the retry bound.
        _record_consolidation(conn, cfg, source, f"consolidation-not-submitted:{uuid.uuid4()}",
                              now, "failed", f"submit failed: {type(exc).__name__}")
        counts["consolidation_failed"] += 1
        return
    _record_consolidation(conn, cfg, source, op, now)
    counts["consolidation_submitted"] = 1


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

    Order: bank binding check → bank preparation (nothing is submitted to a
    bank that is not prepared) → settle submitted operations → persisted
    budget window → diff desired documents against the outbox → submit in
    batches while the run/window document caps and the usage guard allow →
    settle → consolidation when the server reports pending memories.
    Idempotent and resumable; never resubmits an in-flight document.
    """
    from olo.m3.hindsight.budget import StatsUnavailableError

    now_dt = clock()
    now = format_utc(now_dt)
    bind_bank(conn, cfg.bank_id, now)
    counts: dict[str, Any] = {
        "submitted": 0, "unchanged": 0, "deferred": 0, "retries_exhausted": 0,
        "budget_exceeded": 0, "stats_unavailable": 0, "bank_unprepared": 0,
        "consolidation_submitted": 0, **_outcome_counts()}
    _add(counts, _poll_in_flight(conn, cfg, now_dt, now))
    try:
        counts["bank_prepared"] = prepare_bank(cfg.client, cfg.bank_id)
    except BankNotPrepared:
        counts["bank_unprepared"] = 1        # fail closed: nothing submitted
        return counts

    try:
        window_start, baseline = _window_baseline(conn, cfg, now_dt, now)
    except StatsUnavailableError:
        counts["stats_unavailable"] = 1      # fail closed: nothing submitted
        return counts

    todo = _changed_documents(conn, cfg, source, counts)
    stopped = _submit(conn, cfg, source, todo, baseline, window_start, now, counts, _fault)
    _add(counts, _poll_in_flight(conn, cfg, now_dt, now))
    if stopped:
        return counts
    # Work still running server-side is cancelled if spend went over.
    in_flight = _submitted(conn, cfg.bank_id, "retain") + _submitted(conn, cfg.bank_id, "consolidation")
    if in_flight and _guard_stops(conn, cfg, baseline, now, counts):
        return counts
    _consolidate(conn, cfg, source, baseline, window_start, now, counts)
    if counts["consolidation_submitted"]:
        settled = _poll_in_flight(conn, cfg, now_dt, now)
        _add(counts, settled)
        if settled["consolidation_completed"]:
            # Completed is not proof: re-read what the server still has pending.
            _prose_pending(cfg, counts, "consolidation_pending_after")
    return counts


def _changed_documents(conn, cfg: ProjectionConfig, source: str,
                       counts: dict) -> list[tuple[dict, str]]:
    """Desired documents that are neither in flight, confirmed unchanged,
    nor out of retry attempts."""
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
    return todo


def _next_batch(queue, cfg: ProjectionConfig, used: list[int]):
    """Split *queue* into (batch, rest) within batch size and run/window caps.
    ``used`` = [run_docs, run_prose, win_docs, win_prose]."""
    lim = cfg.limits
    run_docs, run_prose, win_docs, win_prose = used
    batch: list[tuple[dict, str]] = []
    rest: list[tuple[dict, str]] = []
    for item, fp in queue:
        fits = (len(batch) < cfg.batch_size
                and run_docs + len(batch) < lim.max_docs_per_run
                and win_docs + len(batch) < lim.max_docs_per_window)
        if fits and _is_prose(item):
            p = sum(1 for i, _ in batch if _is_prose(i))
            fits = (run_prose + p < lim.max_prose_docs_per_run
                    and win_prose + p < lim.max_prose_docs_per_window)
        (batch if fits else rest).append((item, fp))
    return batch, rest


def _submit(conn, cfg: ProjectionConfig, source: str, todo, baseline, window_start: str,
            now: str, counts: dict, _fault) -> bool:
    """Submit *todo* in batches; True when the usage guard stopped the run."""
    win_docs, win_prose = _window_usage(conn, cfg.bank_id, window_start)
    used = [0, 0, win_docs, win_prose]
    queue = list(todo)
    stopped = False
    while queue:
        if _guard_stops(conn, cfg, baseline, now, counts):
            stopped = True
            break
        batch, rest = _next_batch(queue, cfg, used)
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
        used = [used[0] + len(batch), used[1] + prose_n,
                used[2] + len(batch), used[3] + prose_n]
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
    return stopped
