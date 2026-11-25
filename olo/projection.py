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
document under the same document_id.  Rows move pending → confirmed/failed;
an in-flight operation is polled, never resubmitted; failed rows are retried.
Scope configuration is unchanged: items carry the same observation scopes as
the benchmark feed.  Isolation: one olo.db and one bank per business.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from olo.signal_content import PROSE_KINDS, load_content
from olo.signals import format_utc

__all__ = ["ProjectionConfig", "desired_documents", "run_projection_stage"]

_IDENTITY_SETTLED = ("resolved", "unresolved", "ambiguous", "skipped")
_COMMITMENT_UNSETTLED = ("pending", "failed")
_OP_DONE = "completed"
#: Operation states that mean "still running": keep polling, never resubmit.
_OP_IN_FLIGHT = ("pending", "processing", "running", "queued")
_EARLIEST = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ProjectionConfig:
    """Enables the projection stage in run_source.

    ``client`` needs ``retain(items, *, operation_id, async_, bank_id)`` and
    ``get_operation_status(operation_id, bank_id=...)`` (HindsightClient).
    ``await_commitment``: hold prose signals until their commitment outcome is
    settled (set when the commitment stage runs), so each is projected with
    its commitment link instead of twice.
    """
    client: Any
    bank_id: str
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


# ── Outbox ────────────────────────────────────────────────────────────────────

def _poll_in_flight(conn, cfg: ProjectionConfig, now: str) -> dict[str, int]:
    """Settle rows whose retain operation was submitted by an earlier run."""
    rows = conn.execute(
        "SELECT DISTINCT operation_id FROM hindsight_projection"
        " WHERE bank_id=? AND status='pending' AND operation_id != ''",
        (cfg.bank_id,),
    ).fetchall()
    out = {"confirmed": 0, "failed": 0}
    for (op,) in rows:
        try:
            state = (cfg.client.get_operation_status(op, bank_id=cfg.bank_id) or {}).get("status")
        except Exception as exc:
            if getattr(exc, "status_code", None) != 404:
                continue               # transient: poll again next run
            state = "not_found"        # never reached Hindsight: resubmit
        if state in _OP_IN_FLIGHT:
            continue
        if state == _OP_DONE:
            out["confirmed"] += conn.execute(
                "UPDATE hindsight_projection SET status='confirmed', confirmed_at=?"
                " WHERE bank_id=? AND operation_id=? AND status='pending'",
                (now, cfg.bank_id, op)).rowcount
        else:                          # failed, partial or unknown: retry
            out["failed"] += conn.execute(
                "UPDATE hindsight_projection SET status='failed'"
                " WHERE bank_id=? AND operation_id=? AND status='pending'",
                (cfg.bank_id, op)).rowcount
    return out


def run_projection_stage(
    conn: sqlite3.Connection,
    source: str,
    cfg: ProjectionConfig,
    clock: Callable[[], datetime],
    *,
    _fault: Optional[Callable[[str], None]] = None,
) -> dict[str, int]:
    """Bring Hindsight in line with olo.db for *source*; idempotent, resumable."""
    now = format_utc(clock())
    polled = _poll_in_flight(conn, cfg, now)
    existing = {
        r[0]: (r[1], r[2], r[3]) for r in conn.execute(
            "SELECT signal_id, content_sha256, status, operation_id"
            " FROM hindsight_projection WHERE bank_id=?", (cfg.bank_id,))
    }
    todo: list[tuple[dict, str]] = []
    desired = desired_documents(conn, source, await_commitment=cfg.await_commitment)
    for item in desired:
        fp = _fingerprint(item)
        prior = existing.get(item["document_id"])
        if prior is not None and prior[0] == fp and (
            prior[1] == "confirmed" or (prior[1] == "pending" and prior[2])
        ):
            continue                   # already projected, or in flight
        todo.append((item, fp))

    counts = {"submitted": 0, "confirmed": polled["confirmed"],
              "failed": polled["failed"], "unchanged": len(desired) - len(todo)}
    for start in range(0, len(todo), cfg.batch_size):
        batch = todo[start:start + cfg.batch_size]
        op = str(uuid.uuid5(uuid.NAMESPACE_URL, f"olo-projection:{cfg.bank_id}:{now}:" + ",".join(
            f"{i['document_id']}={fp}" for i, fp in batch)))
        conn.execute("BEGIN IMMEDIATE")
        for item, fp in batch:
            conn.execute(
                "INSERT INTO hindsight_projection"
                " (signal_id, bank_id, content_sha256, operation_id, status)"
                " VALUES (?,?,?,?,'pending')"
                " ON CONFLICT(signal_id, bank_id) DO UPDATE SET"
                "  content_sha256=excluded.content_sha256, operation_id=excluded.operation_id,"
                "  status='pending', confirmed_at=NULL",
                (item["document_id"], cfg.bank_id, fp, op),
            )
        conn.execute("COMMIT")
        if _fault:
            _fault("after_projection_enqueue")
        try:
            cfg.client.retain([i for i, _ in batch], operation_id=op,
                              async_=True, bank_id=cfg.bank_id)
        except Exception:
            conn.execute(
                "UPDATE hindsight_projection SET status='failed'"
                " WHERE bank_id=? AND operation_id=?", (cfg.bank_id, op))
            counts["failed"] += len(batch)
            continue
        counts["submitted"] += len(batch)
        settled = _poll_in_flight(conn, cfg, now)
        counts["confirmed"] += settled["confirmed"]
        counts["failed"] += settled["failed"]
    return counts
