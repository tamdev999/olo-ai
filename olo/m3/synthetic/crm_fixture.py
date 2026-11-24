"""olo/m3/synthetic/crm_fixture.py — Synthetic CRM source fixture.

Represents what a business CRM system would export: canonical customer records
with verified contact identifiers.

IMPORTANT: This module is intentionally independent of ``olo.m3.synthetic.generator``
and of the benchmark labels in ENTITIES.  It represents the *source of truth* that
would exist in a real CRM — not the benchmark answer key.

The CRM fixture is the ONLY authoritative source for customer identity in the
development benchmark.  Production deployments replace this with their actual
CRM integration (Salesforce, HubSpot, Zoho, etc.).

Design rules (C1/C2 canonical identity model, Defect A repair):
- Each record has a stable ``contact_id`` assigned by the CRM fixture itself.
  This ID is derived from a stable fixture key, NOT from a mutable attribute
  like an email address or phone number.  It never changes if the customer
  changes their email, phone or name.
- ``canonical_email`` is now documentation-only: the email used when the
  contact was first registered.  It does NOT determine the contact_id.
- Multiple verified identifiers (email addresses, phone numbers, billing IDs)
  map to the same contact_id via contact_identifiers.
- Two employees of the same organization share an ``organization_name``.
- This fixture does NOT import ENTITIES from the generator.
- Fixture-assigned IDs are computed deterministically from stable keys and
  hardcoded so three independent builds always yield identical contact_ids.

Stable key format: "crm:v1:<person_key>:<org_abbrev>"
ID computation: ``cid_`` + sha256(stable_key)[:20] hex chars.
"""
from __future__ import annotations

import hashlib
from typing import Any


# ---------------------------------------------------------------------------
# Stable ID helpers
# ---------------------------------------------------------------------------

def _make_stable_cid(fixture_key: str) -> str:
    """Compute a stable contact_id from a fixture-assigned key string.

    ``fixture_key`` never changes once assigned; the resulting contact_id
    is therefore independent of mutable attributes (email, phone, name).
    """
    return "cid_" + hashlib.sha256(fixture_key.encode()).hexdigest()[:20]


# Pre-computed stable contact IDs — hardcoded for reproducibility.
# To verify: _make_stable_cid("crm:v1:<key>")
_ALICE_CID = _make_stable_cid("crm:v1:alice_chen:acme")    # cid_64c1dd94041b33ab9245
_BOB_CID   = _make_stable_cid("crm:v1:bob_torres:beta")    # cid_8baee186a88a5a300f68
_CARLA_CID = _make_stable_cid("crm:v1:carla_osei:gamma")   # cid_ddf6dbf3cacabffdb5ae
_DAN_CID   = _make_stable_cid("crm:v1:dan_park:acme")      # cid_5afdc768573a354356cf


# ---------------------------------------------------------------------------
# CRM contacts
# ---------------------------------------------------------------------------

#: Each entry is a dict with:
#:   contact_id (str): permanent opaque ID assigned by the CRM fixture.
#:                     Never changes.  Independent of email/phone/name.
#:   canonical_email (str): the email used when this contact was first
#:                     registered (documentation; not used to derive contact_id).
#:   display_name (str): verified canonical name from the CRM.
#:   organization_name (str): canonical organization (empty string = individual).
#:   identifiers (list[tuple[str, str]]): (identifier_type, identifier_value) pairs.
#:
#: Supported identifier types:
#:   "email"              — RFC 5321 email address (normalized lower-case).
#:   "phone"              — E.164 phone number.
#:   "stripe_customer_id" — Stripe cus_* identifier.
#:   "qbo_customer_id"    — QuickBooks realm:customer identifier.
CRM_CONTACTS: list[dict[str, Any]] = [
    {
        "contact_id":       _ALICE_CID,
        "canonical_email":  "alice.chen@acme-corp.example",  # original registration email
        "display_name":     "Alice Chen",
        "organization_name": "Acme Corp",
        "identifiers": [
            ("email",              "alice.chen@acme-corp.example"),
            # Second verified email address (personal) — both resolve to same contact.
            ("email",              "alice.c@gmail.example"),
            ("phone",              "+14155550101"),
            ("stripe_customer_id", "stripe:cus_acme_001"),
            ("qbo_customer_id",    "qbo:realm123:cust_001"),
        ],
    },
    {
        "contact_id":       _BOB_CID,
        "canonical_email":  "bob.torres@beta-tech.example",
        "display_name":     "Bob Torres",
        "organization_name": "Beta Tech",
        "identifiers": [
            ("email",              "bob.torres@beta-tech.example"),
            ("phone",              "+14155550202"),
            ("stripe_customer_id", "stripe:cus_beta_002"),
            ("qbo_customer_id",    "qbo:realm123:cust_002"),
        ],
    },
    {
        "contact_id":       _CARLA_CID,
        "canonical_email":  "carla.osei@gamma-retail.example",
        "display_name":     "Carla Osei",
        "organization_name": "Gamma Retail",
        "identifiers": [
            ("email",              "carla.osei@gamma-retail.example"),
            ("phone",              "+14155550303"),
            ("stripe_customer_id", "stripe:cus_gamma_003"),
            ("qbo_customer_id",    "qbo:realm123:cust_003"),
        ],
    },
    # Second Acme Corp employee — different contact_id, same organization_id.
    {
        "contact_id":       _DAN_CID,
        "canonical_email":  "dan.park@acme-corp.example",
        "display_name":     "Dan Park",
        "organization_name": "Acme Corp",
        "identifiers": [
            ("email", "dan.park@acme-corp.example"),
        ],
    },
]


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_contact_id_for_email(email: str) -> str:
    """Return the fixture-defined contact_id for the given email address.

    Checks all identifier entries of type "email".  Returns "" if the email
    is not registered in the fixture.
    """
    email_norm = email.strip().lower()
    for record in CRM_CONTACTS:
        for id_type, id_value in record.get("identifiers", []):
            if id_type == "email" and id_value == email_norm:
                return record["contact_id"]
    return ""


def get_contact_id_for_phone(phone: str) -> str:
    """Return the fixture-defined contact_id for the given E.164 phone number."""
    for record in CRM_CONTACTS:
        for id_type, id_value in record.get("identifiers", []):
            if id_type == "phone" and id_value == phone:
                return record["contact_id"]
    return ""
