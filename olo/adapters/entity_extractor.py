"""olo/adapters/entity_extractor.py â€” Extract entity contacts from signal payloads.

Populates the ``entity_metadata`` table from Gmail, Calendar, Stripe and QBO
source signals as they are ingested.  Called by the runner after each
``ingest_batch`` so every stored signal has its participants recorded.

Design rules (P0.3 sixth-remediation corrections):
- Never fabricate display names.  Only record names present in the signal payload.
- Never import from olo.m3.synthetic â€” the production path must not depend on
  test generators.
- One entity_id per unique (source, email) pair, computed deterministically.
- Multiple sources for the same email are stored as separate rows â€” no cross-
  source merging (preserves per-business isolation).
- Missing or unattributed participants produce no rows (safe fallback).
- No side effects at import time.
- ``resolve_primary_entity_id_from_payload`` is the single consistent mechanism
  shared by ingestion (entity_extractor) and the feeder (build_hindsight_items).

Actual adapter output structures (verified against production adapters):

- ``email.received`` / ``email.sent`` (GmailAdapter._message_to_signal):
    payload["headers"]["from"]  = [{"name": ..., "email": ...}, ...]
    payload["headers"]["to"]    = [...]
    payload["headers"]["cc"]    = [...]
    Addresses are nested under payload["headers"], NOT top-level.

- ``email.content`` (gmail_full.build_email_content_signal):
    payload["from"] = [{"name": ..., "email": ...}, ...]
    payload["to"]   = [...]
    payload["cc"]   = [...]
    Addresses are TOP-LEVEL (different structure from email.received/sent).
    payload["direction"] = "sent" | "received"

- ``calendar.event.changed`` / ``calendar.event.cancelled`` (gcal._event_to_signal):
    payload["organizer_email"] = str (email only)
    payload["organizer_display_name"] = str (display name, may be "")
    payload["attendees"] = [
        {"email": str, "display_name": str, "response_status": str,
         "organizer": bool, "self": bool}, ...
    ]
    Kind is .changed or .cancelled, NOT .created or .updated.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Union
from typing import Optional

from olo.signals import NormalizedSignal

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entity ID computation
# ---------------------------------------------------------------------------

def entity_id_from_email(email: str) -> str:
    """Compute a stable entity_id from a normalised email address.

    Uses the first 20 hex characters of SHA-256(email.lower()) prefixed with
    ``ent_``, giving a 24-character identifier that is:
    - Deterministic across process restarts and schema migrations.
    - Collision-resistant for typical business address books.

    Returns ``""`` for empty or invalid addresses (no ``@`` sign).
    """
    email = email.strip().lower()
    if not email or "@" not in email:
        return ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"ent_{digest[:20]}"


def _first_email_id(addrs: list) -> str:
    """Return entity_id of first valid email in an address list, or ``""``."""
    for addr in addrs:
        if not isinstance(addr, dict):
            continue
        email_str = (addr.get("email") or "").strip().lower()
        eid = entity_id_from_email(email_str)
        if eid:
            return eid
    return ""


# ---------------------------------------------------------------------------
# Signal-level extraction
# ---------------------------------------------------------------------------

def _extract_from_gmail(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a Gmail signal payload.

    Handles TWO distinct payload structures produced by the Gmail adapters:

    email.received / email.sent (GmailAdapter._message_to_signal):
        payload["headers"]["from"] = [{"name": ..., "email": ...}, ...]
        payload["headers"]["to"]   = [...]
        payload["headers"]["cc"]   = [...]

    email.content (gmail_full.build_email_content_signal):
        payload["from"] = [{"name": ..., "email": ...}, ...]  # top-level
        payload["to"]   = [...]
        payload["cc"]   = [...]

    Returns entity rows for every participant with a valid email address.
    Display names come directly from the RFC 2047-decoded header; empty names
    are stored as empty strings (never fabricated).
    """
    # Addresses may be top-level (email.content) or nested under "headers"
    # (email.received/sent).  Try top-level first, fall back to headers.
    hdr = payload.get("headers") or {}
    rows: list[dict] = []
    seen: set[str] = set()
    for field in ("from", "to", "cc"):
        addrs = payload.get(field) or hdr.get(field) or []
        for addr in addrs:
            if not isinstance(addr, dict):
                continue
            email_str = (addr.get("email") or "").strip().lower()
            eid = entity_id_from_email(email_str)
            if not eid or eid in seen:
                continue
            seen.add(eid)
            rows.append({
                "entity_id": eid,
                "display_name": (addr.get("name") or "").strip(),
                "company": "",
                "source_kind": "email_contact",
                "source": source,
            })
    return rows


def _extract_from_gcal(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a Calendar event payload.

    Handles the actual gcal._event_to_signal output:
        payload["organizer_email"]        = str (email only)
        payload["organizer_display_name"] = str (may be ""; added in P0.3 fix)
        payload["attendees"] = [
            {"email": str, "display_name": str, "response_status": str,
             "organizer": bool, "self": bool}, ...
        ]

    Note: ``displayName`` was renamed to ``display_name`` in the attendee
    dict when the gcal adapter was fixed to preserve it.
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
            "display_name": (payload.get("organizer_display_name") or "").strip(),
            "company": "",
            "source_kind": "calendar_organizer",
            "source": source,
        })

    # Attendees
    for att in payload.get("attendees") or []:
        if not isinstance(att, dict):
            continue
        email_str = (att.get("email") or "").strip().lower()
        eid = entity_id_from_email(email_str)
        if not eid or eid in seen:
            continue
        seen.add(eid)
        # Accept both "display_name" (new field name) and "displayName" (legacy)
        display = (att.get("display_name") or att.get("displayName") or "").strip()
        rows.append({
            "entity_id": eid,
            "display_name": display,
            "company": "",
            "source_kind": "calendar_attendee",
            "source": source,
        })
    return rows


def _extract_from_stripe(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a Stripe invoice signal.

    Stripe adapter stores customer info in payload["customer"] or top-level
    ``customer_email``/``customer_name`` fields where available.
    No production Stripe adapter exists yet â€” this is a stub for when it does.
    """
    rows: list[dict] = []
    customer = payload.get("customer") or {}
    if isinstance(customer, dict):
        email_str = (customer.get("email") or "").strip().lower()
        name = (customer.get("name") or "").strip()
    else:
        email_str = (payload.get("customer_email") or "").strip().lower()
        name = (payload.get("customer_name") or "").strip()

    eid = entity_id_from_email(email_str)
    if eid:
        rows.append({
            "entity_id": eid,
            "display_name": name,
            "company": name,
            "source_kind": "stripe_customer",
            "source": source,
        })
    return rows


def _extract_from_qbo(payload: dict, source: str) -> list[dict]:
    """Extract entity rows from a QuickBooks invoice/transaction payload.
    No production QBO adapter exists yet â€” this is a stub for when it does.
    """
    rows: list[dict] = []
    email_str = (payload.get("customer_email") or "").strip().lower()
    name = (payload.get("customer_name") or payload.get("DisplayName") or "").strip()
    eid = entity_id_from_email(email_str)
    if eid:
        rows.append({
            "entity_id": eid,
            "display_name": name,
            "company": name,
            "source_kind": "qbo_customer",
            "source": source,
        })
    return rows


def extract_entities(signal: NormalizedSignal) -> list[dict]:
    """Extract entity contact records from a signal payload.

    Returns a list of dicts with keys:
        entity_id, display_name, company, source_kind, source

    Handles the actual kind values produced by existing adapters:
    - email.received, email.sent, email.content (Gmail)
    - calendar.event.changed, calendar.event.cancelled (Calendar)
    - calendar.event.created, calendar.event.updated (older/alternate)
    - invoice.created routed by source name prefix (stub)

    Returns an empty list when no entities can be reliably extracted.
    Never fabricates names from non-authoritative data.
    """
    kind = signal.kind
    source = signal.source
    payload = dict(signal.payload) if signal.payload else {}

    if kind in ("email.received", "email.sent", "email.content"):
        return _extract_from_gmail(payload, source)
    if kind in (
        "calendar.event.changed",
        "calendar.event.cancelled",
        "calendar.event.created",   # keep for compat with older/alternate adapters
        "calendar.event.updated",
    ):
        return _extract_from_gcal(payload, source)
    if kind == "invoice.created":
        if "stripe" in source.lower():
            return _extract_from_stripe(payload, source)
        if "qbo" in source.lower() or "quickbooks" in source.lower():
            return _extract_from_qbo(payload, source)
    return []


# ---------------------------------------------------------------------------
# Primary entity resolution (shared by ingestion and feeder)
# ---------------------------------------------------------------------------

def resolve_primary_entity_id_from_payload(kind: str, payload: dict) -> str:
    """Derive the primary counterparty entity_id from a signal's kind and payload.

    This is the SINGLE consistent resolution mechanism shared by:
    - ``upsert_entities`` during ingestion (runner.py)
    - ``build_hindsight_items`` in the feeder (feeder.py)

    The same input (kind, payload) produces the same entity_id in both paths,
    ensuring the feeder's context enrichment uses the same identity as ingestion.

    Priority:
    1. ``payload["entity_id"]`` if present and non-empty (synthetic / pre-labelled).
    2. ``email.received``: sender (the person who wrote to the owner).
    3. ``email.sent``: first TO recipient (the person the owner wrote to).
    4. ``email.content``: sender if ``direction=received``, first TO if ``direction=sent``.
    5. ``calendar.event.*``: organizer if not self, else first non-self attendee.

    Returns ``""`` when:
    - No reliable counterparty can be identified.
    - Multiple non-self recipients exist for a sent email (ambiguous; use first TO).
    - Only self-attendees exist in a calendar event.

    Does NOT fabricate names. Does NOT infer company from email domains.
    Logs a debug message when falling back to "" so unresolved identities
    are observable.
    """
    # Check for pre-computed entity_id (synthetic/pre-labelled signals)
    explicit = (payload.get("entity_id") or "").strip()
    if explicit:
        return explicit

    hdr = payload.get("headers") or {}

    if kind == "email.received":
        # Counterparty = sender
        from_addrs = payload.get("from") or hdr.get("from") or []
        eid = _first_email_id(from_addrs)
        if not eid:
            _log.debug("resolve_primary_entity_id: email.received has no from address")
        return eid

    if kind == "email.sent":
        # Counterparty = first TO recipient
        to_addrs = payload.get("to") or hdr.get("to") or []
        eid = _first_email_id(to_addrs)
        if not eid:
            _log.debug("resolve_primary_entity_id: email.sent has no to address")
        return eid

    if kind == "email.content":
        direction = payload.get("direction", "received")
        if direction == "sent":
            to_addrs = payload.get("to") or []
            eid = _first_email_id(to_addrs)
            if not eid:
                _log.debug("resolve_primary_entity_id: email.content(sent) has no to address")
            return eid
        else:
            from_addrs = payload.get("from") or []
            eid = _first_email_id(from_addrs)
            if not eid:
                _log.debug("resolve_primary_entity_id: email.content(received) has no from address")
            return eid

    if kind in (
        "calendar.event.changed",
        "calendar.event.cancelled",
        "calendar.event.created",
        "calendar.event.updated",
    ):
        # Use organizer if not self; else first non-self attendee.
        # "self" = the account owner whose calendar this is.
        org_email = (payload.get("organizer_email") or "").strip().lower()
        if org_email:
            # Check if any attendee has self=True AND the same email as the organizer
            is_org_self = any(
                att.get("self") and (att.get("email") or "").lower() == org_email
                for att in (payload.get("attendees") or [])
                if isinstance(att, dict)
            )
            if not is_org_self:
                return entity_id_from_email(org_email)

        # Fall back to first non-self attendee
        for att in payload.get("attendees") or []:
            if not isinstance(att, dict):
                continue
            if att.get("self"):
                continue
            att_email = (att.get("email") or "").strip().lower()
            eid = entity_id_from_email(att_email)
            if eid:
                return eid

        _log.debug("resolve_primary_entity_id: calendar event has no non-self counterparty")
        return ""

    return ""


def resolve_primary_entity_id(signal: NormalizedSignal) -> str:
    """Convenience wrapper: resolve from a NormalizedSignal object."""
    payload = dict(signal.payload) if signal.payload else {}
    return resolve_primary_entity_id_from_payload(signal.kind, payload)


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

    Runs a single BEGIN IMMEDIATE â€¦ COMMIT block for all rows extracted from
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
    # Prefer a row with a non-empty display_name when the same entity appears
    # multiple times (e.g. sender and CC recipient in the same batch).
    deduped: dict[tuple[str, str], dict] = {}
    for sig in signals:
        for row in extract_entities(sig):
            key = (row["source"], row["entity_id"])
            existing = deduped.get(key)
            if existing is None or (
                not existing.get("display_name") and row.get("display_name")
            ):
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
            upserted += conn.execute("SELECT changes()").fetchone()[0]
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return upserted

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
