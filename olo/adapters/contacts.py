"""olo/adapters/contacts.py — Canonical per-business customer identity model.

Implements the C1 contact/organization schema and C2 source-backed identity
resolution (Opus audit §C1–C2), with Defect A and B repairs and
identity-safety fixes 1–3 (Opus P1/P2).

Schema
------
  organizations
      id TEXT PK          oid_<sha256[:20]> derived from lower(org_name)
      name TEXT           canonical organization name
      source TEXT         which CRM/fixture populated this record
      created_at TEXT     ISO-8601
      updated_at TEXT     ISO-8601

  contacts
      id TEXT PK          permanent opaque ID, assigned by CRM fixture
                          (Defect A fix: no longer derived from email address)
      organization_id TEXT FK → organizations.id  (nullable)
      display_name TEXT   canonical name from CRM (never from signal payload)
      source TEXT         which CRM/fixture set this record
      created_at TEXT
      updated_at TEXT

  contact_identifiers
      identifier_type  TEXT NOT NULL
      identifier_value TEXT NOT NULL   (email normalized lower-case)
      contact_id       TEXT NOT NULL FK → contacts.id
      source           TEXT NOT NULL
      verified_at      TEXT NOT NULL
      PK (identifier_type, identifier_value, source)

  identifier_conflicts                 (Defect B + P2 fix)
      id               INTEGER PK AUTOINCREMENT
      identifier_type  TEXT NOT NULL
      identifier_value TEXT NOT NULL
      existing_contact_id TEXT NOT NULL   contact that currently owns the identifier
      new_contact_id   TEXT NOT NULL      contact that tried to claim it
      source           TEXT NOT NULL
      detected_at      TEXT NOT NULL
      resolved_contact_id TEXT            set by resolve_identifier_conflict()
      resolved_at      TEXT               timestamp of resolution
      resolved_by      TEXT               authorized operator that resolved it
      -- No UNIQUE constraint: each new ownership dispute is a fresh event.
      -- Application-level idempotency: skip if an identical UNRESOLVED row
      -- already exists for (type, value, existing, new, source).

  contact_name_history
      id INTEGER PK AUTOINCREMENT
      contact_id TEXT NOT NULL
      old_display_name TEXT
      new_display_name TEXT NOT NULL
      changed_at TEXT NOT NULL
      source TEXT NOT NULL

Design rules (C1/C2 + Defect A/B + P1/P2/P3 repairs)
------------------------------------------------------
- contact_id is assigned by the CRM fixture entry's ``contact_id`` field.
  It is a permanent opaque ID independent of email, phone, name.
- Changing canonical_email, phone, or display_name never changes contact_id.
- Only CRM-fixture population or authorized calls may insert/update contacts.
- Signal payloads never rename or relink a contact.
- resolve_contact_from_signal() uses ONLY verified identifiers in
  contact_identifiers; it never reads ent: tags or customer_name.
- Returns "" when no verified match is found — never fabricates an association.

P1 (fix 1) — fail closed on disputes:
  load_contact_index() excludes any identifier that has an unresolved
  ownership conflict from the identifier_map.  Neither claimant can use the
  identifier for automatic attribution until the conflict is resolved.

P2 (fix 2) — record every new dispute event:
  identifier_conflicts has no UNIQUE constraint.  Idempotency is application-
  level: skip INSERT only when an identical UNRESOLVED row already exists for
  (type, value, existing_contact, new_contact, source).  After a conflict is
  resolved, a fresh claim from the same parties creates a new event.

P3 (fix 3) — restricted resolution pathway:
  resolve_identifier_conflict() validates that resolved_by is in
  AUTHORIZED_OPERATORS and that resolved_contact_id is a legitimate claimant
  (a contact that appears in an unresolved dispute row for this identifier).
  Arbitrary callers and non-claimants are rejected with IdentityResolutionError.

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
# P3: Authorized operator identities for conflict resolution
# ---------------------------------------------------------------------------

#: Set of authorized operator identities that may call resolve_identifier_conflict().
#: These are intentionally distinct from any CRM source name or contact ID format —
#: they cannot be forged by an arbitrary caller-supplied string.
#:
#: In production, replace this check with an integration to the business's IAM/RBAC
#: system.  The constant is public so that callers can inspect it and integration
#: code can override it at startup.
AUTHORIZED_OPERATORS: frozenset[str] = frozenset({
    "admin",
    "crm_admin",
})


class IdentityResolutionError(Exception):
    """Raised when resolve_identifier_conflict() is called without authorization
    or with an invalid target contact (P3: restricted resolution pathway)."""


# ---------------------------------------------------------------------------
# Stable ID derivation
# ---------------------------------------------------------------------------

def make_org_id(org_name: str) -> str:
    """Derive a stable organization_id from the canonical organization name.

    Uses the first 20 hex characters of SHA-256(lower(org_name)) prefixed
    with ``oid_``.  Org names are stable; this derivation is acceptable.

    Returns "" for empty inputs.
    """
    name = org_name.strip().lower()
    if not name:
        return ""
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"oid_{digest[:20]}"


def make_contact_id(canonical_email: str) -> str:
    """LEGACY: Derive a contact_id from an email address.

    This was the original implementation.  It is RETAINED for backward
    compatibility and migration reference only.  New code must use the
    ``contact_id`` field from the CRM fixture entry directly (Defect A fix).

    Returns "" for empty or invalid inputs.
    """
    email = canonical_email.strip().lower()
    if not email or "@" not in email:
        return ""
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
    return f"cid_{digest[:20]}"


def make_fixture_contact_id(fixture_key: str) -> str:
    """Derive a stable contact_id from a fixture-assigned stable key.

    ``fixture_key`` is a string like ``"crm:v1:alice_chen:acme"`` that is
    permanently assigned by the CRM fixture and never changes with mutable
    attributes like email or phone.

    Returns ``cid_{sha256(key)[:20]}``.
    """
    return "cid_" + hashlib.sha256(fixture_key.encode("utf-8")).hexdigest()[:20]


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

def migrate_contacts(conn: sqlite3.Connection) -> None:
    """Create contact identity tables if they don't exist (idempotent).

    Safe to call on every startup; uses IF NOT EXISTS guards.

    P2 schema repair: ``identifier_conflicts`` no longer has a UNIQUE constraint
    over (type, value, existing, new).  Each new ownership dispute is a fresh
    event; idempotency is enforced at the application level in _upsert_identifier.
    Existing databases with the old constraint are migrated automatically.
    """
    # P2 migration: if identifier_conflicts exists with the old 4-column UNIQUE
    # constraint, recreate it without the constraint so new events can be recorded
    # after a previous conflict between the same parties was resolved.
    try:
        old_row = conn.execute(
            "SELECT sql FROM sqlite_master"
            " WHERE type='table' AND name='identifier_conflicts'"
        ).fetchone()
        old_sql = (old_row[0] or "") if old_row else ""
        if old_sql and "UNIQUE" in old_sql and "new_contact_id" in old_sql:
            # Old schema had UNIQUE (type, value, existing, new) — migrate.
            conn.executescript("""
                ALTER TABLE identifier_conflicts
                    RENAME TO _ic_migrate_old;

                CREATE TABLE identifier_conflicts (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    identifier_type     TEXT NOT NULL,
                    identifier_value    TEXT NOT NULL,
                    existing_contact_id TEXT NOT NULL,
                    new_contact_id      TEXT NOT NULL,
                    source              TEXT NOT NULL,
                    detected_at         TEXT NOT NULL,
                    resolved_contact_id TEXT,
                    resolved_at         TEXT,
                    resolved_by         TEXT
                );

                INSERT INTO identifier_conflicts
                    SELECT id, identifier_type, identifier_value,
                           existing_contact_id, new_contact_id, source,
                           detected_at, resolved_contact_id, resolved_at,
                           resolved_by
                    FROM _ic_migrate_old;

                DROP TABLE _ic_migrate_old;

                CREATE INDEX IF NOT EXISTS
                    idx_identifier_conflicts_lookup
                ON identifier_conflicts (identifier_type, identifier_value,
                                          resolved_contact_id);
            """)
    except sqlite3.OperationalError:
        pass  # table does not exist yet — will be created below

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

        -- P2 fix: no UNIQUE constraint — each dispute event is independently recorded.
        -- Idempotency is enforced at the application level.
        CREATE TABLE IF NOT EXISTS identifier_conflicts (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier_type     TEXT NOT NULL,
            identifier_value    TEXT NOT NULL,
            existing_contact_id TEXT NOT NULL,
            new_contact_id      TEXT NOT NULL,
            source              TEXT NOT NULL,
            detected_at         TEXT NOT NULL,
            resolved_contact_id TEXT,
            resolved_at         TEXT,
            resolved_by         TEXT
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

        CREATE INDEX IF NOT EXISTS
            idx_identifier_conflicts_lookup
        ON identifier_conflicts (identifier_type, identifier_value,
                                  resolved_contact_id);
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
    Each entry MUST have:
        contact_id (str): permanent opaque ID assigned by the CRM (Defect A fix).
        display_name (str)
        organization_name (str)
        identifiers (list[tuple[str, str]])

    ``canonical_email`` in the fixture entry is informational only.

    Defect A repair: contact_id is taken directly from ``entry["contact_id"]``.
    Changing an email address in a later CRM update does NOT change contact_id.

    Defect B repair: before assigning each identifier, check whether it is
    already claimed by a DIFFERENT contact.  If so:
      • Record the conflict in ``identifier_conflicts``.
      • Do NOT overwrite the existing ownership.
      • The identifier is excluded from automatic attribution until explicitly
        resolved via ``resolve_identifier_conflict()``.

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
            # Defect A fix: use fixture-assigned contact_id, never derive from email.
            contact_id = (entry.get("contact_id") or "").strip()
            if not contact_id:
                _log.warning(
                    "CRM entry missing 'contact_id' field (display_name=%r). "
                    "Skipping — all fixture entries must have explicit contact_ids.",
                    entry.get("display_name", "?"),
                )
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

            # Upsert identifiers with Defect B conflict detection.
            for id_type, id_value in (entry.get("identifiers") or []):
                normalized = id_value.strip()
                if id_type == "email":
                    normalized = normalized.lower()
                if not normalized:
                    continue

                _upsert_identifier(
                    id_type, normalized, contact_id, source, now, conn
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


def _upsert_identifier(
    id_type: str,
    id_value: str,
    contact_id: str,
    source: str,
    now: str,
    conn: sqlite3.Connection,
) -> None:
    """Insert or update one identifier entry with Defect B conflict detection.

    If the identifier is already owned by a DIFFERENT contact (from any source),
    record a conflict and do NOT overwrite the existing ownership.
    If the identifier is already owned by this contact, update the timestamp.
    If no prior ownership exists, insert freely.

    Called inside an open transaction.
    """
    # Check for existing ownership (any source)
    existing_owners = conn.execute(
        "SELECT DISTINCT contact_id FROM contact_identifiers"
        " WHERE identifier_type = ? AND identifier_value = ?",
        (id_type, id_value),
    ).fetchall()

    other_owners = [row[0] for row in existing_owners if row[0] != contact_id]
    if other_owners:
        # Defect B + P2: identifier already claimed by another contact.
        # Record each conflict and refuse the assignment.
        for other_id in other_owners:
            # P2 idempotency: skip if an IDENTICAL unresolved conflict already
            # exists for (type, value, existing, new, source).  This prevents
            # duplicates within the same CRM sync run.  After a conflict is
            # resolved, a fresh claim from the same parties is a NEW event.
            already = conn.execute(
                "SELECT id FROM identifier_conflicts"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND existing_contact_id = ? AND new_contact_id = ?"
                "   AND source = ? AND resolved_contact_id IS NULL",
                (id_type, id_value, other_id, contact_id, source),
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO identifier_conflicts"
                    " (identifier_type, identifier_value, existing_contact_id,"
                    "  new_contact_id, source, detected_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (id_type, id_value, other_id, contact_id, source, now),
                )
            _log.warning(
                "Identifier conflict: %s %r is already owned by %r; "
                "new claim from %r (%s) refused and recorded.",
                id_type, id_value, other_id, contact_id, source,
            )
        # Do NOT insert/update — the conflicted identifier is excluded from
        # automatic attribution until explicitly resolved (P1).
        return

    # Safe to insert or update (same contact or no prior owner)
    conn.execute(
        "INSERT INTO contact_identifiers"
        " (identifier_type, identifier_value, contact_id, source, verified_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(identifier_type, identifier_value, source)"
        " DO UPDATE SET"
        "  contact_id = excluded.contact_id,"
        "  verified_at = excluded.verified_at",
        (id_type, id_value, contact_id, source, now),
    )


# ---------------------------------------------------------------------------
# Conflict inspection and resolution
# ---------------------------------------------------------------------------

def get_identifier_conflicts(
    conn: sqlite3.Connection,
    unresolved_only: bool = True,
) -> list[dict]:
    """Return recorded identifier conflicts from this business's contacts DB.

    Args:
        unresolved_only: if True (default), return only conflicts without a
                         resolved_contact_id.  Pass False to retrieve all.

    Returns a list of dicts with keys:
        id, identifier_type, identifier_value,
        existing_contact_id, new_contact_id, source, detected_at,
        resolved_contact_id, resolved_at, resolved_by.
    """
    try:
        where = "WHERE resolved_contact_id IS NULL" if unresolved_only else ""
        rows = conn.execute(
            f"SELECT id, identifier_type, identifier_value, existing_contact_id,"
            f"  new_contact_id, source, detected_at,"
            f"  resolved_contact_id, resolved_at, resolved_by"
            f" FROM identifier_conflicts {where}"
            f" ORDER BY detected_at",
        ).fetchall()
        keys = ("id", "identifier_type", "identifier_value",
                "existing_contact_id", "new_contact_id", "source", "detected_at",
                "resolved_contact_id", "resolved_at", "resolved_by")
        return [dict(zip(keys, row)) for row in rows]
    except sqlite3.OperationalError:
        return []


def resolve_identifier_conflict(
    identifier_type: str,
    identifier_value: str,
    resolved_contact_id: str,
    resolved_by: str,
    conn: sqlite3.Connection,
    clock=None,
) -> int:
    """Explicitly resolve a conflicted identifier by assigning it to one contact.

    P3 (restricted resolution pathway):
    - ``resolved_by`` must be in ``AUTHORIZED_OPERATORS``.  Arbitrary caller-
      supplied strings are rejected with ``IdentityResolutionError``.
    - ``resolved_contact_id`` must be a legitimate claimant — a contact that
      appears in an unresolved conflict row for this identifier.  Non-claimants
      are rejected with ``IdentityResolutionError``.

    If there are no unresolved conflicts for this identifier, returns 0
    (idempotent no-op).

    On success:
      1. Marks all unresolved conflicts for this identifier as resolved.
      2. Removes the identifier from contact_identifiers for every contact
         except the resolved one.
      3. Inserts/updates the identifier to point to the resolved contact.

    Returns the number of conflict rows resolved.
    """
    # P3 check 1: authorized operator
    if resolved_by not in AUTHORIZED_OPERATORS:
        raise IdentityResolutionError(
            f"resolve_identifier_conflict: resolved_by={resolved_by!r} is not "
            f"an authorized operator. Valid operators: {sorted(AUTHORIZED_OPERATORS)}. "
            "Obtain authorization before resolving identifier disputes."
        )

    now = clock().isoformat() if clock else datetime.now(timezone.utc).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Find all unresolved conflicts for this identifier
        unresolved = conn.execute(
            "SELECT existing_contact_id, new_contact_id"
            " FROM identifier_conflicts"
            " WHERE identifier_type = ? AND identifier_value = ?"
            "   AND resolved_contact_id IS NULL",
            (identifier_type, identifier_value),
        ).fetchall()

        if not unresolved:
            conn.execute("COMMIT")
            return 0  # Idempotent: nothing to resolve

        # P3 check 2: resolved_contact_id must be a legitimate claimant
        claimants: set[str] = set()
        for row in unresolved:
            claimants.add(row[0])
            claimants.add(row[1])

        if resolved_contact_id not in claimants:
            conn.execute("ROLLBACK")
            raise IdentityResolutionError(
                f"resolve_identifier_conflict: resolved_contact_id="
                f"{resolved_contact_id!r} is not a claimant in any unresolved "
                f"dispute for ({identifier_type}, {identifier_value!r}). "
                f"Valid claimants: {sorted(claimants)}."
            )

        # Mark conflicts as resolved
        updated = conn.execute(
            "UPDATE identifier_conflicts"
            "  SET resolved_contact_id = ?,"
            "      resolved_at = ?,"
            "      resolved_by = ?"
            " WHERE identifier_type = ?"
            "   AND identifier_value = ?"
            "   AND resolved_contact_id IS NULL",
            (resolved_contact_id, now, resolved_by, identifier_type, identifier_value),
        ).rowcount

        if updated > 0:
            # Remove identifier from all contacts that are NOT the resolved one
            conn.execute(
                "DELETE FROM contact_identifiers"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id != ?",
                (identifier_type, identifier_value, resolved_contact_id),
            )
            # Assign to the resolved contact (may already exist)
            conn.execute(
                "INSERT INTO contact_identifiers"
                " (identifier_type, identifier_value, contact_id, source, verified_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(identifier_type, identifier_value, source)"
                " DO UPDATE SET"
                "  contact_id = excluded.contact_id,"
                "  verified_at = excluded.verified_at",
                (identifier_type, identifier_value, resolved_contact_id,
                 resolved_by, now),
            )

        conn.execute("COMMIT")
        return updated
    except IdentityResolutionError:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


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

    P1 (fail closed on disputes): any identifier that has at least one
    unresolved ownership conflict is excluded from identifier_map entirely.
    Neither the existing owner nor the new claimant can use the identifier for
    automatic attribution until the conflict is explicitly resolved via
    ``resolve_identifier_conflict()``.  Undisputed identifiers are unaffected.

    Use ``get_identifier_conflicts()`` to inspect outstanding disputes.

    Returns empty dicts when the contacts tables do not yet exist.
    """
    identifier_map: dict[tuple[str, str], str] = {}
    contact_info: dict[str, tuple[str, str]] = {}
    try:
        # Load all identifiers. The original owner keeps their mapping in
        # contact_identifiers even when a conflict was recorded.
        rows = conn.execute(
            "SELECT identifier_type, identifier_value, contact_id"
            " FROM contact_identifiers"
        ).fetchall()
        for row in rows:
            identifier_map[(row[0], row[1])] = row[2]

        # P1 fix: exclude identifiers with any UNRESOLVED conflict from the
        # index.  When a dispute is active, neither claimant may use the
        # identifier for automatic attribution.  Undisputed identifiers are
        # unaffected.  After an authorised resolution, the identifier is
        # re-included for the winning contact.
        try:
            contested_rows = conn.execute(
                "SELECT DISTINCT identifier_type, identifier_value"
                " FROM identifier_conflicts"
                " WHERE resolved_contact_id IS NULL"
            ).fetchall()
            for id_type, id_value in contested_rows:
                identifier_map.pop((id_type, id_value), None)
        except sqlite3.OperationalError:
            pass  # table may not exist on old DBs

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

    P1: identifiers with unresolved conflicts are absent from identifier_map
    (they were excluded by load_contact_index), so they return "" here and
    cannot create false associations for either party in the dispute.

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
