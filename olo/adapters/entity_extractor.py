"""olo/adapters/entity_extractor.py — Extract entity contacts from signal payloads.

Populates the ``entity_metadata`` table from Gmail, Calendar, Stripe and QBO
source signals as they are ingested.  Called by the runner after each
``ingest_batch`` so every stored signal has its participants recorded.

Design rules:
- Never fabricate display names.  Only record names present in the signal payload.
- Never import from olo.m3.synthetic — the production path must not depend on
  test generators.
- One entity_id per unique (source, email) pair, computed deterministically.
- Multiple sources for the same email are stored as separate rows — no cross-
  source merging (preserves per-business isolation).
- Missing or unattributed participants produce no rows (safe fallback).
- No side effects at import time.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from olo.signals import NormalizedSignal


# ---------------------------------------------------------------------------
# Entity ID computation
# ---------------------------------------------------------------------------

def entity_id_from_email(email: str) -> str:
    """Compute a stable entity_id from a normalised email address.

    Uses the first 20 hex characters of SHA-256(email.lower()) prefixed with
    ``ent_``, giving a 24-character identifier that is:
    - Deterministic across process restarts and schema migrations.
    - Collision-resistant for typical business address books.
    - Compatible with the existing ``ent_01...`` IDs used in test fixtures
      (different prefix ensures no accidental collision).

    ``email`` must be a valid address string; empty strings return ``""``.
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        return ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"ent_{digest[:20]}"


# ---------------------------------------------------------------------------
# Signal-level extraction
# ---------------------------------------------------------------------------

def _extract_from_gmail(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a Gmail email.received or email.sent payload.

    Gmail adapter stores parsed address lists as:
        payload["from"] = [{"name": "Alice", "email": "alice@acme.com"}, ...]
        payload["to"]   = [...]
        payload["cc"]   = [...]

    Returns entity rows for every participant with a valid email address.
    Display names come directly from the RFC 2047-decoded header; empty names
    are stored as empty strings (never fabricated).
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for field in ("from", "to", "cc"):
        for addr in payload.get(field) or []:
            if not isinstance(addr, dict):
                continue
            email = (addr.get("email") or "").strip().lower()
            eid = entity_id_from_email(email)
            if not eid or eid in seen:
                continue
            seen.add(eid)
            rows.append({
                "entity_id": eid,
                "display_name": (addr.get("name") or "").strip(),
                "company": "",           # not available from email headers alone
                "source_kind": "email_contact",
                "source": source,
            })
    return rows


def _extract_from_gcal(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a Calendar event payload.

    Calendar adapter stores:
        payload["organizer_email"] = "alice@acme.com"
        payload["attendees"] = [
            {"email": "bob@beta.com", "response_status": "accepted", ...},
            ...
        ]
    """
    rows: list[dict] = []
    seen: set[str] = set()

    # Organizer
    org_email = (payload.get("organizer_email") or "").strip().lower()
    org_eid = entity_id_from_email(org_email)
    if org_eid:
        seen.add(org_eid)
        rows.append({
            "entity_id": org_eid,
            "display_name": "",   # organizer displayName not stored separately
            "company": "",
            "source_kind": "calendar_organizer",
            "source": source,
        })

    # Attendees
    for att in payload.get("attendees") or []:
        if not isinstance(att, dict):
            continue
        email = (att.get("email") or "").strip().lower()
        eid = entity_id_from_email(email)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        rows.append({
            "entity_id": eid,
            "display_name": (att.get("displayName") or "").strip(),
            "company": "",
            "source_kind": "calendar_attendee",
            "source": source,
        })
    return rows


def _extract_from_stripe(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a Stripe invoice signal.

    Stripe adapter stores customer info in payload["customer"] or top-level
    ``customer_email``/``customer_name`` fields where available.
    """
    rows: list[dict] = []
    customer = payload.get("customer") or {}
    if isinstance(customer, dict):
        email = (customer.get("email") or "").strip().lower()
        name = (customer.get("name") or "").strip()
    else:
        email = (payload.get("customer_email") or "").strip().lower()
        name = (payload.get("customer_name") or "").strip()

    eid = entity_id_from_email(email)
    if eid:
        rows.append({
            "entity_id": eid,
            "display_name": name,
            "company": name,   # for Stripe, company = customer name
            "source_kind": "stripe_customer",
            "source": source,
        })
    return rows


def _extract_from_qbo(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a QuickBooks invoice/transaction payload."""
    rows: list[dict] = []
    email = (payload.get("customer_email") or "").strip().lower()
    name = (payload.get("customer_name") or payload.get("DisplayName") or "").strip()
    eid = entity_id_from_email(email)
    if eid:
        rows.append({
            "entity_id": eid,
            "display_name": name,
            "company": name,
            "source_kind": "qbo_customer",
            "source": source,
        })
    return rows


# Kind-to-extractor dispatch table
_KIND_EXTRACTORS: dict[str, object] = {
    "email.received": _extract_from_gmail,
    "email.sent": _extract_from_gmail,
    "calendar.event.created": _extract_from_gcal,
    "calendar.event.updated": _extract_from_gcal,
    "invoice.created": None,   # dispatched by source prefix below
}


def extract_entities(signal: NormalizedSignal) -> list[dict]:
    """Extract entity contact records from a signal payload.

    Returns a list of dicts with keys:
        entity_id, display_name, company, source_kind, source

    Returns an empty list when no entities can be reliably extracted.
    Never fabricates names from non-authoritative data.
    """
    kind = signal.kind
    source = signal.source
    payload = dict(signal.payload) if signal.payload else {}

    if kind in ("email.received", "email.sent"):
        return _extract_from_gmail(payload, source)
    if kind in ("calendar.event.created", "calendar.event.updated"):
        return _extract_from_gcal(payload, source)
    if kind == "invoice.created":
        # Route by source name prefix
        if "stripe" in source.lower():
            return _extract_from_stripe(payload, source)
        if "qbo" in source.lower() or "quickbooks" in source.lower():
            return _extract_from_qbo(payload, source)
    return []


# ---------------------------------------------------------------------------
# Batch extraction + SQLite upsert
# ---------------------------------------------------------------------------

def upsert_entities(
    signals: tuple[NormalizedSignal, ...],
    source: str,
    conn: sqlite3.Connection,
    clock=None,
) -> int:
    """Extract entity contacts from *signals* and upsert into entity_metadata.

    Runs a single BEGIN IMMEDIATE … COMMIT block for all rows extracted from
    this batch.  Idempotent: existing rows are updated with the latest metadata.

    Returns the number of rows inserted or updated (0 is normal when no signals
    have participant information).

    ``clock`` is optional; defaults to ``datetime.now(timezone.utc)``.
    """
    if clock is None:
        now_str = datetime.now(timezone.utc).isoformat()
    else:
        now_str = clock().isoformat()

    # Collect rows, deduplicating by (source, entity_id) within this batch.
    # Last writer wins when the same entity appears in multiple signals.
    deduped: dict[tuple[str, str], dict] = {}
    for sig in signals:
        for row in extract_entities(sig):
            key = (row["source"], row["entity_id"])
            # Prefer a row with a non-empty display_name
            existing = deduped.get(key)
            if existing is None or (not existing.get("display_name") and row.get("display_name")):
                deduped[key] = row

    if not deduped:
        return 0

    conn.execute("BEGIN IMMEDIATE")
    upserted = 0
    try:
        for row in deduped.values():
            conn.execute(
                "INSERT INTO entity_metadata"
                "(source, entity_id, display_name, company, source_kind,"
                " created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(source, entity_id) DO UPDATE SET"
                "  display_name = CASE WHEN excluded.display_name != '' THEN"
                "    excluded.display_name ELSE entity_metadata.display_name END,"
                "  company = CASE WHEN excluded.company != '' THEN"
                "    excluded.company ELSE entity_metadata.company END,"
                "  source_kind = excluded.source_kind,"
                "  updated_at = excluded.updated_at",
                (
                    row["source"],
                    row["entity_id"],
                    row["display_name"],
                    row["company"],
                    row["source_kind"],
                    now_str,
                    now_str,
                ),
            )
            upserted += conn.execute(
                "SELECT changes()"
            ).fetchone()[0]
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return upserted
