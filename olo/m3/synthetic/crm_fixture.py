"""olo/m3/synthetic/crm_fixture.py — Synthetic CRM source fixture.

Represents what a business CRM system would export: canonical customer records
with verified contact identifiers.

IMPORTANT: This module is intentionally independent of ``olo.m3.synthetic.generator``
and of the benchmark labels in ENTITIES.  It represents the *source of truth* that
would exist in a real CRM — not the benchmark answer key.

The CRM fixture is the ONLY authoritative source for customer identity in the
development benchmark.  Production deployments replace this with their actual
CRM integration (Salesforce, HubSpot, Zoho, etc.).

Design rules (C1/C2 canonical identity model):
- Each record has one stable ``canonical_email`` used to derive a stable contact_id.
- Multiple verified identifiers (email addresses, phone numbers, billing system IDs)
  map to the same contact record.
- Two employees of the same organization share an ``organization_name``.
- This fixture does NOT import ENTITIES from the generator.
- The names and identifiers here happen to match the benchmark's customer set
  because the fixture is derived from the same real-world scenario; they are NOT
  answers to benchmark questions.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# CRM contacts
# ---------------------------------------------------------------------------

#: Each entry is a dict with:
#:   canonical_email (str): primary email, used to derive the stable contact_id.
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
        "canonical_email": "alice.chen@acme-corp.example",
        "display_name": "Alice Chen",
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
        "canonical_email": "bob.torres@beta-tech.example",
        "display_name": "Bob Torres",
        "organization_name": "Beta Tech",
        "identifiers": [
            ("email",              "bob.torres@beta-tech.example"),
            ("phone",              "+14155550202"),
            ("stripe_customer_id", "stripe:cus_beta_002"),
            ("qbo_customer_id",    "qbo:realm123:cust_002"),
        ],
    },
    {
        "canonical_email": "carla.osei@gamma-retail.example",
        "display_name": "Carla Osei",
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
        "canonical_email": "dan.park@acme-corp.example",
        "display_name": "Dan Park",
        "organization_name": "Acme Corp",
        "identifiers": [
            ("email", "dan.park@acme-corp.example"),
        ],
    },
]
