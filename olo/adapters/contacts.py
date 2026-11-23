"""olo/adapters/contacts.py — Canonical per-business customer identity model.

Implements the C1 contact/organization schema and C2 source-backed identity
resolution (Opus audit §C1–C2).

Schema
------
  organizations
      id TEXT PK          oid_<sha256[:20]> derived from lower(org_name)
      name TEXT           canonical organization name
      source TEXT         which CRM/fixture populated this record
      created_at TEXT     ISO-8601
      updated_at TEXT     ISO-8601

  contacts
      id TEXT PK          cid_<sha256[:20]> derived from canonical_email
      organization_id TEXT FK → organizations.id  (nullable)
      display_name TEXT   canonical name from CRM (never from signal payload)
      source TEXT         which CRM/fixture set this record
      created_at TEXT
      updated_at TEXT

  contact_identifiers
      identifier_type TEXT   'email' | 'phone' | 'stripe_customer_id' | ...
      identifier_value TEXT  normalized value (email lower-cased)
      contact_id TEXT FK → contacts.id
      source TEXT
      verified_at TEXT
      PK (identifier_type, identifier_value, source)

  contact_name_history
      id INTEGER PK AUTOINCREMENT
      contact_id TEXT NOT NULL
      old_display_name TEXT
      new_display_name TEXT NOT NULL
      changed_at TEXT NOT NULL
      source TEXT NOT NULL

Design rules (C1/C2)
--------------------
- contact_id is ALWAYS derived from the contact record's canonical_email, not from
  a signal's ent: tag or customer_name payload field.
- Only CRM-fixture population or authorized contact-update calls may insert or
  update contact records.
- Signal payloads never rename a contact; payload claims are ignored.
- resolve_contact_from_signal() uses ONLY verified identifiers in
  contact_identifiers; it never reads ent: tags or payload.customer_name.
- Returns "" when no verified match is found — never fabricates an association.
- Business isolation: contacts tables live inside the per-business olo.db.
- No side effects at import time.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stable ID derivation
# ---------------------------------------------------------------------------

def make_contact_id(canonical_email: str) -> str:
    """Derive a stable, opaque contact_id from the contact's canonical email.

    Uses the first 20 hex characters of SHA-256(lower(email)) prefixed with
    ``cid_``.  Stable across identifier additions; deterministic for the same
    email.

    Returns "" for empty or invalid inputs.
    """
    email = canonical_email.strip().lower()
    if not email or "@" not in email:
        return ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"cid_{digest[:20]}"


def make_org_id(org_name: str) -> str:
    """Derive a stable organization_id from the canonical organization name.

    Uses the first 20 hex characters of SHA-256(lower(org_name)) prefixed
    with ``oid_``.

    Returns "" for empty inputs.
    """
    name = org_name.strip().lower()
    if not name:
        return ""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"oid_{digest[:20]}"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def migrate_contacts(conn: sqlite3.Connection) -> None:
    """Create contact identity tables if they don't exist (idempotent).

    Safe to call on every startup; uses IF NOT EXISTS guards.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS organizations (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            source      TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS contacts (
            id              TEXT PRIMARY KEY,
            organization_id TEXT,
            display_name    TEXT NOT NULL,
            source          TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            FOREIGN KEY (organization_id) REFERENCES organizations(id)
        );

        CREATE TABLE IF NOT EXISTS contact_identifiers (
            identifier_type  TEXT NOT NULL,
            identifier_value TEXT NOT NULL,
            contact_id       TEXT NOT NULL,
            source           TEXT NOT NULL,
            verified_at      TEXT NOT NULL,
            PRIMARY KEY (identifier_type, identifier_value, source),
            FOREIGN KEY (contact_id) REFERENCES contacts(id)
        );

        CREATE TABLE IF NOT EXISTS contact_name_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id       TEXT NOT NULL,
            old_display_name TEXT,
            new_display_name TEXT NOT NULL,
            changed_at       TEXT NOT NULL,
            source           TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS
            idx_contact_identifiers_lookup
        ON contact_identifiers (identifier_type, identifier_value);
    """)


# ---------------------------------------------------------------------------
# CRM fixture population
# ---------------------------------------------------------------------------

def populate_contacts_from_crm(
    crm_contacts: list,
    conn: sqlite3.Connection,
    source: str = "crm_fixture",
    clock=None,
) -> int:
    """Insert or update contact records from a CRM fixture.

    *crm_contacts* is the list of dicts from ``crm_fixture.CRM_CONTACTS``.
    Each entry must have:
        canonical_email (str)
        display_name (str)
        organization_name (str)
        identifiers (list[tuple[str, str]])

    Idempotent: existing records are updated when canonical data changes.
    Name changes are recorded in ``contact_name_history`` for provenance.

    Returns the number of contacts upserted.
    """
    if not crm_contacts:
        return 0
    now = clock().isoformat() if clock else datetime.now(timezone.utc).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    upserted = 0
    try:
        for entry in crm_contacts:
            canonical_email = (entry.get("canonical_email") or "").strip().lower()
            if not canonical_email:
                continue
            contact_id = make_contact_id(canonical_email)
            if not contact_id:
                continue
            display_name = (entry.get("display_name") or "").strip()
            org_name = (entry.get("organization_name") or "").strip()
            org_id = make_org_id(org_name) if org_name else None

            # Upsert organization
            if org_id and org_name:
                conn.execute(
                    "INSERT INTO organizations (id, name, source, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET"
                    "  name = excluded.name,"
                    "  updated_at = excluded.updated_at",
                    (org_id, org_name, source, now, now),
                )

            # Upsert contact — record name change in history if display_name differs
            existing = conn.execute(
                "SELECT display_name FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO contacts"
                    " (id, organization_id, display_name, source, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (contact_id, org_id, display_name, source, now, now),
                )
            else:
                old_name = existing[0]
                conn.execute(
                    "UPDATE contacts SET"
                    "  organization_id = ?,"
                    "  display_name = ?,"
                    "  source = ?,"
                    "  updated_at = ?"
                    " WHERE id = ?",
                    (org_id, display_name, source, now, contact_id),
                )
                if display_name and display_name != old_name:
                    conn.execute(
                        "INSERT INTO contact_name_history"
                        " (contact_id, old_display_name, new_display_name,"
                        "  changed_at, source)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (contact_id, old_name, display_name, now, source),
                    )

            # Upsert identifiers
            for id_type, id_value in (entry.get("identifiers") or []):
                normalized = id_value.strip()
                if id_type == "email":
                    normalized = normalized.lower()
                if not normalized:
                    continue
                conn.execute(
                    "INSERT INTO contact_identifiers"
                    " (identifier_type, identifier_value, contact_id,"
                    "  source, verified_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(identifier_type, identifier_value, source)"
                    " DO UPDATE SET"
                    "  contact_id = excluded.contact_id,"
                    "  verified_at = excluded.verified_at",
                    (id_type, normalized, contact_id, source, now),
                )

            upserted += 1

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return upserted


# ---------------------------------------------------------------------------
# Contact cache for fast per-build lookup
# ---------------------------------------------------------------------------

def load_contact_index(
    conn: sqlite3.Connection,
) -> tuple[dict[tuple[str, str], str], dict[str, tuple[str, str]]]:
    """Load all contact data into two in-memory maps for fast per-signal lookup.

    Returns:
        identifier_map: {(identifier_type, identifier_value): contact_id}
        contact_info:   {contact_id: (display_name, org_name)}

    Both maps are plain dicts; the caller owns them.  Returns empty dicts when
    the contacts tables do not yet exist (old olo.db files built before this
    schema version).
    """
    identifier_map: dict[tuple[str, str], str] = {}
    contact_info: dict[str, tuple[str, str]] = {}
    try:
        rows = conn.execute(
            "SELECT identifier_type, identifier_value, contact_id"
            " FROM contact_identifiers"
        ).fetchall()
        for row in rows:
            identifier_map[(row[0], row[1])] = row[2]

        rows = conn.execute(
            "SELECT c.id, c.display_name, COALESCE(o.name, '')"
            " FROM contacts c"
            " LEFT JOIN organizations o ON o.id = c.organization_id"
        ).fetchall()
        for row in rows:
            contact_info[row[0]] = (row[1], row[2])

    except sqlite3.OperationalError:
        # Tables do not exist — returns empty maps safely.
        pass

    return identifier_map, contact_info


# ---------------------------------------------------------------------------
# Signal-level contact resolution
# ---------------------------------------------------------------------------

def _pick_emails_from_gmail(payload: dict, kind: str) -> list[str]:
    """Extract the counterparty email addresses from a Gmail signal payload."""
    hdr = payload.get("headers") or {}
    if kind == "email.received":
        addrs = payload.get("from") or hdr.get("from") or []
    elif kind == "email.sent":
        addrs = payload.get("to") or hdr.get("to") or []
    elif kind == "email.content":
        direction = payload.get("direction", "received")
        if direction == "sent":
            addrs = payload.get("to") or []
        else:
            addrs = payload.get("from") or []
    else:
        return []
    emails = []
    for addr in addrs:
        if isinstance(addr, dict):
            e = (addr.get("email") or "").strip().lower()
            if e and "@" in e:
                emails.append(e)
    return emails


def _pick_calendar_emails(payload: dict) -> list[str]:
    """Extract the counterparty email from a Calendar event payload."""
    org_email = (payload.get("organizer_email") or "").strip().lower()
    is_org_self = bool(payload.get("organizer_self", False))

    if org_email and not is_org_self:
        return [org_email]

    # Organizer is self — return first non-self attendee
    for att in (payload.get("attendees") or []):
        if isinstance(att, dict) and not att.get("self"):
            e = (att.get("email") or "").strip().lower()
            if e and "@" in e:
                return [e]
    return []


def resolve_contact_from_signal(
    kind: str,
    payload: dict,
    identifier_map: dict[tuple[str, str], str],
) -> str:
    """Resolve a canonical contact_id from a signal's verified source identifiers.

    Uses ONLY the contact_identifiers table (pre-loaded into *identifier_map*).
    Never reads ``ent:`` tags, ``customer_name``, or any other asserted payload
    field that could be forged.

    Resolution order per signal kind:
      email.received/sent/content → email address(es) from headers
      calendar.event.*            → non-self organizer / attendee email
      call.transcript             → phone number from metadata
      invoice.created             → customer_email, then stripe/qbo customer_id

    Returns the contact_id string (``cid_...``) or ``""`` when no verified
    mapping exists for this signal.
    """
    candidates: list[tuple[str, str]] = []

    if kind in ("email.received", "email.sent", "email.content"):
        for email in _pick_emails_from_gmail(payload, kind):
            candidates.append(("email", email))

    elif kind in (
        "calendar.event.changed",
        "calendar.event.cancelled",
        "calendar.event.created",
        "calendar.event.updated",
    ):
        for email in _pick_calendar_emails(payload):
            candidates.append(("email", email))

    elif kind == "call.transcript":
        # Verified identifier: the caller's phone number from the call metadata.
        # Payload.customer_name is NOT used — it is an assertion, not a CRM fact.
        phone = (payload.get("phone") or "").strip()
        if phone:
            candidates.append(("phone", phone))

    elif kind == "invoice.created":
        # Priority 1: customer email (most specific identifier).
        email = (payload.get("customer_email") or "").strip().lower()
        if email and "@" in email:
            candidates.append(("email", email))
        # Priority 2: billing system customer ID (Stripe or QBO).
        cust_id = (payload.get("customer_id") or "").strip()
        if cust_id:
            source_tag = (payload.get("source") or "").lower()
            if "stripe" in source_tag:
                candidates.append(("stripe_customer_id", cust_id))
            elif "qbo" in source_tag or "quickbooks" in source_tag:
                candidates.append(("qbo_customer_id", cust_id))

    for id_type, id_value in candidates:
        contact_id = identifier_map.get((id_type, id_value))
        if contact_id:
            return contact_id

    return ""
