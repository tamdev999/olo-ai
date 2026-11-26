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
  Email prose is retained as its author's own text only (quoted replies and
  forwarded history omitted, ``text_scope=authored``); the full original
  stays immutable in olo.db.
* Ledger-state documents (``ledger:<commitment_id>:v<version>``) — one per
  confirmed ledger version: status and date as of that version, the version
  it supersedes, and its supporting documents.  The ledger's current version
  (``get_current_state``) is marked ``CURRENT`` and tagged
  ``ledger:current``; every other version is marked ``SUPERSEDED`` (naming
  the current state) and tagged ``ledger:superseded``, so a new version
  re-renders the one it supersedes.  Pending proposals are not state changes
  and get no state document.  Customer-facing answers come from
  ``olo.current_state``, which reads the current state from the ledger and
  labels recalled memories against it.

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
the ``usage_guard`` budget are reached.  Spend is the server-reported LLM
usage of each hour of the window, recorded in olo.db at its largest value
(``hindsight_usage_hours``): Hindsight ages trace rows out after a day, so a
cumulative total minus a stored baseline lost spend; the guard's stop cancels this bank's in-flight
operations.  These are client-side controls, not a server-enforced monetary
cap: work already accepted by Hindsight runs until it completes or is
cancelled.  Without readable usage statistics nothing is submitted.  Each
submission holds one retain strategy: Hindsight 1.2.1 rejects an
operation_id whose items resolve to several strategies (HTTP 400).

Bank preparation (S5 preflight): before anything is retained the bank is
created if missing, Olo's ``structured-no-llm`` retain strategy (chunks
extraction, observations off) is merged into its ``retain_strategies``, and
the bank's ``enable_auto_consolidation`` is turned off (Hindsight's
automatic consolidations are bank-wide and on by default).  Hindsight's
config PATCH replaces a top-level key wholesale, so the current strategies
are read and only Olo's entry is added or corrected; every other bank setting
is left alone.  Repeating it is a read and no write.  A bank that cannot be
prepared receives nothing: ledger-state documents would otherwise fall back
to LLM extraction.

Consolidation: auto-consolidation is off, so observations form only when the
stage asks for them.  Prose documents with a verified customer carry the
``olo:prose`` tag and consolidation is scoped to it.  The request scope only
selects candidates: each memory's own observation_scopes (per customer,
per month and cross-month) still decide batching, related-observation lookup
and the observations written — verified live with two customers in one bank
(docs/olo/m3/S5_PREFLIGHT_EVIDENCE.md).  Unlinked prose is not tagged: with
no customer scope Hindsight would pool every unverified sender of a month
into shared observations.  Hindsight 1.2.1 counts
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

from olo.signal_content import EMAIL_KINDS, PROSE_KINDS, authored_text, load_content
from olo.signals import format_utc

__all__ = [
    "LEDGER_CURRENT_TAG",
    "LEDGER_SUPERSEDED_TAG",
    "BankNotPrepared",
    "ProjectionConfig",
    "ProjectionLimits",
    "ProjectionRefused",
    "bind_bank",
    "desired_documents",
    "olo_strategies",
    "prepare_bank",
    "reconcile_documents",
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
        extra = {}
        if row["kind"] in EMAIL_KINDS:
            # Retained prose is the author's own text only: quoted replies and
            # forwarded history are other people's earlier statements, which
            # Hindsight's extraction would otherwise record as facts of this
            # message.  The full original stays immutable in olo.db
            # (content_sha256 names its blob); the commitment extractor reads
            # the same authored text.
            own = authored_text(row["kind"], content).rstrip()
            extra["text_scope"] = "authored"
            if own != content.rstrip():
                extra["quoted_history"] = "omitted"
            content = own
        if not content.strip():
            continue
        contact = row["contact_id"] if row["identity_status"] == "resolved" else ""
        if row["commitment_id"]:
            extra.update({"commitment_id": row["commitment_id"],
                          "commitment_version": str(row["commitment_version"] or "")})
        item = build_signal_item(
            row, payload, content, sha,
            source=source, contact_id=contact or "", contact_info=contact_info,
            entity_cache=entity_cache, production=True, extra_metadata=extra,
        )
        # Only prose with a verified customer scope is consolidated.  Unlinked
        # prose has no observation_scopes, so Hindsight would consolidate it in
        # "combined" mode over its own tags ({ym:YYYY-MM}) — pooling every
        # unverified sender of a month into shared observations.
        if _is_prose(item) and item.get("observation_scopes"):
            item["tags"] = [*item["tags"], PROSE_SCOPE_TAG]
        items.append(item)
    return items


# ── Ledger-state documents ────────────────────────────────────────────────────

#: Tags marking which ledger-state document holds a commitment's current
#: confirmed state.  Exactly one document per commitment carries
#: LEDGER_CURRENT_TAG; every earlier confirmed version carries
#: LEDGER_SUPERSEDED_TAG and says so in its first line.  Both are derived from
#: olo.db on every run, so a new version re-renders (and Hindsight replaces in
#: place) the document it supersedes.
LEDGER_CURRENT_TAG = "ledger:current"
LEDGER_SUPERSEDED_TAG = "ledger:superseded"
#: Render contract of ledger-state documents (part of every fingerprint).
#:   1 — S5 part 2;  2 — current / superseded marking.
LEDGER_RENDER_VERSION = "2"


def _ledger_documents(conn: sqlite3.Connection) -> list[dict]:
    from olo.ledger import (
        CANCEL_ACTION,
        STATUS_CANCELLED,
        STATUS_SCHEDULED,
        get_current_state,
        utc_instant,
    )
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
        if not versions:
            continue
        # The ledger's own reading of "current" (latest confirmed by UTC
        # instant) decides which document is current — never this ordering.
        current = get_current_state(conn, cid) or {}
        current_version = current.get("version")
        current_line = (
            f"{current.get('status')}"
            + (f", date {current['scheduled_date']}" if current.get("scheduled_date")
               else ", no scheduled date")
        )
        ordered = sorted(versions, key=lambda v: (utc_instant(v[5]) or _EARLIEST, v[0]))
        previous = None
        for v in ordered:
            version, action, actor, confirmed, prior, occurred, doc, notes = tuple(v)
            cancelled = action == CANCEL_ACTION
            status = STATUS_CANCELLED if cancelled else STATUS_SCHEDULED
            date = None if cancelled else confirmed
            is_current = version == current_version
            supporting = [d for d in (notes, doc) if d]
            supersedes = (
                f"supersedes version {previous[0]} (date {previous[3] or 'none'})"
                if previous is not None else "first confirmed version"
            )
            role = (
                f"CURRENT: version {version} is the latest confirmed state of this commitment."
                if is_current else
                f"SUPERSEDED: version {version} is history, not the current state. "
                f"Current confirmed state (version {current_version}): {current_line}."
            )
            content = (
                f"{role}\n"
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
                "tags": [ent_tag, _ym_tag(occurred), "kind:ledger.state",
                         LEDGER_CURRENT_TAG if is_current else LEDGER_SUPERSEDED_TAG],
                "observation_scopes": [[ent_tag, "ym:*"], [ent_tag]],
                "metadata": {
                    "kind": "ledger.state",
                    "commitment_id": cid,
                    "version": str(version),
                    "status": status,
                    "scheduled_date": date or "",
                    "superseded_version": str(previous[0]) if previous is not None else "",
                    "ledger_role": "current" if is_current else "superseded",
                    "current_version": str(current_version or ""),
                    "supporting_documents": ",".join(supporting),
                    "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "render_version": LEDGER_RENDER_VERSION,
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

#: Settings of Olo's prose strategy: Hindsight's own resolved defaults
#: (concise LLM extraction, observations on), stated explicitly.  Before S5
#: remediation the strategy name was never registered and Hindsight 1.2.1
#: fell back to the resolved config with a warning ("Unknown retain strategy
#: 'prose'"); registering it keeps that behaviour and makes it verifiable.
PROSE_STRATEGY_SETTINGS = {"retain_extraction_mode": "concise", "enable_observations": True}


def olo_strategies() -> dict[str, dict]:
    """Every retain strategy Olo submits, with the settings it requires."""
    from olo.m3.feeder import _BANK_CONFIG_OVERRIDES, PROSE_STRATEGY, STRUCTURED_STRATEGY

    return {STRUCTURED_STRATEGY: dict(_BANK_CONFIG_OVERRIDES["retain_strategies"][STRUCTURED_STRATEGY]),
            PROSE_STRATEGY: dict(PROSE_STRATEGY_SETTINGS)}


def _strategies(state: dict) -> dict:
    return dict((state.get("config") or {}).get("retain_strategies") or {})


def _has(strategies: dict, name: str, wanted: dict) -> bool:
    got = strategies.get(name)
    return isinstance(got, dict) and all(got.get(k) == v for k, v in wanted.items())


def _prepared(state: dict, wanted: dict[str, dict]) -> bool:
    strategies = _strategies(state)
    return (all(_has(strategies, name, w) for name, w in wanted.items())
            and (state.get("config") or {}).get("enable_auto_consolidation") is False)


def prepare_bank(client: Any, bank_id: str) -> str:
    """Make *bank_id* ready for Olo documents; returns created/updated/unchanged.

    Creates the bank when the server does not know it, then ensures:

    * the resolved ``retain_strategies`` contain every strategy Olo submits
      (``olo_strategies``: structured and prose) with the required settings —
      the PATCH carries the current resolved strategies plus Olo's entries,
      because Hindsight replaces a top-level config key wholesale.  Hindsight
      1.2.1 accepts an unregistered strategy name with only a warning and
      falls back to the resolved config; Olo never relies on that fallback;
    * ``enable_auto_consolidation`` is off for the bank.  Every automatic
      consolidation Hindsight 1.2.1 queues (after a retain, a document update
      or delete) is bank-wide, and its default is on: it would hand
      ledger-state and structured memories — which carry customer observation
      scopes — to the consolidation LLM.  The stage's own prose-scoped
      consolidation is the only one Olo runs.

    No other key is sent.  Raises BankNotPrepared when the server refuses or
    the result cannot be verified.
    """
    from olo.m3.hindsight.client import HindsightAPIError, HindsightError

    wanted = olo_strategies()
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
        if _prepared(state, wanted):
            return "created" if created else "unchanged"
        updates: dict[str, Any] = {}
        strategies = _strategies(state)
        for name, settings in wanted.items():
            if not _has(strategies, name, settings):
                strategies[name] = {**(strategies.get(name) or {}), **settings}
                updates["retain_strategies"] = strategies
        if (state.get("config") or {}).get("enable_auto_consolidation") is not False:
            updates["enable_auto_consolidation"] = False
        state = client.patch_bank_config(updates, bank_id=bank_id)
    except HindsightError as exc:
        raise BankNotPrepared(
            f"bank {bank_id!r}: {type(exc).__name__} "
            f"(HTTP {getattr(exc, 'status_code', None)}): {str(exc)[:120]}") from exc
    if not _prepared(state, wanted):
        raise BankNotPrepared(
            f"bank {bank_id!r}: Olo retain strategies {sorted(wanted)} or disabled "
            "auto-consolidation not in effect after update")
    return "created" if created else "updated"


# ── Budget window (persisted) ─────────────────────────────────────────────────

def _window(conn, cfg: ProjectionConfig, now_dt: datetime, now: str) -> str:
    """Start of this bank's current budget window (persisted in olo.db, so a
    restart never opens a new window).  Opening one first proves the usage
    statistics are readable (fails closed with StatsUnavailableError)."""
    row = conn.execute(
        "SELECT window_start FROM hindsight_budget_window WHERE bank_id=?",
        (cfg.bank_id,)).fetchone()
    if row is not None:
        start = datetime.fromisoformat(row[0])
        if now_dt - start < timedelta(hours=cfg.limits.window_hours):
            return row[0]
    _observe_usage(conn, cfg, now)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO hindsight_budget_window"
        " (bank_id, window_start, baseline_input_tokens, baseline_output_tokens)"
        " VALUES (?,?,0,0) ON CONFLICT(bank_id) DO UPDATE SET"
        "  window_start=excluded.window_start, baseline_input_tokens=0,"
        "  baseline_output_tokens=0",
        (cfg.bank_id, now))
    conn.execute("COMMIT")
    return now


def _hour_key(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00Z")


def _observe_usage(conn, cfg: ProjectionConfig, now: str) -> None:
    """Record every hour bucket the server reports, keeping each hour's
    largest value: Hindsight drops trace rows after a day, Olo does not."""
    hours = cfg.usage_guard.hourly_usage()
    conn.execute("BEGIN IMMEDIATE")
    for hour, snap in hours:
        conn.execute(
            "INSERT INTO hindsight_usage_hours (bank_id, hour, input_tokens, output_tokens, seen_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(bank_id, hour) DO UPDATE SET"
            "  input_tokens=MAX(input_tokens, excluded.input_tokens),"
            "  output_tokens=MAX(output_tokens, excluded.output_tokens),"
            "  seen_at=excluded.seen_at",
            (cfg.bank_id, _hour_key(hour), snap.input_tokens, snap.output_tokens, now))
    conn.execute("COMMIT")


def window_spend(conn, bank_id: str, window_start: str):
    """Tokens recorded for the hours overlapping the window (the partial
    first hour included: an upper bound, never an under-count)."""
    from olo.m3.hindsight.budget import TokenSnapshot

    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0)"
        " FROM hindsight_usage_hours WHERE bank_id=? AND hour >= ?",
        (bank_id, _hour_key(window_start))).fetchone()
    return TokenSnapshot(int(row[0]), int(row[1]))


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
    return {k: 0 for k in (*_RETAIN_OUTCOME.values(), *_CONSOLIDATION_OUTCOME.values(),
                           "cancelled_persisted")}


def reconcile_documents(client: Any, bank_id: str, document_ids: list[str]) -> dict:
    """What the server actually holds for *document_ids*: {persisted: {id:
    memory units}, absent: [ids], unknown: [ids]}.

    Hindsight 1.2.1 cancellation is cooperative: the operation row is flipped
    to 'cancelled' and its worker stops at its next checkpoint, after the
    batch or document it is on.  Nothing is rolled back, so a cancelled
    retain can leave documents and memories behind.  This reads them back
    rather than inferring anything from the operation status.
    """
    from olo.m3.hindsight.client import HindsightAPIError

    out: dict[str, Any] = {"persisted": {}, "absent": [], "unknown": []}
    for doc in document_ids:
        try:
            got = client.get_document(doc, bank_id=bank_id) or {}
            out["persisted"][doc] = int(got.get("memory_unit_count") or 0)
        except HindsightAPIError as exc:
            (out["absent"] if exc.status_code == 404 else out["unknown"]).append(doc)
        except Exception:
            out["unknown"].append(doc)
    return out


def _record_cancelled(out: dict[str, int], conn, cfg: ProjectionConfig, op: str, kind: str,
                      now: str, why: str) -> None:
    """Record a cancellation together with what it did NOT undo.

    The documents stay 'failed' (resubmission replaces them in place), and the
    operation's detail names every document the server still holds, so the
    status never reads as a rollback.
    """
    _record(out, conn, cfg.bank_id, op, kind, "cancelled", now, why)
    if kind == "consolidation":
        _note(conn, cfg.bank_id, op, f"{why}; cooperative cancel: observations written before "
                                     "the stop are kept")
        return
    docs = [r[0] for r in conn.execute(
        "SELECT document_id FROM hindsight_operation_docs WHERE operation_id=? ORDER BY document_id",
        (op,))]
    seen = reconcile_documents(cfg.client, cfg.bank_id, docs)
    out["cancelled_persisted"] += len(seen["persisted"])
    _note(conn, cfg.bank_id, op, (
        f"{why}; cooperative cancel, not a rollback: persisted on server "
        f"{seen['persisted'] or 'none'}; absent {seen['absent'] or 'none'}; "
        f"unknown {seen['unknown'] or 'none'}"))


def _note(conn, bank_id: str, op: str, detail: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE hindsight_operations SET detail=? WHERE operation_id=? AND bank_id=?",
                 (detail[:500], op, bank_id))
    conn.execute("COMMIT")


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
            _record_cancelled(out, conn, cfg, op, kind, now, "timed out")
        elif state == _OP_DONE:
            _record(out, conn, cfg.bank_id, op, kind, "completed", now)
        elif state == "cancelled":
            _record_cancelled(out, conn, cfg, op, kind, now, "cancelled on the server")
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
        _record_cancelled(out, conn, cfg, op, kind, now, why)
    return out


def _add(counts: dict[str, int], more: dict[str, int]) -> None:
    for k, v in more.items():
        counts[k] = counts.get(k, 0) + v


def _guard_stops(conn, cfg: ProjectionConfig, window_start: str, now: str, counts: dict) -> bool:
    """True when the usage budget forbids more work; on overspend this bank's
    in-flight operations (retains and consolidations) are cancelled.

    Spend is the window's recorded hour buckets (``window_spend``), refreshed
    from the server first, so it cannot shrink when the server ages trace
    rows out.
    """
    from olo.m3.hindsight.budget import StatsUnavailableError
    try:
        _observe_usage(conn, cfg, now)
    except StatsUnavailableError:
        counts["stats_unavailable"] = 1
        return True
    spent = window_spend(conn, cfg.bank_id, window_start).cost_usd()
    counts["window_spend_usd"] = round(spent, 6)
    if spent >= cfg.usage_guard.budget_usd:
        counts["budget_exceeded"] = 1
        _add(counts, _cancel_in_flight(conn, cfg, now, "usage budget exceeded"))
        return True
    return False


# ── Consolidation ─────────────────────────────────────────────────────────────

#: Tag carried by every prose document with a verified customer, and the only
#: scope consolidation is asked to cover.  Ledger-state and structured signal
#: documents share entity tags with prose, so an entity scope would hand them
#: to the consolidation LLM; this tag never matches them.  It selects
#: candidates only: it is never part of a memory's observation_scopes.
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


def _consolidate(conn, cfg: ProjectionConfig, source: str, window_start: str,
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
    if _guard_stops(conn, cfg, window_start, now, counts):
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
        "unregistered_strategy": 0, "consolidation_submitted": 0, **_outcome_counts()}
    _add(counts, _poll_in_flight(conn, cfg, now_dt, now))
    try:
        counts["bank_prepared"] = prepare_bank(cfg.client, cfg.bank_id)
    except BankNotPrepared as exc:
        counts["bank_unprepared"] = 1        # fail closed: nothing submitted
        counts["bank_unprepared_reason"] = str(exc)[:200]
        return counts

    try:
        window_start = _window(conn, cfg, now_dt, now)
    except StatsUnavailableError:
        counts["stats_unavailable"] = 1      # fail closed: nothing submitted
        return counts

    todo = _changed_documents(conn, cfg, source, counts)
    stopped = _submit(conn, cfg, source, todo, window_start, now, counts, _fault)
    _add(counts, _poll_in_flight(conn, cfg, now_dt, now))
    if stopped:
        return counts
    # Work still running server-side is cancelled if spend went over.
    in_flight = _submitted(conn, cfg.bank_id, "retain") + _submitted(conn, cfg.bank_id, "consolidation")
    if in_flight and _guard_stops(conn, cfg, window_start, now, counts):
        return counts
    _consolidate(conn, cfg, source, window_start, now, counts)
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
    registered = olo_strategies()
    todo: list[tuple[dict, str]] = []
    for item in desired_documents(conn, source, await_commitment=cfg.await_commitment):
        if item.get("strategy") not in registered:
            # Fail closed: Hindsight would silently fall back to its default.
            counts["unregistered_strategy"] += 1
            continue
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
    ``used`` = [run_docs, run_prose, win_docs, win_prose].

    A batch holds one retain strategy: Hindsight 1.2.1 rejects a retain with
    an operation_id whose items resolve to more than one strategy (HTTP 400).
    """
    lim = cfg.limits
    run_docs, run_prose, win_docs, win_prose = used
    strategy = queue[0][0].get("strategy") if queue else None
    batch: list[tuple[dict, str]] = []
    rest: list[tuple[dict, str]] = []
    for item, fp in queue:
        fits = (item.get("strategy") == strategy
                and len(batch) < cfg.batch_size
                and run_docs + len(batch) < lim.max_docs_per_run
                and win_docs + len(batch) < lim.max_docs_per_window)
        if fits and _is_prose(item):
            p = sum(1 for i, _ in batch if _is_prose(i))
            fits = (run_prose + p < lim.max_prose_docs_per_run
                    and win_prose + p < lim.max_prose_docs_per_window)
        (batch if fits else rest).append((item, fp))
    return batch, rest


def _submit(conn, cfg: ProjectionConfig, source: str, todo, window_start: str,
            now: str, counts: dict, _fault) -> bool:
    """Submit *todo* in batches; True when the usage guard stopped the run."""
    win_docs, win_prose = _window_usage(conn, cfg.bank_id, window_start)
    used = [0, 0, win_docs, win_prose]
    queue = list(todo)
    stopped = False
    while queue:
        if _guard_stops(conn, cfg, window_start, now, counts):
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
