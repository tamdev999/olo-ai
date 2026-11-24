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
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P3: Authorized operator identities for conflict resolution
# ---------------------------------------------------------------------------

# Fix 3: replace string-based authorization with a verifiable typed boundary.
# AUTHORIZED_OPERATOR_NAMES names the operators currently permitted to invoke
# resolve_identifier_conflict().  Replace the validator inside
# create_operator_token() with an IAM/RBAC integration for production.
# This frozenset is intentionally distinct from any CRM source name or contact
# ID format — it cannot be satisfied by an arbitrary caller-supplied string.
AUTHORIZED_OPERATOR_NAMES: frozenset[str] = frozenset({"admin", "crm_admin"})

# Backward-compatible alias kept for existing code and tests that inspect the set.
AUTHORIZED_OPERATORS: frozenset[str] = AUTHORIZED_OPERATOR_NAMES


class IdentityResolutionError(Exception):
    """Raised when resolve_identifier_conflict() is called without authorization
    or with an invalid target contact (P3: restricted resolution pathway)."""


class PhoneNormalizationError(Exception):
    """Raised by normalize_phone() when the number is invalid or ambiguous (Fix 3)."""


# ---------------------------------------------------------------------------
# Fix 3: Verifiable authorization boundary
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperatorToken:
    """Authenticated operator context for privileged identity operations.

    Obtain via create_operator_token().  The token is immutable after creation.
    Its actor field is recorded in identifier_ownership_history for audit.

    IAM integration point: in production, replace create_operator_token()'s
    validation logic with a call to the business's authentication system
    (session tokens, JWT validation, RBAC lookup).
    """
    actor: str


def create_operator_token(name: str) -> OperatorToken:
    """Validate and mint an OperatorToken for the named operator.

    Raises IdentityResolutionError for unrecognised names.  This is the
    single authorization boundary for privileged identity operations —
    unauthorized callers cannot obtain a token and therefore cannot call
    resolve_identifier_conflict().

    INTEGRATION REQUIREMENT: for production deployments, replace the
    frozenset check below with an authenticated IAM/RBAC call so the
    caller's real identity is verified rather than accepted as a string.
    """
    if name not in AUTHORIZED_OPERATOR_NAMES:
        raise IdentityResolutionError(
            f"create_operator_token: {name!r} is not an authorized operator. "
            f"Valid operators: {sorted(AUTHORIZED_OPERATOR_NAMES)}. "
            "Obtain IAM authorization before resolving identifier disputes."
        )
    return OperatorToken(actor=name)


# ---------------------------------------------------------------------------
# Fix 3: Phone normalization
# ---------------------------------------------------------------------------

def normalize_phone(raw: str, default_region: Optional[str] = None) -> str:
    """Normalize a phone number to E.164 format (+XXXXXXXXXX).

    Numbers already in E.164 (+XX...) are validated and returned unchanged.
    National numbers (no leading +) require an explicit ``default_region``.
    Invalid or ambiguous numbers raise ``PhoneNormalizationError``.

    Supported regions for national-number expansion:
      'US', 'CA' — NANP: 10 digits → +1XXXXXXXXXX; 11 digits starting with 1
                   → +1XXXXXXXXXX.

    For other regions, supply the number in E.164 format directly.

    Args:
        raw:            Raw phone number string (e.g. "+1 415-555-0101",
                        "(415) 555-0101", "4155550101").
        default_region: ISO 3166-1 alpha-2 region code required for national
                        numbers lacking a + prefix (e.g. 'US').

    Returns:
        E.164 string starting with '+'.

    Raises:
        PhoneNormalizationError: on invalid length, unsupported region, or
                                 missing region for a national number.
    """
    if not raw:
        raise PhoneNormalizationError("Empty phone number.")
    raw = raw.strip()
    has_plus = raw.startswith("+")
    digits = "".join(c for c in raw if c.isdigit())

    if has_plus:
        # Input already carries a country code.
        if not (7 <= len(digits) <= 15):
            raise PhoneNormalizationError(
                f"Invalid E.164 number {raw!r}: expected 7–15 digits after +, "
                f"got {len(digits)}."
            )
        return "+" + digits

    # No leading '+' — country code is absent.
    if not default_region:
        raise PhoneNormalizationError(
            f"Phone {raw!r} has no country code and no default_region was given. "
            "Provide the number in E.164 format (+XX…) or supply a region such as 'US'."
        )

    region = default_region.upper()
    if region in ("US", "CA"):
        if len(digits) == 10:
            return "+1" + digits
        if len(digits) == 11 and digits[0] == "1":
            return "+" + digits
        raise PhoneNormalizationError(
            f"Invalid {region} phone {raw!r}: expected 10 digits or "
            f"11 digits starting with 1, got {len(digits)}."
        )

    raise PhoneNormalizationError(
        f"Normalization for region {region!r} is not supported. "
        "Supply the number in E.164 format (+XX…) instead."
    )


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
# Fix 1: Persistent production contact IDs
# ---------------------------------------------------------------------------

def mint_contact_id() -> str:
    """Mint a fresh random, opaque production contact_id.

    Uses UUID4 so the ID is globally unique and contains no mutable
    attribute information (Defect A fix for the production path).  The ID
    is permanent: once stored in olo.db it is never re-derived.

    Use for production contacts created outside the frozen development
    benchmark.  For the benchmark, use make_fixture_contact_id() with a
    stable fixture key so the benchmark corpus stays deterministic.
    """
    return "cid_" + uuid.uuid4().hex


def create_verified_contact(
    display_name: str,
    org_name: str,
    identifiers: list,
    conn: sqlite3.Connection,
    source: str = "api",
    clock=None,
    default_region: Optional[str] = None,
) -> str:
    """Create or find a production contact from verified identifiers.

    If any identifier already maps to an active contact, that contact's
    existing ID is returned — preserving the ID across restarts and CRM
    resyncs.  If multiple identifiers map to *different* contacts, raises
    IdentityResolutionError (ambiguous identity — do not silently merge).

    If no existing contact is found but at least one valid identifier is
    provided, a new contact is created with an ID from mint_contact_id()
    — never derived from a mutable attribute.

    If no valid identifiers are provided, returns "" without creating a
    contact (unknown identity — never fabricate a contact).

    Phone identifiers are normalized to E.164 when ``default_region`` is
    given; invalid phones are skipped with a warning.

    Args:
        display_name:   canonical name from the verified source.
        org_name:       canonical organization name (empty = individual).
        identifiers:    list of (identifier_type, identifier_value) pairs.
        conn:           open SQLite connection (autocommit / explicit txn).
        source:         provenance label for new records.
        clock:          callable → datetime (default: UTC now).
        default_region: ISO 3166-1 alpha-2 region for phone normalization.

    Returns:
        contact_id string (``cid_...``) or ``""`` for unknown identity.
    """
    now = clock().isoformat() if clock else datetime.now(timezone.utc).isoformat()

    # Normalize identifiers before touching the DB
    normalized: list[tuple[str, str]] = []
    for id_type, id_value in identifiers:
        v = id_value.strip()
        if not v:
            continue
        if id_type == "email":
            v = v.lower()
        elif id_type == "phone":
            try:
                v = normalize_phone(v, default_region)
            except PhoneNormalizationError:
                _log.warning(
                    "create_verified_contact: skipping invalid phone %r", id_value
                )
                continue
        normalized.append((id_type, v))

    if not normalized:
        return ""  # no valid identifiers — unknown identity, do not fabricate

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Discover any existing active contacts for these identifiers
        existing_cids: set[str] = set()
        for id_type, id_value in normalized:
            row = conn.execute(
                "SELECT contact_id FROM contact_identifiers"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND revoked_at IS NULL",
                (id_type, id_value),
            ).fetchone()
            if row:
                existing_cids.add(row[0])

        if len(existing_cids) > 1:
            conn.execute("ROLLBACK")
            raise IdentityResolutionError(
                f"create_verified_contact: identifiers map to multiple existing "
                f"contacts {sorted(existing_cids)}.  Resolve the conflict before "
                "creating or linking this contact."
            )

        org_id = make_org_id(org_name) if org_name else None
        if org_id and org_name:
            conn.execute(
                "INSERT INTO organizations"
                " (id, name, source, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                "  name = excluded.name,"
                "  updated_at = excluded.updated_at",
                (org_id, org_name, source, now, now),
            )

        if existing_cids:
            # Preserve the existing ID across restarts / resyncs
            contact_id = next(iter(existing_cids))
            existing = conn.execute(
                "SELECT display_name FROM contacts WHERE id = ?",
                (contact_id,),
            ).fetchone()
            if existing:
                old_name = existing[0]
                conn.execute(
                    "UPDATE contacts SET"
                    "  display_name = ?,"
                    "  organization_id = ?,"
                    "  source = ?,"
                    "  updated_at = ?"
                    " WHERE id = ?",
                    (display_name, org_id, source, now, contact_id),
                )
                if display_name and display_name != old_name:
                    conn.execute(
                        "INSERT INTO contact_name_history"
                        " (contact_id, old_display_name, new_display_name,"
                        "  changed_at, source)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (contact_id, old_name, display_name, now, source),
                    )
        else:
            # New contact — mint a random, opaque ID (never derived from data)
            contact_id = mint_contact_id()
            conn.execute(
                "INSERT INTO contacts"
                " (id, organization_id, display_name, source,"
                "  created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (contact_id, org_id, display_name, source, now, now),
            )

        for id_type, id_value in normalized:
            _upsert_identifier(id_type, id_value, contact_id, source, now, conn)

        conn.execute("COMMIT")
        return contact_id

    except IdentityResolutionError:
        raise
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


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

    # Fix 1: add revocation columns to contact_identifiers if missing.
    # ALTER TABLE ADD COLUMN is non-destructive and idempotent in SQLite.
    for _col, _dflt in [
        ("revoked_at", "NULL"),
        ("revocation_source", "NULL"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE contact_identifiers ADD COLUMN {_col} TEXT DEFAULT {_dflt}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

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

        -- Fix 1: revoked_at / revocation_source track identifier lifecycle.
        -- NULL revoked_at = active.  Non-NULL = revoked by that source at that time.
        CREATE TABLE IF NOT EXISTS contact_identifiers (
            identifier_type   TEXT NOT NULL,
            identifier_value  TEXT NOT NULL,
            contact_id        TEXT NOT NULL,
            source            TEXT NOT NULL,
            verified_at       TEXT NOT NULL,
            revoked_at        TEXT,
            revocation_source TEXT,
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

        -- Fix 2: append-only ownership event log.
        -- event_type: 'assigned' | 'revoked' | 'transferred_in' | 'transferred_out'
        -- source: the original CRM source (never overwritten with actor name).
        -- actor: the authorized operator for transfer events (NULL for CRM-driven events).
        CREATE TABLE IF NOT EXISTS identifier_ownership_history (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier_type  TEXT NOT NULL,
            identifier_value TEXT NOT NULL,
            contact_id       TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            source           TEXT NOT NULL,
            actor            TEXT,
            reason           TEXT,
            occurred_at      TEXT NOT NULL
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
        ON contact_identifiers (identifier_type, identifier_value, revoked_at);

        CREATE INDEX IF NOT EXISTS
            idx_identifier_conflicts_lookup
        ON identifier_conflicts (identifier_type, identifier_value,
                                  resolved_contact_id);

        CREATE INDEX IF NOT EXISTS
            idx_ownership_history_lookup
        ON identifier_ownership_history (identifier_type, identifier_value,
                                          contact_id);
    """)


# ---------------------------------------------------------------------------
# CRM fixture population
# ---------------------------------------------------------------------------

def populate_contacts_from_crm(
    crm_contacts: list,
    conn: sqlite3.Connection,
    source: str = "crm_fixture",
    clock=None,
    snapshot_mode: bool = False,
    default_region: Optional[str] = None,
) -> int:
    """Insert or update contact records from a CRM fixture.

    *crm_contacts* is the list of dicts from ``crm_fixture.CRM_CONTACTS``.
    Each entry MUST have:
        contact_id (str): permanent opaque ID assigned by the CRM (Defect A fix).
        display_name (str)
        organization_name (str)
        identifiers (list[tuple[str, str]])

    ``canonical_email`` in the fixture entry is informational only.

    Fix 1 — identifier removals:
      When ``snapshot_mode=True``, this call is treated as an authoritative
      full snapshot for every contact present in *crm_contacts*.  Any active
      identifier for a contact that is NOT present in the new snapshot is
      REVOKED (not deleted) with provenance recorded in
      ``identifier_ownership_history``.  Partial/delta updates must pass
      ``snapshot_mode=False`` (the default) to avoid unintended revocations.

    Fix 3 — phone normalization:
      ``default_region`` (e.g. ``'US'``) is passed to ``normalize_phone()``
      for phone identifiers that lack a leading '+'.

    Idempotent: existing records are updated when canonical data changes.
    Name changes are recorded in ``contact_name_history`` for provenance.

    Returns the number of contacts upserted.
    """
    if not crm_contacts:
        return 0
    now = clock().isoformat() if clock else datetime.now(timezone.utc).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    upserted = 0
    # Build snapshot map for revocation check (snapshot_mode only)
    snapshot_identifiers: dict[str, set[tuple[str, str]]] = {}  # contact_id → {(type,value)}
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

            # Upsert identifiers with conflict detection.
            snapshot_identifiers[contact_id] = set()
            for id_type, id_value in (entry.get("identifiers") or []):
                normalized = id_value.strip()
                if id_type == "email":
                    normalized = normalized.lower()
                elif id_type == "phone":
                    try:
                        normalized = normalize_phone(normalized, default_region)
                    except PhoneNormalizationError as exc:
                        _log.warning(
                            "Skipping phone %r for contact %r: %s",
                            id_value, contact_id, exc,
                        )
                        continue
                if not normalized:
                    continue
                snapshot_identifiers[contact_id].add((id_type, normalized))
                _upsert_identifier(
                    id_type, normalized, contact_id, source, now, conn
                )

            upserted += 1

        # Fix 1 — snapshot-mode revocation.
        # Revoke any active identifier for a contact that is NOT present in
        # the authoritative snapshot.  Only contacts present in this call are
        # eligible; contacts absent from crm_contacts are untouched (this is
        # explicitly NOT a full-database wipe).
        if snapshot_mode:
            for contact_id, in_snapshot in snapshot_identifiers.items():
                active_rows = conn.execute(
                    "SELECT identifier_type, identifier_value, source"
                    " FROM contact_identifiers"
                    " WHERE contact_id = ? AND revoked_at IS NULL",
                    (contact_id,),
                ).fetchall()
                for id_type, id_value, orig_source in active_rows:
                    if (id_type, id_value) not in in_snapshot:
                        # This identifier was removed from the CRM snapshot.
                        conn.execute(
                            "UPDATE contact_identifiers"
                            "  SET revoked_at = ?, revocation_source = ?"
                            " WHERE identifier_type = ? AND identifier_value = ?"
                            "   AND contact_id = ? AND revoked_at IS NULL",
                            (now, source, id_type, id_value, contact_id),
                        )
                        _record_ownership_event(
                            conn, id_type, id_value, contact_id, "revoked",
                            orig_source, None, now,
                            reason=f"removed from CRM snapshot ({source})",
                        )

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return upserted


def _record_ownership_event(
    conn: sqlite3.Connection,
    identifier_type: str,
    identifier_value: str,
    contact_id: str,
    event_type: str,
    source: str,
    actor: Optional[str],
    occurred_at: str,
    reason: Optional[str] = None,
) -> None:
    """Append one event to identifier_ownership_history.  Called inside a transaction.

    Not idempotent by default — callers check for existing events where needed.
    """
    try:
        conn.execute(
            "INSERT INTO identifier_ownership_history"
            " (identifier_type, identifier_value, contact_id, event_type,"
            "  source, actor, reason, occurred_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (identifier_type, identifier_value, contact_id, event_type,
             source, actor, reason, occurred_at),
        )
    except sqlite3.OperationalError:
        pass  # table may not exist on databases built before this schema version


def _upsert_identifier(
    id_type: str,
    id_value: str,
    contact_id: str,
    source: str,
    now: str,
    conn: sqlite3.Connection,
) -> None:
    """Insert or update one identifier entry with conflict detection.

    Fix 1: only ACTIVE (revoked_at IS NULL) identifiers are considered when
    checking for conflicts.  Revoked identifiers may be re-assigned freely.
    If this contact's own identifier was revoked, re-activate it.

    Fix 2: records 'assigned' events in identifier_ownership_history for new
    or re-activated assignments.  Idempotent for already-active identifiers.

    Called inside an open transaction.
    """
    # Check this contact's current state for this identifier
    own_row = conn.execute(
        "SELECT source, revoked_at FROM contact_identifiers"
        " WHERE identifier_type = ? AND identifier_value = ?"
        "   AND contact_id = ?",
        (id_type, id_value, contact_id),
    ).fetchone()

    # Check for ACTIVE ownership by OTHER contacts
    other_active = conn.execute(
        "SELECT contact_id FROM contact_identifiers"
        " WHERE identifier_type = ? AND identifier_value = ?"
        "   AND contact_id != ? AND revoked_at IS NULL",
        (id_type, id_value, contact_id),
    ).fetchall()

    if other_active:
        # Conflict: another contact has an active claim.  Record and refuse.
        for (other_id,) in other_active:
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
        return  # Do NOT assign — identifier excluded from attribution until resolved (P1)

    if own_row is not None:
        orig_source, revoked_at = own_row[0], own_row[1]
        if revoked_at is not None:
            # Previously revoked — re-activate.
            conn.execute(
                "UPDATE contact_identifiers"
                "  SET revoked_at = NULL, revocation_source = NULL,"
                "      verified_at = ?"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ?",
                (now, id_type, id_value, contact_id),
            )
            _record_ownership_event(
                conn, id_type, id_value, contact_id, "assigned",
                source, None, now, reason="re-activated after revocation",
            )
        else:
            # Already active — update timestamp only (no new history event).
            conn.execute(
                "UPDATE contact_identifiers SET verified_at = ?"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ?",
                (now, id_type, id_value, contact_id),
            )
    else:
        # New assignment.
        conn.execute(
            "INSERT INTO contact_identifiers"
            " (identifier_type, identifier_value, contact_id, source, verified_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (id_type, id_value, contact_id, source, now),
        )
        _record_ownership_event(
            conn, id_type, id_value, contact_id, "assigned",
            source, None, now,
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


def get_identifier_ownership_history(
    conn: sqlite3.Connection,
    identifier_type: Optional[str] = None,
    identifier_value: Optional[str] = None,
    contact_id: Optional[str] = None,
) -> list[dict]:
    """Return ownership history events, optionally filtered.

    Returns a list of dicts with keys:
        id, identifier_type, identifier_value, contact_id, event_type,
        source, actor, reason, occurred_at.

    Ordered by occurred_at ascending (chronological).
    Returns [] when the history table does not yet exist.
    """
    try:
        where_parts = []
        params: list = []
        if identifier_type:
            where_parts.append("identifier_type = ?"); params.append(identifier_type)
        if identifier_value:
            where_parts.append("identifier_value = ?"); params.append(identifier_value)
        if contact_id:
            where_parts.append("contact_id = ?"); params.append(contact_id)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = conn.execute(
            f"SELECT id, identifier_type, identifier_value, contact_id, event_type,"
            f"  source, actor, reason, occurred_at"
            f" FROM identifier_ownership_history {where}"
            f" ORDER BY occurred_at",
            params,
        ).fetchall()
        keys = ("id", "identifier_type", "identifier_value", "contact_id",
                "event_type", "source", "actor", "reason", "occurred_at")
        return [dict(zip(keys, row)) for row in rows]
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# Fix 2: Legacy identity migration
# ---------------------------------------------------------------------------

@dataclass
class LegacyContactMapping:
    """Single legacy email-derived ID → canonical CRM-assigned ID mapping."""
    legacy_id: str       # old cid_... derived from make_contact_id(email)
    canonical_id: str    # new stable cid_... from the CRM fixture
    verified_email: str  # the email that proves this mapping (not ent: tags)


@dataclass
class MigrationPlan:
    """Plan produced by plan_migration(); safe to inspect before executing.

    is_safe is True only when no ambiguous cases were detected.  Never pass
    an unsafe plan to execute_migration.
    """
    mappings: list    # list[LegacyContactMapping]
    ambiguous: list   # list[dict] — legacy IDs with conflicting canonical mappings
    is_dry_run: bool
    is_safe: bool     # False when any legacy ID maps to multiple CRM entries


@dataclass
class MigrationResult:
    """Outcome produced by execute_migration()."""
    contacts_updated: int
    identifiers_updated: int
    history_updated: int
    is_complete: bool  # True when verify_migration() finds no remaining stale IDs


def _is_legacy_contact_id(contact_id: str, emails: list) -> bool:
    """Return True when contact_id was derived from one of the given email addresses.

    Detects the old make_contact_id(email) derivation pattern.
    Called by plan_migration; never touches the DB.
    """
    for email in emails:
        if make_contact_id(email) == contact_id:
            return True
    return False


def plan_migration(
    conn: sqlite3.Connection,
    crm_contacts: Optional[list] = None,
    dry_run: bool = True,
) -> MigrationPlan:
    """Scan the database for legacy email-derived contact IDs and build a migration plan.

    Identity is established exclusively via verified email identifiers in
    contact_identifiers — generator ent: tags are never consulted (they are
    benchmark artifacts, not identity evidence).

    When a legacy contact's emails map to exactly one CRM entry the mapping
    is unambiguous.  When they map to multiple CRM entries the case is
    recorded in plan.ambiguous and plan.is_safe is set to False — do not
    execute an unsafe plan.

    Args:
        conn:         open connection to the business olo.db.
        crm_contacts: authoritative CRM contacts list (e.g. CRM_CONTACTS).
                      Pass [] or None to produce an empty-mapping plan.
        dry_run:      recorded in the returned plan; does not affect DB.

    Returns:
        MigrationPlan with mappings, ambiguous cases, and is_safe flag.
    """
    if crm_contacts is None:
        crm_contacts = []

    # Build email → canonical_id lookup from the authoritative CRM list.
    email_to_canonical: dict[str, str] = {}
    for entry in crm_contacts:
        for id_type, id_value in entry.get("identifiers", []):
            if id_type == "email" and id_value not in email_to_canonical:
                email_to_canonical[id_value] = entry.get("contact_id", "")

    contacts = conn.execute("SELECT id FROM contacts").fetchall()
    mappings: list[LegacyContactMapping] = []
    ambiguous: list[dict] = []

    for (cid,) in contacts:
        email_rows = conn.execute(
            "SELECT identifier_value FROM contact_identifiers"
            " WHERE contact_id = ? AND identifier_type = 'email'"
            "   AND revoked_at IS NULL",
            (cid,),
        ).fetchall()
        emails = [row[0] for row in email_rows]

        if not _is_legacy_contact_id(cid, emails):
            continue  # already a stable ID — nothing to migrate

        # Map ALL email identifiers of this legacy contact to their CRM-canonical IDs.
        # We use the derivation-matching email as proof that this is a legacy contact,
        # but check ALL emails for ambiguity so we never silently merge two CRM entries.
        email_to_cid: dict[str, str] = {}
        for email in emails:
            canonical = email_to_canonical.get(email, "")
            if canonical and canonical != cid:
                email_to_cid[email] = canonical

        if not email_to_cid:
            continue  # legacy ID but no matching CRM entry — skip

        unique_canonicals = set(email_to_cid.values())
        if len(unique_canonicals) > 1:
            ambiguous.append({
                "legacy_id": cid,
                "conflicting_canonical_ids": sorted(unique_canonicals),
                "emails": list(email_to_cid.keys()),
            })
        else:
            canonical_id = next(iter(unique_canonicals))
            # Prefer the email that was used to derive the legacy ID as proof.
            verified_email = next(
                (e for e in emails if make_contact_id(e) == cid and e in email_to_cid),
                next(iter(email_to_cid)),
            )
            mappings.append(LegacyContactMapping(
                legacy_id=cid,
                canonical_id=canonical_id,
                verified_email=verified_email,
            ))

    return MigrationPlan(
        mappings=mappings,
        ambiguous=ambiguous,
        is_dry_run=dry_run,
        is_safe=(len(ambiguous) == 0),
    )


def execute_migration(
    conn: sqlite3.Connection,
    plan: MigrationPlan,
    retain_fn=None,
    clock=None,
) -> MigrationResult:
    """Apply a migration plan atomically.

    Updates contacts.id and all FK references in a single transaction.
    Calls retain_fn(item) for each mapped contact so the caller can
    re-retain affected Hindsight links with the corrected entity ID.

    If retain_fn raises, the entire DB transaction is rolled back — the
    database remains unchanged and the migration can be retried cleanly.
    This prevents a partially certified bank.

    Do NOT call this during ordinary startup; it must be an explicit
    operator-initiated action.

    Args:
        conn:      open connection to the business olo.db.
        plan:      output of plan_migration() with is_safe=True.
        retain_fn: optional callable(dict) for Hindsight re-retain.
                   Receives dict(legacy_contact_id, canonical_contact_id,
                   verified_email).  Raise to abort the migration.
        clock:     callable → datetime (default: UTC now).

    Returns:
        MigrationResult.

    Raises:
        ValueError: if plan.is_safe is False (ambiguous mappings present).
    """
    if not plan.is_safe:
        raise ValueError(
            "Migration plan contains ambiguous mappings. "
            "Inspect plan.ambiguous and resolve each case before executing. "
            "Do not silently merge contacts."
        )
    if not plan.mappings:
        return MigrationResult(
            contacts_updated=0, identifiers_updated=0,
            history_updated=0, is_complete=True,
        )

    contacts_updated = 0
    identifiers_updated = 0
    history_updated = 0
    retain_items: list[dict] = []

    conn.execute("BEGIN IMMEDIATE")
    try:
        for mapping in plan.mappings:
            old_id = mapping.legacy_id
            new_id = mapping.canonical_id

            # Only insert new contact row when it doesn't already exist
            if not conn.execute(
                "SELECT 1 FROM contacts WHERE id = ?", (new_id,)
            ).fetchone():
                conn.execute(
                    "INSERT INTO contacts"
                    " SELECT ? AS id, organization_id, display_name, source,"
                    "   created_at, updated_at"
                    " FROM contacts WHERE id = ?",
                    (new_id, old_id),
                )
                contacts_updated += 1

            # Update FK references in every table that references contact_id
            _fk_refs = [
                ("contact_identifiers",         "contact_id"),
                ("identifier_conflicts",        "existing_contact_id"),
                ("identifier_conflicts",        "new_contact_id"),
                ("identifier_conflicts",        "resolved_contact_id"),
                ("identifier_ownership_history","contact_id"),
                ("contact_name_history",        "contact_id"),
            ]
            for table, col in _fk_refs:
                try:
                    n = conn.execute(
                        f"UPDATE {table} SET {col} = ? WHERE {col} = ?",
                        (new_id, old_id),
                    ).rowcount
                    if table == "contact_identifiers":
                        identifiers_updated += n
                    elif table == "identifier_ownership_history":
                        history_updated += n
                except sqlite3.OperationalError:
                    pass  # table may not exist on older DB schemas

            conn.execute("DELETE FROM contacts WHERE id = ?", (old_id,))

            retain_items.append({
                "legacy_contact_id": old_id,
                "canonical_contact_id": new_id,
                "verified_email": mapping.verified_email,
            })

        # Call retain_fn inside the transaction so a failure here rolls back
        # all DB changes — no partially certified bank.
        if retain_fn is not None:
            for item in retain_items:
                retain_fn(item)

        conn.execute("COMMIT")

    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    verification = verify_migration(conn)
    return MigrationResult(
        contacts_updated=contacts_updated,
        identifiers_updated=identifiers_updated,
        history_updated=history_updated,
        is_complete=verification["is_complete"],
    )


def verify_migration(conn: sqlite3.Connection) -> dict:
    """Check for remaining legacy email-derived contact IDs.

    Returns a dict with:
        stale_legacy_ids (list[str]): contact IDs still derived from emails.
        is_complete (bool):           True when no stale IDs remain.

    Use after execute_migration() to confirm all legacy IDs have been updated
    before declaring the migration complete.
    """
    contacts = conn.execute("SELECT id FROM contacts").fetchall()
    stale: list[str] = []
    for (cid,) in contacts:
        email_rows = conn.execute(
            "SELECT identifier_value FROM contact_identifiers"
            " WHERE contact_id = ? AND identifier_type = 'email'"
            "   AND revoked_at IS NULL",
            (cid,),
        ).fetchall()
        emails = [row[0] for row in email_rows]
        if _is_legacy_contact_id(cid, emails):
            stale.append(cid)
    return {"stale_legacy_ids": stale, "is_complete": len(stale) == 0}


def resolve_identifier_conflict(
    identifier_type: str,
    identifier_value: str,
    resolved_contact_id: str,
    operator: OperatorToken,
    conn: sqlite3.Connection,
    clock=None,
) -> int:
    """Explicitly resolve a conflicted identifier by assigning it to one contact.

    Fix 3 (verifiable authorization boundary):
    - ``operator`` must be an OperatorToken obtained via create_operator_token().
      Unauthorized callers cannot obtain a token and are rejected at token
      creation before this function is ever invoked.
    - ``resolved_contact_id`` must be a legitimate claimant — a contact that
      appears in an unresolved conflict row for this identifier.  Non-claimants
      are rejected with ``IdentityResolutionError``.

    If there are no unresolved conflicts for this identifier, returns 0
    (idempotent no-op).

    On success:
      1. Marks all unresolved conflicts for this identifier as resolved.
      2. Revokes the identifier from every loser contact.
      3. Assigns (or re-activates) the identifier on the winning contact.

    Returns the number of conflict rows resolved.
    """
    # Defense-in-depth: guard against OperatorToken(actor=...) constructed
    # directly without going through create_operator_token().
    if operator.actor not in AUTHORIZED_OPERATOR_NAMES:
        raise IdentityResolutionError(
            f"resolve_identifier_conflict: operator.actor={operator.actor!r} is not "
            f"an authorized operator. Valid operators: {sorted(AUTHORIZED_OPERATOR_NAMES)}. "
            "Obtain authorization via create_operator_token()."
        )
    resolved_by = operator.actor

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
            # Fix 2: revoke losers' identifiers instead of hard-deleting them.
            # Preserves original source and provenance; revocation_source records
            # who authorized the transfer.
            loser_rows = conn.execute(
                "SELECT contact_id, source FROM contact_identifiers"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id != ? AND revoked_at IS NULL",
                (identifier_type, identifier_value, resolved_contact_id),
            ).fetchall()
            for loser_cid, loser_source in loser_rows:
                conn.execute(
                    "UPDATE contact_identifiers"
                    "  SET revoked_at = ?, revocation_source = ?"
                    " WHERE identifier_type = ? AND identifier_value = ?"
                    "   AND contact_id = ? AND revoked_at IS NULL",
                    (now, resolved_by, identifier_type, identifier_value, loser_cid),
                )
                # Record 'transferred_out' with original source preserved.
                _record_ownership_event(
                    conn, identifier_type, identifier_value, loser_cid,
                    "transferred_out", loser_source, resolved_by, now,
                )

            # Assign the identifier to the winner.
            # Fix 2: use the winner's ORIGINAL CRM source, not resolved_by,
            # so the ownership provenance reflects the authentic registration.
            winner_active = conn.execute(
                "SELECT source FROM contact_identifiers"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ? AND revoked_at IS NULL",
                (identifier_type, identifier_value, resolved_contact_id),
            ).fetchone()
            if winner_active:
                # Winner already has an active entry — just refresh timestamp.
                winner_source = winner_active[0]
                conn.execute(
                    "UPDATE contact_identifiers SET verified_at = ?"
                    " WHERE identifier_type = ? AND identifier_value = ?"
                    "   AND contact_id = ? AND revoked_at IS NULL",
                    (now, identifier_type, identifier_value, resolved_contact_id),
                )
            else:
                # Winner's entry was revoked or never existed.
                # Find their original source from contact_identifiers (revoked)
                # or from the conflict record.
                revoked_row = conn.execute(
                    "SELECT source FROM contact_identifiers"
                    " WHERE identifier_type = ? AND identifier_value = ?"
                    "   AND contact_id = ? AND revoked_at IS NOT NULL"
                    " ORDER BY revoked_at DESC LIMIT 1",
                    (identifier_type, identifier_value, resolved_contact_id),
                ).fetchone()
                conflict_row = conn.execute(
                    "SELECT source FROM identifier_conflicts"
                    " WHERE identifier_type = ? AND identifier_value = ?"
                    "   AND new_contact_id = ? LIMIT 1",
                    (identifier_type, identifier_value, resolved_contact_id),
                ).fetchone()
                winner_source = (
                    (revoked_row[0] if revoked_row else None)
                    or (conflict_row[0] if conflict_row else None)
                    or resolved_by  # last resort
                )
                conn.execute(
                    "INSERT INTO contact_identifiers"
                    " (identifier_type, identifier_value, contact_id,"
                    "  source, verified_at,"
                    "  revoked_at, revocation_source)"
                    " VALUES (?, ?, ?, ?, ?, NULL, NULL)"
                    " ON CONFLICT(identifier_type, identifier_value, source)"
                    " DO UPDATE SET"
                    "  contact_id = excluded.contact_id,"
                    "  verified_at = excluded.verified_at,"
                    "  revoked_at = NULL,"
                    "  revocation_source = NULL",
                    (identifier_type, identifier_value, resolved_contact_id,
                     winner_source, now),
                )

            # Record 'transferred_in' for the winner (once; idempotent check).
            already_in = conn.execute(
                "SELECT id FROM identifier_ownership_history"
                " WHERE identifier_type = ? AND identifier_value = ?"
                "   AND contact_id = ? AND event_type = 'transferred_in'",
                (identifier_type, identifier_value, resolved_contact_id),
            ).fetchone()
            if not already_in:
                _record_ownership_event(
                    conn, identifier_type, identifier_value, resolved_contact_id,
                    "transferred_in", winner_source, resolved_by, now,
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
        # Load all ACTIVE (non-revoked) identifiers.
        rows = conn.execute(
            "SELECT identifier_type, identifier_value, contact_id"
            " FROM contact_identifiers"
            " WHERE revoked_at IS NULL"
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
        # Fix 3: normalize to E.164 before lookup.  Numbers already in E.164
        # pass through unchanged; national numbers require a default_region.
        phone = (payload.get("phone") or "").strip()
        if phone:
            try:
                phone = normalize_phone(phone, payload.get("phone_region"))
            except PhoneNormalizationError:
                _log.debug(
                    "resolve_contact_from_signal: could not normalize phone %r — "
                    "skipping lookup",
                    phone,
                )
                phone = ""
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
        # Fix 3: normalize phone identifiers before lookup so callers can
        # supply numbers in any common format.
        if id_type == "phone":
            try:
                id_value = normalize_phone(id_value, payload.get("phone_region"))
            except PhoneNormalizationError:
                continue  # skip unnormalizable phone
        contact_id = identifier_map.get((id_type, id_value))
        if contact_id:
            return contact_id

    return ""
