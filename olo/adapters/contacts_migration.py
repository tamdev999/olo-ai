"""olo/adapters/contacts_migration.py -- Durable, resumable identity migration.

Repairs legacy entity links in Hindsight by mapping old ID formats to
canonical CRM-assigned contact IDs.

Detected legacy formats
-----------------------
  cid_{sha256(email)[:20]}  contacts.make_contact_id() -- pre-Defect-A
  ent_{sha256(email)[:20]}  entity_extractor.entity_id_from_email() -- pre-C1
  ent_{ULID}                generator ENTITIES list -- pre-C1 CRM model

State machine (per document)
-----------------------------
  planned  -- SQLite record created; nothing else done
  aliased  -- SQLite contact-ID rename committed (cid_email_derived only)
  retained -- Hindsight retain called; operation_id committed
  verified -- Hindsight confirmed correct entity link
  failed   -- non-recoverable error (ambiguous mapping, verification mismatch)

Durability guarantee
--------------------
SQLite commits happen BEFORE each Hindsight call.  A failed or interrupted
retain leaves a recoverable 'retained' or 'planned' state with the
operation_id persisted so the same UUID can be supplied again (idempotent
retain semantics via Hindsight operation_id).

A partial failure is reported as *incomplete and recoverable*, never as an
atomic rollback of external writes.  Do NOT delete or recreate the live bank
as a migration shortcut.

The existing execute_migration() in contacts.py wraps SQLite and Hindsight
calls in a single SQLite transaction and incorrectly claims atomicity.  Use
plan_identity_migration / step_retain / step_verify (or resume_migration)
instead.

Usage
-----
  plan  = plan_identity_migration(run_id, conn, crm_contacts, hindsight_docs)
  step_alias(run_id, conn)
  step_retain(run_id, conn, build_item_fn, retain_fn)
  step_verify(run_id, conn, get_document_fn)
  # or resume from any interrupted point:
  resume_migration(run_id, conn, build_item_fn, retain_fn, get_document_fn)
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

_log = logging.getLogger(__name__)

_HASH_LEN = 20  # sha256[:20] used by both make_contact_id and entity_id_from_email


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LegacyMigrationAmbiguous(Exception):
    """Raised when an old entity link maps to multiple canonical contacts."""


class LegacyMigrationError(Exception):
    """General migration error (run not found, bad state, etc.)."""


# ---------------------------------------------------------------------------
# Legacy-format detection
# ---------------------------------------------------------------------------

def _is_cid_email_derived(entity_text: str, conn: sqlite3.Connection) -> bool:
    """Return True when entity_text is a cid_ derived from one of its emails."""
    if not entity_text.startswith("cid_"):
        return False
    if len(entity_text) != 4 + _HASH_LEN:
        return False
    try:
        rows = conn.execute(
            "SELECT identifier_value FROM contact_identifiers"
            " WHERE contact_id = ? AND identifier_type = 'email'",
            (entity_text,),
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    from olo.adapters.contacts import make_contact_id
    return any(make_contact_id(r[0]) == entity_text for r in rows)


def _is_ent_email_derived(entity_text: str, conn: sqlite3.Connection) -> bool:
    """Return True when entity_text is an ent_ derived from an email address."""
    if not entity_text.startswith("ent_"):
        return False
    if len(entity_text) != 4 + _HASH_LEN:
        return False
    from olo.adapters.entity_extractor import entity_id_from_email
    try:
        rows = conn.execute(
            "SELECT DISTINCT identifier_value FROM contact_identifiers"
            " WHERE identifier_type = 'email'",
        ).fetchall()
    except sqlite3.OperationalError:
        return False
    return any(entity_id_from_email(r[0]) == entity_text for r in rows)


def detect_legacy_format(entity_text: str, conn: sqlite3.Connection) -> Optional[str]:
    """Return the legacy format name, or None if entity_text is canonical.

    Returns:
        'cid_email_derived' | 'ent_email_derived' | 'ent_ulid' | None
    """
    if not entity_text:
        return None
    if entity_text.startswith("ent_"):
        if _is_ent_email_derived(entity_text, conn):
            return "ent_email_derived"
        return "ent_ulid"
    if entity_text.startswith("cid_"):
        if _is_cid_email_derived(entity_text, conn):
            return "cid_email_derived"
        return None  # canonical (fixture-key or uuid4-based)
    return None


# ---------------------------------------------------------------------------
# Entity-to-email reverse map (for ent_ IDs without a contacts-table entry)
# ---------------------------------------------------------------------------

def _build_entity_email_map(conn: sqlite3.Connection) -> dict:
    """Return ent_... -> [email, ...] from signal payload email fields.

    Scans the signals table to find which emails appear alongside each
    ent_... entity_id, so ent_... IDs can be resolved via contact_identifiers.

    Never relies on generator answer-key tags -- only signal payload emails.
    """
    result: dict = {}
    try:
        rows = conn.execute(
            "SELECT external_id, kind, payload FROM signals"
        ).fetchall()
    except sqlite3.OperationalError:
        return result

    for (_sig_id, kind, payload_json) in rows:
        try:
            payload = json.loads(payload_json)
        except (json.JSONDecodeError, TypeError):
            continue
        entity_id = payload.get("entity_id", "")
        if not entity_id.startswith("ent_"):
            continue

        emails: list = []
        hdr = payload.get("headers") or {}
        if kind == "email.received":
            candidates = payload.get("from") or hdr.get("from") or []
        elif kind == "email.sent":
            candidates = payload.get("to") or hdr.get("to") or []
        elif kind == "email.content":
            direction = payload.get("direction", "received")
            candidates = (
                payload.get("to") if direction == "sent"
                else payload.get("from") or []
            )
        elif kind.startswith("calendar."):
            org = (payload.get("organizer_email") or "").strip().lower()
            if org and "@" in org and not payload.get("organizer_self"):
                emails.append(org)
            candidates = []
        else:
            candidates = []

        for addr in (candidates or []):
            if isinstance(addr, dict):
                e = (addr.get("email") or "").strip().lower()
                if e and "@" in e:
                    emails.append(e)

        for e in emails:
            result.setdefault(entity_id, [])
            if e not in result[entity_id]:
                result[entity_id].append(e)

    return result


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _resolve_to_canonical(
    old_link: str,
    legacy_format: str,
    identifier_map: dict,
    entity_email_map: dict,
    crm_contacts: list,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[str]:
    """Map an old entity link to its canonical cid_ from the CRM fixture.

    Uses authoritative crm_contacts list -- never generator answer-key tags.

    ``conn`` is used as a fallback for cid_email_derived to find emails that
    are currently in an identifier conflict (and thus excluded from the
    identifier_map by load_contact_index).

    Returns:
        str  -- the unique canonical cid_
        None -- no mapping found (entity unknown to this CRM)
    Raises:
        LegacyMigrationAmbiguous -- if emails map to multiple CRM entries.
    """
    email_to_canonical: dict = {}
    for entry in crm_contacts:
        for id_type, id_value in entry.get("identifiers", []):
            if id_type == "email" and id_value not in email_to_canonical:
                email_to_canonical[id_value] = entry.get("contact_id", "")

    if legacy_format == "cid_email_derived":
        # Primary: emails from the active identifier_map
        emails = [
            id_val
            for (id_type, id_val), cid in identifier_map.items()
            if id_type == "email" and cid == old_link
        ]
        # Fallback: query contact_identifiers directly so conflicted emails
        # (which load_contact_index excludes from the identifier_map) are
        # still used for resolution.
        if not emails and conn is not None:
            try:
                rows = conn.execute(
                    "SELECT identifier_value FROM contact_identifiers"
                    " WHERE contact_id = ? AND identifier_type = 'email'",
                    (old_link,),
                ).fetchall()
                emails = [r[0] for r in rows]
            except sqlite3.OperationalError:
                pass

    elif legacy_format in ("ent_email_derived", "ent_ulid"):
        emails = entity_email_map.get(old_link, [])

    else:
        return None

    candidates = {
        email_to_canonical[e]
        for e in emails
        if e in email_to_canonical and email_to_canonical[e] != old_link
    }
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        raise LegacyMigrationAmbiguous(
            f"Old link {old_link!r} maps to multiple canonical IDs: "
            f"{sorted(candidates)}"
        )
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def migrate_migration_schema(conn: sqlite3.Connection) -> None:
    """Create migration tracking tables (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS identity_migration_runs (
            run_id        TEXT PRIMARY KEY,
            started_at    TEXT NOT NULL,
            completed_at  TEXT,
            state         TEXT NOT NULL DEFAULT 'planning',
            is_dry_run    INTEGER NOT NULL DEFAULT 1,
            plan_json     TEXT
        );

        CREATE TABLE IF NOT EXISTS identity_migration_docs (
            run_id               TEXT NOT NULL
                                     REFERENCES identity_migration_runs(run_id),
            document_id          TEXT NOT NULL,
            old_entity_link      TEXT NOT NULL,
            new_entity_link      TEXT NOT NULL,
            legacy_format        TEXT NOT NULL,
            evidence_hash        TEXT,
            state                TEXT NOT NULL DEFAULT 'planned',
            retain_operation_id  TEXT,
            attempt_count        INTEGER NOT NULL DEFAULT 0,
            last_error           TEXT,
            last_updated         TEXT NOT NULL,
            PRIMARY KEY (run_id, document_id)
        );

        CREATE INDEX IF NOT EXISTS
            idx_migration_docs_state
        ON identity_migration_docs (run_id, state);
    """)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_legacy_links(
    conn: sqlite3.Connection,
    crm_contacts: list,
    hindsight_docs: list,
) -> dict:
    """Find documents with legacy entity links.

    Args:
        conn:            open SQLite connection with contacts tables migrated.
        crm_contacts:    authoritative CRM contacts list (e.g. CRM_CONTACTS).
        hindsight_docs:  list of document dicts from list_documents() or a
                         mock.  Each dict must have 'document_id' and
                         'entities' fields.

    Returns a dict with:
        mappings  -- list of {document_id, old_entity_link, new_entity_link,
                              legacy_format, evidence_hash}
        ambiguous -- list of {document_id, old_link, error}
        unknown   -- list of {document_id, old_link} (no canonical CID found)
    """
    from olo.adapters.contacts import load_contact_index

    identifier_map, _contact_info = load_contact_index(conn)
    entity_email_map = _build_entity_email_map(conn)

    mappings: list = []
    ambiguous: list = []
    unknown: list = []

    for doc in hindsight_docs:
        doc_id = doc.get("document_id") or doc.get("id", "")
        if not doc_id:
            continue
        # Hindsight v1.2.1 stores entities inside retain_params, not at the
        # top level.  Accept both formats so unit tests (which use top-level
        # entities) and live Hindsight responses both work.
        _retain_params = doc.get("retain_params") or {}
        entities = (
            doc.get("entities")                 # unit-test / legacy format
            or _retain_params.get("entities")   # Hindsight v1.2.1 actual
            or []
        )
        if not entities:
            continue
        old_link = (entities[0].get("text") or "").strip()
        if not old_link:
            continue

        fmt = detect_legacy_format(old_link, conn)
        if fmt is None:
            continue  # already canonical

        try:
            new_cid = _resolve_to_canonical(
                old_link, fmt, identifier_map, entity_email_map, crm_contacts,
                conn=conn,
            )
        except LegacyMigrationAmbiguous as exc:
            ambiguous.append(
                {"document_id": doc_id, "old_link": old_link, "error": str(exc)}
            )
            continue

        if new_cid is None:
            unknown.append({"document_id": doc_id, "old_link": old_link})
            continue

        evidence = json.dumps(
            {"doc": doc_id, "old": old_link, "new": new_cid, "fmt": fmt},
            sort_keys=True,
        )
        evidence_hash = hashlib.sha256(evidence.encode()).hexdigest()
        mappings.append({
            "document_id": doc_id,
            "old_entity_link": old_link,
            "new_entity_link": new_cid,
            "legacy_format": fmt,
            "evidence_hash": evidence_hash,
        })

    return {"mappings": mappings, "ambiguous": ambiguous, "unknown": unknown}


# ---------------------------------------------------------------------------
# Plan step
# ---------------------------------------------------------------------------

def plan_identity_migration(
    run_id: str,
    conn: sqlite3.Connection,
    crm_contacts: list,
    hindsight_docs: list,
    is_dry_run: bool = True,
) -> dict:
    """Create a migration run record and all document records in state=planned.

    Writes to the migration tracking tables only -- does not touch contacts,
    contact_identifiers or Hindsight.  Raises LegacyMigrationAmbiguous before
    creating any records if the discovery finds ambiguous mappings.

    Returns the manifest dict:
        run_id, is_dry_run, mappings (list), unknown (list)
    """
    now = datetime.now(timezone.utc).isoformat()
    discovery = discover_legacy_links(conn, crm_contacts, hindsight_docs)

    if discovery["ambiguous"]:
        raise LegacyMigrationAmbiguous(
            f"Migration plan for run {run_id!r} has "
            f"{len(discovery['ambiguous'])} ambiguous mapping(s) -- "
            "cannot proceed safely.  Inspect discovery['ambiguous'] and "
            "resolve each manually before planning."
        )

    migrate_migration_schema(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO identity_migration_runs"
            " (run_id, started_at, state, is_dry_run, plan_json)"
            " VALUES (?, ?, 'in_progress', ?, ?)",
            (
                run_id, now,
                1 if is_dry_run else 0,
                json.dumps({
                    "mappings": len(discovery["mappings"]),
                    "unknown": len(discovery["unknown"]),
                }),
            ),
        )
        for m in discovery["mappings"]:
            conn.execute(
                "INSERT INTO identity_migration_docs"
                " (run_id, document_id, old_entity_link, new_entity_link,"
                "  legacy_format, evidence_hash, state, last_updated)"
                " VALUES (?, ?, ?, ?, ?, ?, 'planned', ?)",
                (
                    run_id, m["document_id"], m["old_entity_link"],
                    m["new_entity_link"], m["legacy_format"],
                    m["evidence_hash"], now,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return {
        "run_id": run_id,
        "is_dry_run": is_dry_run,
        "mappings": discovery["mappings"],
        "unknown": discovery["unknown"],
    }


# ---------------------------------------------------------------------------
# Alias step (SQLite contact-ID rename -- cid_email_derived only)
# ---------------------------------------------------------------------------

def step_alias(run_id: str, conn: sqlite3.Connection) -> int:
    """Rename legacy email-derived cid_ IDs in the contacts tables.

    For each document whose legacy_format is 'cid_email_derived', renames
    the contact ID across all FK-referencing tables in one committed
    transaction, then marks each document as 'aliased'.

    Documents with ent_ legacy formats do not need a SQLite rename -- they
    skip directly from planned to retained.

    Returns the number of documents aliased.
    """
    now = datetime.now(timezone.utc).isoformat()

    rows = conn.execute(
        "SELECT document_id, old_entity_link, new_entity_link"
        " FROM identity_migration_docs"
        " WHERE run_id = ? AND legacy_format = 'cid_email_derived'"
        "   AND state = 'planned'",
        (run_id,),
    ).fetchall()

    if not rows:
        return 0

    # Collect unique old->new contact-ID pairs
    pairs: dict = {}
    for (_doc_id, old_cid, new_cid) in rows:
        pairs[old_cid] = new_cid

    _FK_COLUMNS = [
        ("contact_identifiers",         "contact_id"),
        ("identifier_conflicts",        "existing_contact_id"),
        ("identifier_conflicts",        "new_contact_id"),
        ("identifier_conflicts",        "resolved_contact_id"),
        ("identifier_ownership_history","contact_id"),
        ("contact_name_history",        "contact_id"),
    ]

    conn.execute("BEGIN IMMEDIATE")
    try:
        for old_cid, new_cid in pairs.items():
            if not conn.execute(
                "SELECT 1 FROM contacts WHERE id = ?", (new_cid,)
            ).fetchone():
                conn.execute(
                    "INSERT INTO contacts"
                    " SELECT ? AS id, organization_id, display_name, source,"
                    "   created_at, updated_at"
                    " FROM contacts WHERE id = ?",
                    (new_cid, old_cid),
                )
            for table, col in _FK_COLUMNS:
                try:
                    conn.execute(
                        f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                        (new_cid, old_cid),
                    )
                except sqlite3.OperationalError:
                    pass
            conn.execute("DELETE FROM contacts WHERE id = ?", (old_cid,))

        # Clean up any self-referential conflicts created by the rename.
        # When LEGACY_CID is renamed to ALICE_CID, any conflict row that now
        # has existing_contact_id == new_contact_id is spurious and must be
        # resolved so load_contact_index includes the identifier in future runs.
        try:
            conn.execute(
                "UPDATE identifier_conflicts"
                " SET resolved_contact_id = existing_contact_id,"
                "     resolved_at = ?,"
                "     resolved_by = 'migration_alias'"
                " WHERE existing_contact_id = new_contact_id"
                "   AND resolved_contact_id IS NULL",
                (now,),
            )
        except sqlite3.OperationalError:
            pass  # table may not exist on older schemas

        doc_ids = [r[0] for r in rows]
        conn.executemany(
            "UPDATE identity_migration_docs"
            " SET state='aliased', last_updated=?"
            " WHERE run_id=? AND document_id=?",
            [(now, run_id, doc_id) for doc_id in doc_ids],
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return len(rows)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _commit_op_id(
    run_id: str,
    doc_id: str,
    op_id: str,
    conn: sqlite3.Connection,
) -> None:
    """Commit the operation_id for a document (pre-Hindsight checkpoint).

    This is the crash-safe checkpoint: op_id is durable before the external
    Hindsight call so the same UUID can be reused for idempotent retry.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE identity_migration_docs"
            " SET retain_operation_id=?, attempt_count=attempt_count+1,"
            "     last_updated=?"
            " WHERE run_id=? AND document_id=?",
            (op_id, _now_iso(), run_id, doc_id),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def _commit_doc_state(
    run_id: str,
    doc_id: str,
    state: str,
    error: Optional[str],
    conn: sqlite3.Connection,
) -> None:
    """Commit a state transition for one document."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE identity_migration_docs"
            " SET state=?, last_error=?, last_updated=?"
            " WHERE run_id=? AND document_id=?",
            (state, error, _now_iso(), run_id, doc_id),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# Retain step
# ---------------------------------------------------------------------------

def step_retain(
    run_id: str,
    conn: sqlite3.Connection,
    build_item_fn: Callable,
    retain_fn: Callable,
) -> dict:
    """Call Hindsight retain for each eligible document.

    Eligible states:
      'aliased'  -- cid_email_derived documents after SQLite rename
      'planned'  -- ent_* documents (no alias step needed)
      'failed'   -- any format, retry with same operation_id

    Protocol per document:
      1. Assign a stable operation_id; commit it BEFORE the retain call
         (crash-safe checkpoint).  For synchronous Hindsight retains,
         idempotency is guaranteed via document_id (Hindsight v1.2.1 does
         not echo operation_id on sync retains and uses document_id for
         deduplication).  The persisted op_id is kept for consistent state
         tracking and retry correlation.
      2. Call retain_fn(item_dict, operation_id).
      3. If successful: commit state='retained'.
      4. If raises: commit state='failed', last_error.

    Args:
        build_item_fn: (document_id: str, new_entity_link: str) -> dict
        retain_fn:     (item_dict: dict, operation_id: str) -> None;
                       raises on permanent error.

    Returns a summary dict: retained, failed, skipped.
    """
    rows = conn.execute(
        "SELECT document_id, new_entity_link, retain_operation_id, legacy_format, state"
        " FROM identity_migration_docs"
        " WHERE run_id = ? AND state IN ('aliased', 'planned', 'failed')",
        (run_id,),
    ).fetchall()

    retained = 0
    failed = 0
    skipped = 0

    for (doc_id, new_link, existing_op_id, fmt, state) in rows:
        # planned cid_email_derived must go through alias first
        if state == "planned" and fmt == "cid_email_derived":
            skipped += 1
            continue

        # Reuse existing op_id for idempotent retry; mint new for first attempt
        op_id = existing_op_id or str(uuid.uuid4())

        # CHECKPOINT: commit op_id before the external call
        try:
            _commit_op_id(run_id, doc_id, op_id, conn)
        except Exception as exc:
            _log.warning("step_retain: checkpoint commit failed for %r: %s", doc_id, exc)
            failed += 1
            continue

        # Build item and call Hindsight (outside any SQLite transaction)
        try:
            item = build_item_fn(doc_id, new_link)
            retain_fn(item, op_id)
        except Exception as exc:
            _commit_doc_state(run_id, doc_id, "failed", str(exc), conn)
            _log.warning("step_retain: retain failed for %r (run=%r): %s", doc_id, run_id, exc)
            failed += 1
            continue

        _commit_doc_state(run_id, doc_id, "retained", None, conn)
        retained += 1

    return {"retained": retained, "failed": failed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Verify step
# ---------------------------------------------------------------------------

def step_verify(
    run_id: str,
    conn: sqlite3.Connection,
    get_document_fn: Callable,
) -> dict:
    """Verify each retained document has the correct entity link in Hindsight.

    Queries each document via get_document_fn and checks that:
    - The document exists.
    - Its first entity link equals new_entity_link.
    - No stale legacy link remains.

    Args:
        get_document_fn: (document_id: str) -> dict with 'entities' field;
                         raises if document not found.

    Returns a summary dict: verified, failed, stale_links.
    """
    rows = conn.execute(
        "SELECT document_id, old_entity_link, new_entity_link"
        " FROM identity_migration_docs"
        " WHERE run_id=? AND state='retained'",
        (run_id,),
    ).fetchall()

    verified = 0
    failed = 0
    stale: list = []

    for (doc_id, old_link, new_link) in rows:
        try:
            doc = get_document_fn(doc_id)
        except Exception as exc:
            _commit_doc_state(run_id, doc_id, "failed", f"get_document: {exc}", conn)
            failed += 1
            continue

        entities = (
            doc.get("entities")
            or (doc.get("retain_params") or {}).get("entities")
            or []
        )
        actual_link = (entities[0].get("text") or "") if entities else ""

        if actual_link == new_link:
            _commit_doc_state(run_id, doc_id, "verified", None, conn)
            verified += 1
        elif actual_link == old_link:
            msg = (
                f"Stale legacy link {old_link!r} still present "
                f"(expected {new_link!r})"
            )
            _commit_doc_state(run_id, doc_id, "failed", msg, conn)
            stale.append(
                {"document_id": doc_id, "old_link": old_link, "actual": actual_link}
            )
            failed += 1
        else:
            msg = (
                f"Unexpected entity link {actual_link!r} "
                f"(expected {new_link!r}, old {old_link!r})"
            )
            _commit_doc_state(run_id, doc_id, "failed", msg, conn)
            failed += 1

    _maybe_complete_run(run_id, conn)
    return {"verified": verified, "failed": failed, "stale_links": stale}


def _maybe_complete_run(run_id: str, conn: sqlite3.Connection) -> None:
    """Mark the run completed when no documents remain in a pending state."""
    pending = conn.execute(
        "SELECT COUNT(*) FROM identity_migration_docs"
        " WHERE run_id=? AND state NOT IN ('verified', 'failed')",
        (run_id,),
    ).fetchone()[0]
    if pending > 0:
        return
    failed_count = conn.execute(
        "SELECT COUNT(*) FROM identity_migration_docs"
        " WHERE run_id=? AND state='failed'",
        (run_id,),
    ).fetchone()[0]
    final_state = "completed" if failed_count == 0 else "incomplete"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE identity_migration_runs"
            " SET state=?, completed_at=? WHERE run_id=?",
            (final_state, _now_iso(), run_id),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def resume_migration(
    run_id: str,
    conn: sqlite3.Connection,
    build_item_fn: Callable,
    retain_fn: Callable,
    get_document_fn: Callable,
) -> dict:
    """Resume a migration run from its last committed state.

    Idempotent: calling resume on a completed run is a no-op.

    Protocol:
      1. step_alias  -- rename any planned cid_email_derived contacts.
      2. step_retain -- retain aliased + planned-ent_* + failed documents.
      3. step_verify -- verify entity links for retained documents.

    Returns consolidated summary:
        aliased, retained, failed, verified, stale_links, run_state
    """
    run = conn.execute(
        "SELECT state FROM identity_migration_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if run is None:
        raise LegacyMigrationError(f"Migration run {run_id!r} not found.")

    alias_count = step_alias(run_id, conn)
    retain_summary = step_retain(run_id, conn, build_item_fn, retain_fn)
    verify_summary = step_verify(run_id, conn, get_document_fn)

    run_state_row = conn.execute(
        "SELECT state FROM identity_migration_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    run_state = run_state_row[0] if run_state_row else "unknown"

    return {
        "aliased": alias_count,
        "retained": retain_summary["retained"],
        "failed": retain_summary["failed"],
        "verified": verify_summary["verified"],
        "stale_links": verify_summary["stale_links"],
        "run_state": run_state,
    }


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------

def get_migration_status(run_id: str, conn: sqlite3.Connection) -> dict:
    """Return a status summary for a migration run.

    Returns:
        run_id, state, is_dry_run, started_at, completed_at,
        counts (dict of state->count), incomplete, recoverable
    """
    run = conn.execute(
        "SELECT run_id, state, is_dry_run, started_at, completed_at"
        " FROM identity_migration_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if run is None:
        return {"run_id": run_id, "state": "not_found"}

    counts: dict = {}
    for (state, cnt) in conn.execute(
        "SELECT state, COUNT(*) FROM identity_migration_docs"
        " WHERE run_id=? GROUP BY state",
        (run_id,),
    ).fetchall():
        counts[state] = cnt

    incomplete = run[1] not in ("completed",)
    recoverable = any(
        counts.get(s, 0) > 0
        for s in ("failed", "planned", "aliased", "retained")
    )

    return {
        "run_id": run[0],
        "state": run[1],
        "is_dry_run": bool(run[2]),
        "started_at": run[3],
        "completed_at": run[4],
        "counts": counts,
        "incomplete": incomplete,
        "recoverable": recoverable,
    }
