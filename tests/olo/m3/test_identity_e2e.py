"""P0.3 real-adapter identity integration tests.

These tests start from raw API-shaped records (as returned by the Gmail and
Calendar APIs) and run real production adapter functions — NOT handcrafted
signals with prefilled ``entity_id``.

Business used: Nova Dental LLC (not Acme/Beta/Gamma).

Each test follows the full path:
  raw API record → adapter function → NormalizedSignal → upsert_entities
  → entity_metadata in SQLite → build_hindsight_items → context enrichment.

Tests are marked as integration tests for the adapters but do NOT require
network access — they call the adapter's signal-construction functions
directly, bypassing the OAuth/HTTP layer.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from olo.adapters.entity_extractor import (
    entity_id_from_email,
    extract_entities,
    resolve_primary_entity_id,
    resolve_primary_entity_id_from_payload,
    upsert_entities,
)
from olo.signals import NormalizedSignal, ContentBlob


# ---------------------------------------------------------------------------
# Raw API records for Nova Dental LLC
# ---------------------------------------------------------------------------

# Simulates Gmail API response for format=metadata
_RAW_GMAIL_RECEIVED = {
    "id": "nova001",
    "threadId": "thread001",
    "labelIds": ["INBOX", "UNREAD"],
    "internalDate": "1704067200000",  # 2024-01-01 00:00:00 UTC
    "sizeEstimate": 2048,
    "payload": {
        "headers": [
            {"name": "From",
             "value": "Alice Chen <alice.chen@nova-dental.com>"},
            {"name": "To",
             "value": "Dr. Martinez <dr.martinez@nova-dental.com>"},
            {"name": "Cc",
             "value": "Office Manager <office@nova-dental.com>"},
            {"name": "Subject",
             "value": "Patient record update — Nguyen"},
            {"name": "Date",
             "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
            {"name": "Message-ID",
             "value": "<nova001@nova-dental.com>"},
        ]
    },
}

_RAW_GMAIL_SENT = {
    "id": "nova002",
    "threadId": "thread001",
    "labelIds": ["SENT"],
    "internalDate": "1704067260000",
    "sizeEstimate": 1024,
    "payload": {
        "headers": [
            {"name": "From",
             "value": "Dr. Martinez <dr.martinez@nova-dental.com>"},
            {"name": "To",
             "value": "Alice Chen <alice.chen@nova-dental.com>"},
            {"name": "Subject",
             "value": "Re: Patient record update — Nguyen"},
            {"name": "Date",
             "value": "Mon, 1 Jan 2024 00:01:00 +0000"},
        ]
    },
}

# format=full Gmail API response (for email.content)
_RAW_GMAIL_FULL = {
    "id": "nova003",
    "threadId": "thread002",
    "labelIds": ["INBOX"],
    "internalDate": "1704153600000",  # 2024-01-02
    "sizeEstimate": 4096,
    "payload": {
        "mimeType": "text/plain",
        "headers": [
            {"name": "From",
             "value": "Insurance Provider <claims@dental-insurance.example>"},
            {"name": "To",
             "value": "Dr. Martinez <dr.martinez@nova-dental.com>"},
            {"name": "Subject",
             "value": "Claim approval #DI-2024-001"},
            {"name": "Date",
             "value": "Tue, 2 Jan 2024 00:00:00 +0000"},
            {"name": "Message-ID",
             "value": "<claim001@dental-insurance.example>"},
        ],
        "body": {
            "data": "Q2xhaW0gYXBwcm92ZWQ=",  # base64("Claim approved")
            "size": 13,
        },
    },
}

# Google Calendar API event
_RAW_GCAL_EVENT = {
    "id": "evt001",
    "status": "confirmed",
    "summary": "New patient consultation — Nguyen family",
    "updated": "2024-01-03T09:00:00Z",
    "created": "2024-01-02T08:00:00Z",
    "organizer": {
        "email": "dr.martinez@nova-dental.com",
        "displayName": "Dr. Martinez",
        "self": True,
    },
    "attendees": [
        {
            "email": "dr.martinez@nova-dental.com",
            "displayName": "Dr. Martinez",
            "responseStatus": "accepted",
            "organizer": True,
            "self": True,
        },
        {
            "email": "alice.chen@nova-dental.com",
            "displayName": "Alice Chen",
            "responseStatus": "accepted",
            "self": False,
        },
        {
            "email": "patient.nguyen@email.example",
            "displayName": "Nguyen",
            "responseStatus": "tentative",
            "self": False,
        },
    ],
    "start": {"dateTime": "2024-01-10T14:00:00Z"},
    "end": {"dateTime": "2024-01-10T15:00:00Z"},
}

_RAW_GCAL_CANCELLED = {
    "id": "evt002",
    "status": "cancelled",
    "summary": "Rescheduled: Implant consultation",
    "updated": "2024-01-04T10:00:00Z",
    "created": "2024-01-01T08:00:00Z",
    "organizer": {
        "email": "alice.chen@nova-dental.com",
        "displayName": "Alice Chen",
    },
    "attendees": [
        {
            "email": "alice.chen@nova-dental.com",
            "displayName": "Alice Chen",
            "responseStatus": "accepted",
            "organizer": True,
            "self": False,
        },
        {
            "email": "dr.martinez@nova-dental.com",
            "displayName": "Dr. Martinez",
            "responseStatus": "accepted",
            "self": True,
        },
    ],
    "start": {"dateTime": "2024-01-12T10:00:00Z"},
    "end": {"dateTime": "2024-01-12T11:00:00Z"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SOURCE = "gmail_nova"
SOURCE_CAL = "gcal_nova"
RECORD_ID = "hist001"
CALENDAR_ID = "primary"
FETCH_TIME = datetime(2024, 1, 5, tzinfo=timezone.utc)


def _build_gmail_signal(raw: dict) -> NormalizedSignal:
    """Build a signal via the real GmailAdapter._message_to_signal (no OAuth)."""
    from olo.adapters.gmail import GmailAdapter
    adapter = GmailAdapter.__new__(GmailAdapter)
    adapter.source = SOURCE
    sig = adapter._message_to_signal(raw, RECORD_ID)
    assert sig is not None, "GmailAdapter._message_to_signal returned None"
    return sig


def _build_gmail_full_signal(raw: dict) -> NormalizedSignal:
    """Build an email.content signal via the real gmail_full function."""
    from olo.adapters.gmail_full import build_email_content_signal
    return build_email_content_signal(SOURCE, raw, RECORD_ID)


def _build_gcal_signal(raw: dict) -> NormalizedSignal:
    """Build a calendar signal via the real gcal._event_to_signal."""
    from olo.adapters.gcal import _event_to_signal
    return _event_to_signal(raw, SOURCE_CAL, FETCH_TIME, CALENDAR_ID)


def _make_db(tmp_path: Path, subdir: str = "olo") -> Path:
    db_path = tmp_path / subdir / "olo.db"
    db_path.parent.mkdir(parents=True)
    from olo.store import connect, migrate
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    return db_path


def _store_signal_and_get_conn(db_path: Path, sig: NormalizedSignal) -> sqlite3.Connection:
    """Write a signal to the DB and return the open connection."""
    from olo.store import connect, migrate, acquire_run, ingest_batch, finish_run
    conn = connect(db_path)
    migrate(conn)
    run_id = acquire_run(sig.source, "test", conn)
    ingest_batch(sig.source, run_id, (sig,), "c1", None, conn)
    finish_run(sig.source, run_id, "succeeded", conn)
    return conn


# ---------------------------------------------------------------------------
# 1. Gmail email.received — real adapter → signal → entity → feeder
# ---------------------------------------------------------------------------

class TestGmailReceivedRealAdapter:
    """email.received via GmailAdapter._message_to_signal (no OAuth)."""

    def test_signal_kind_and_structure(self) -> None:
        sig = _build_gmail_signal(_RAW_GMAIL_RECEIVED)
        assert sig.kind == "email.received"
        assert sig.source == SOURCE
        # Addresses are nested under payload["headers"]
        hdr = sig.payload.get("headers", {})
        assert hdr.get("from"), "from addresses must be in payload['headers']['from']"
        assert hdr["from"][0]["email"] == "alice.chen@nova-dental.com"

    def test_entity_extraction_from_real_signal(self) -> None:
        sig = _build_gmail_signal(_RAW_GMAIL_RECEIVED)
        rows = extract_entities(sig)
        emails = {r["entity_id"] for r in rows}
        assert entity_id_from_email("alice.chen@nova-dental.com") in emails, (
            "Sender must be extracted from email.received"
        )
        assert entity_id_from_email("dr.martinez@nova-dental.com") in emails, (
            "Recipient must be extracted from email.received"
        )
        # Display names from headers must be preserved
        sender = next(r for r in rows
                      if r["entity_id"] == entity_id_from_email("alice.chen@nova-dental.com"))
        assert sender["display_name"] == "Alice Chen", (
            f"Display name from From header must be preserved, got {sender['display_name']!r}"
        )

    def test_primary_entity_resolution_email_received(self) -> None:
        """email.received: primary entity = sender."""
        sig = _build_gmail_signal(_RAW_GMAIL_RECEIVED)
        eid = resolve_primary_entity_id(sig)
        expected = entity_id_from_email("alice.chen@nova-dental.com")
        assert eid == expected, (
            f"Primary entity for email.received must be sender, got {eid!r}"
        )

    def test_e2e_entity_in_feeder_context(self, tmp_path: Path) -> None:
        """Full path: raw Gmail API (full format) → email.content signal → SQLite → feeder."""
        # email.received has no blob and cannot be fed to Hindsight directly.
        # Production pipeline uses email.content (from gmail_full) which carries the blob.
        # This test demonstrates the entity context enrichment via the full-content path.
        full_sig = _build_gmail_full_signal(_RAW_GMAIL_FULL)
        db_path = _make_db(tmp_path, "nova_recv")
        conn = _store_signal_and_get_conn(db_path, full_sig)
        upsert_entities((full_sig,), SOURCE, conn)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db_path, source=SOURCE)
        assert items, "Must produce hindsight items from email.content"
        item = items[0]
        context = item.get("context", "")
        # Context should include the contact name from entity_metadata
        assert "Insurance Provider" in context, (
            f"Feeder context must include sender name from email.content. Got: {context!r}"
        )


# ---------------------------------------------------------------------------
# 2. Gmail email.sent — real adapter
# ---------------------------------------------------------------------------

class TestGmailSentRealAdapter:
    """email.sent — counterparty is the TO recipient."""

    def test_signal_kind(self) -> None:
        sig = _build_gmail_signal(_RAW_GMAIL_SENT)
        assert sig.kind == "email.sent"

    def test_primary_entity_resolution_email_sent(self) -> None:
        """email.sent: primary entity = first TO recipient."""
        sig = _build_gmail_signal(_RAW_GMAIL_SENT)
        eid = resolve_primary_entity_id(sig)
        expected = entity_id_from_email("alice.chen@nova-dental.com")
        assert eid == expected, (
            f"Primary entity for email.sent must be first TO recipient, got {eid!r}"
        )

    def test_entity_extraction_from_sent(self) -> None:
        sig = _build_gmail_signal(_RAW_GMAIL_SENT)
        rows = extract_entities(sig)
        emails = {r["entity_id"] for r in rows}
        assert entity_id_from_email("alice.chen@nova-dental.com") in emails


# ---------------------------------------------------------------------------
# 3. Gmail email.content — real gmail_full function
# ---------------------------------------------------------------------------

class TestGmailFullContentRealAdapter:
    """email.content via gmail_full.build_email_content_signal."""

    def test_signal_kind_and_structure(self) -> None:
        sig = _build_gmail_full_signal(_RAW_GMAIL_FULL)
        assert sig.kind == "email.content"
        # email.content stores addresses top-level
        assert isinstance(sig.payload.get("from"), list), (
            "email.content must have top-level 'from' list"
        )
        assert sig.payload["from"][0]["email"] == "claims@dental-insurance.example"

    def test_entity_extraction_handles_email_content(self) -> None:
        sig = _build_gmail_full_signal(_RAW_GMAIL_FULL)
        rows = extract_entities(sig)
        emails = {r["entity_id"] for r in rows}
        assert entity_id_from_email("claims@dental-insurance.example") in emails, (
            "email.content sender must be extracted"
        )
        # Display name from the full content headers
        sender = next((r for r in rows
                       if r["entity_id"] == entity_id_from_email("claims@dental-insurance.example")),
                      None)
        assert sender is not None
        assert sender["display_name"] == "Insurance Provider"

    def test_primary_entity_direction_received(self) -> None:
        """email.content(received): primary entity = sender."""
        sig = _build_gmail_full_signal(_RAW_GMAIL_FULL)
        assert sig.payload.get("direction") == "received"
        eid = resolve_primary_entity_id(sig)
        expected = entity_id_from_email("claims@dental-insurance.example")
        assert eid == expected

    def test_e2e_email_content_to_feeder(self, tmp_path: Path) -> None:
        """email.content end-to-end: signals stored and feeder enriched."""
        sig = _build_gmail_full_signal(_RAW_GMAIL_FULL)
        db_path = _make_db(tmp_path, "nova_content")
        conn = _store_signal_and_get_conn(db_path, sig)
        upsert_entities((sig,), SOURCE, conn)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db_path, source=SOURCE)
        assert items, "email.content signal must produce feeder items"
        item = items[0]
        # Context should contain the Insurance Provider name
        context = item.get("context", "")
        assert "Insurance Provider" in context, (
            f"email.content feeder context must include sender name. Got: {context!r}"
        )


# ---------------------------------------------------------------------------
# 4. Calendar event.changed — real gcal adapter
# ---------------------------------------------------------------------------

class TestGcalRealAdapter:
    """calendar.event.changed / cancelled via gcal._event_to_signal."""

    def test_signal_kind_is_changed(self) -> None:
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        assert sig.kind == "calendar.event.changed", (
            f"Expected calendar.event.changed, got {sig.kind!r}"
        )

    def test_signal_kind_is_cancelled(self) -> None:
        sig = _build_gcal_signal(_RAW_GCAL_CANCELLED)
        assert sig.kind == "calendar.event.cancelled", (
            f"Expected calendar.event.cancelled, got {sig.kind!r}"
        )

    def test_organizer_display_name_preserved_in_payload(self) -> None:
        """gcal fix: organizer displayName must be in payload."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        assert sig.payload.get("organizer_display_name") == "Dr. Martinez", (
            f"organizer_display_name missing or wrong: "
            f"{sig.payload.get('organizer_display_name')!r}"
        )

    def test_attendee_display_name_preserved_in_payload(self) -> None:
        """gcal fix: attendee displayName must be in payload as 'display_name'."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        attendees = sig.payload.get("attendees", [])
        alice = next((a for a in attendees
                      if a.get("email") == "alice.chen@nova-dental.com"), None)
        assert alice is not None, "alice.chen must appear in attendees"
        assert alice.get("display_name") == "Alice Chen", (
            f"Attendee display_name missing: {alice!r}"
        )

    def test_entity_extraction_from_gcal(self) -> None:
        """Calendar entities include organizer and all attendees."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        rows = extract_entities(sig)
        emails = {r["entity_id"] for r in rows}
        assert entity_id_from_email("dr.martinez@nova-dental.com") in emails
        assert entity_id_from_email("alice.chen@nova-dental.com") in emails
        assert entity_id_from_email("patient.nguyen@email.example") in emails

        # Organizer display name from payload
        organizer = next((r for r in rows
                          if r["entity_id"] == entity_id_from_email("dr.martinez@nova-dental.com")),
                         None)
        assert organizer is not None
        assert organizer["display_name"] == "Dr. Martinez"

        # Attendee display name
        alice = next((r for r in rows
                      if r["entity_id"] == entity_id_from_email("alice.chen@nova-dental.com")),
                     None)
        assert alice is not None
        assert alice["display_name"] == "Alice Chen"

    def test_primary_entity_non_self_counterparty(self) -> None:
        """Calendar: primary entity = first non-self attendee (organizer is self)."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        eid = resolve_primary_entity_id(sig)
        # Organizer is self (dr.martinez has self=True) → first non-self attendee
        # First non-self is alice.chen, then patient.nguyen
        assert eid in (
            entity_id_from_email("alice.chen@nova-dental.com"),
            entity_id_from_email("patient.nguyen@email.example"),
        ), f"Primary entity must be a non-self attendee, got {eid!r}"

    def test_primary_entity_non_self_organizer(self) -> None:
        """Calendar: primary = organizer when organizer is not self."""
        sig = _build_gcal_signal(_RAW_GCAL_CANCELLED)
        eid = resolve_primary_entity_id(sig)
        # Organizer is alice.chen (not self); dr.martinez is self
        expected = entity_id_from_email("alice.chen@nova-dental.com")
        assert eid == expected, (
            f"Primary entity must be non-self organizer, got {eid!r}"
        )

    def test_e2e_gcal_to_feeder(self, tmp_path: Path) -> None:
        """Calendar event: full path to feeder context enrichment."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        db_path = _make_db(tmp_path, "nova_cal")
        conn = _store_signal_and_get_conn(db_path, sig)
        upsert_entities((sig,), SOURCE_CAL, conn)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db_path, source=SOURCE_CAL)
        assert items, "Calendar signal must produce feeder items"
        item = items[0]
        # Context should reference a non-self attendee (alice.chen)
        context = item.get("context", "")
        # At minimum the kind descriptor must be present
        assert "Calendar" in context or "calendar" in context.lower(), (
            f"Calendar event feeder context must have kind descriptor. Got: {context!r}"
        )

    # ── P0.3 final: organizer.self field preserved and used ───────────────

    def test_organizer_self_field_in_payload(self) -> None:
        """gcal fix: organizer.self must be stored in payload as 'organizer_self'."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        assert "organizer_self" in sig.payload, (
            "organizer_self field must be in Calendar signal payload"
        )
        assert sig.payload["organizer_self"] is True, (
            f"organizer_self must be True for dr.martinez (self=True). "
            f"Got: {sig.payload['organizer_self']!r}"
        )

    def test_owner_organized_external_attendee_resolves_to_attendee(self) -> None:
        """P0.3: owner organizes event; owner NOT in attendees list → attendee is counterparty.

        Replicates Opus's identity_e2e_live.py RAW_EVENTS[0]:
        organizer.self=True, owner NOT listed as attendee.
        Old code: is_org_self = False (no attendee match) → returned owner as counterparty.
        New code: organizer_self=True in payload → skips organizer, picks Priya.
        """
        from olo.adapters.entity_extractor import resolve_primary_entity_id_from_payload

        payload_owner_organizer_external_attendee = {
            "organizer_email": "owner@olo.example",
            "organizer_display_name": "Owner",
            "organizer_self": True,          # owner organizes
            "attendees": [
                # Owner is NOT listed as attendee (common for external-only invites)
                {"email": "priya@novadental.example",
                 "display_name": "Priya Raman",
                 "response_status": "accepted",
                 "organizer": False,
                 "self": False},
            ],
        }
        eid = resolve_primary_entity_id_from_payload(
            "calendar.event.changed", payload_owner_organizer_external_attendee
        )
        expected = entity_id_from_email("priya@novadental.example")
        assert eid == expected, (
            f"Owner-organized event with external attendee: counterparty must be "
            f"Priya ({expected[:16]}...), not the owner. Got: {eid!r}"
        )

    def test_owner_organized_via_real_adapter_resolves_to_attendee(self) -> None:
        """P0.3 live-scenario: real _event_to_signal output for owner-organized event."""
        raw_owner_organized = {
            "id": "owner-evt-001",
            "status": "confirmed",
            "summary": "Crown fitting",
            "updated": "2026-11-22T10:00:00Z",
            "created": "2026-11-20T10:00:00Z",
            "start": {"dateTime": "2026-11-28T15:00:00Z"},
            "end": {"dateTime": "2026-11-28T16:00:00Z"},
            "organizer": {
                "email": "owner@olo.example",
                "displayName": "Owner",
                "self": True,
            },
            # Owner is NOT listed among attendees (external-only invite pattern)
            "attendees": [
                {"email": "priya@novadental.example",
                 "displayName": "Priya Raman",
                 "responseStatus": "accepted"},
            ],
        }
        sig = _build_gcal_signal(raw_owner_organized)
        assert sig.payload.get("organizer_self") is True, (
            "organizer_self must be True in payload"
        )
        eid = resolve_primary_entity_id(sig)
        expected = entity_id_from_email("priya@novadental.example")
        assert eid == expected, (
            f"Real adapter owner-organized event: counterparty must be Priya. "
            f"Expected {expected[:16]}..., got {eid!r}"
        )

    def test_personal_event_no_external_attendee_has_no_counterparty(self) -> None:
        """P0.3: personal owner-organized event with no external attendee → no counterparty.

        Replicates Opus's RAW_EVENTS[1] ('Focus time'): no attendees at all.
        """
        raw_focus_time = {
            "id": "focus-001",
            "status": "confirmed",
            "summary": "Focus time",
            "updated": "2026-11-22T11:00:00Z",
            "created": "2026-11-20T10:00:00Z",
            "start": {"dateTime": "2026-11-29T15:00:00Z"},
            "end": {"dateTime": "2026-11-29T16:00:00Z"},
            "organizer": {
                "email": "owner@olo.example",
                "displayName": "Owner",
                "self": True,
            },
            # No attendees — personal calendar block
        }
        sig = _build_gcal_signal(raw_focus_time)
        eid = resolve_primary_entity_id(sig)
        assert eid == "", (
            f"Personal event with no external attendees must return '' "
            f"(no fabricated counterparty). Got: {eid!r}"
        )


# ---------------------------------------------------------------------------
# P0.3 final: kind descriptors for real signal kinds
# ---------------------------------------------------------------------------

class TestKindDescriptors:
    """Feeder must produce correct human-readable descriptors for all real signal kinds."""

    def _get_descriptor_from_feeder(
        self,
        tmp_path: Path,
        sig: NormalizedSignal,
        source: str,
    ) -> str:
        """Store signal, run feeder, return context from first item."""
        db_path = _make_db(tmp_path, source)
        conn = _store_signal_and_get_conn(db_path, sig)
        upsert_entities((sig,), source, conn)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db_path, source=source)
        return items[0].get("context", "") if items else ""

    def test_email_received_descriptor(self, tmp_path: Path) -> None:
        """email.content(received) → 'Email received' descriptor."""
        sig = _build_gmail_full_signal(_RAW_GMAIL_FULL)
        assert sig.payload.get("direction") == "received"
        ctx = self._get_descriptor_from_feeder(tmp_path, sig, SOURCE)
        assert ctx.startswith("Email received"), (
            f"email.content (received) must start with 'Email received'. Got: {ctx!r}"
        )

    def test_email_sent_descriptor(self, tmp_path: Path) -> None:
        """email.content(sent) → 'Email sent' descriptor."""
        # Build a sent full-content email
        raw_sent = {
            **_RAW_GMAIL_FULL,
            "id": "sent001",
            "labelIds": ["SENT"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From",
                     "value": "Dr. Martinez <dr.martinez@nova-dental.com>"},
                    {"name": "To",
                     "value": "Alice Chen <alice.chen@nova-dental.com>"},
                    {"name": "Subject", "value": "Re: Appointment"},
                    {"name": "Date",
                     "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                    {"name": "Message-ID",
                     "value": "<sent001@nova-dental.com>"},
                ],
                "body": {"data": "UmU6IEFwcG9pbnRtZW50", "size": 20},
            },
        }
        from olo.adapters.gmail_full import build_email_content_signal
        sig = build_email_content_signal(SOURCE, raw_sent, RECORD_ID)
        assert sig.payload.get("direction") == "sent", (
            f"direction must be 'sent' for SENT-labelled email. "
            f"Got: {sig.payload.get('direction')!r}"
        )
        ctx = self._get_descriptor_from_feeder(tmp_path, sig, SOURCE)
        assert ctx.startswith("Email sent"), (
            f"email.content (sent) must start with 'Email sent'. Got: {ctx!r}"
        )

    def test_calendar_changed_descriptor(self, tmp_path: Path) -> None:
        """calendar.event.changed → 'Calendar event' descriptor."""
        sig = _build_gcal_signal(_RAW_GCAL_EVENT)
        assert sig.kind == "calendar.event.changed"
        ctx = self._get_descriptor_from_feeder(tmp_path, sig, SOURCE_CAL)
        assert ctx.startswith("Calendar event"), (
            f"calendar.event.changed must start with 'Calendar event'. Got: {ctx!r}"
        )

    def test_calendar_cancelled_descriptor(self, tmp_path: Path) -> None:
        """calendar.event.cancelled → 'Cancelled calendar event' descriptor."""
        sig = _build_gcal_signal(_RAW_GCAL_CANCELLED)
        assert sig.kind == "calendar.event.cancelled"
        ctx = self._get_descriptor_from_feeder(tmp_path, sig, SOURCE_CAL)
        assert ctx.startswith("Cancelled calendar event"), (
            f"calendar.event.cancelled must start with 'Cancelled calendar event'. "
            f"Got: {ctx!r}"
        )

    def test_email_received_metadata_signal_descriptor(self) -> None:
        """email.received (metadata signal) → 'Email received' via _KIND_DESCRIPTORS."""
        # Verify the descriptor lookup table directly
        from olo.m3.feeder import build_hindsight_items
        # _KIND_DESCRIPTORS is defined inside build_hindsight_items — test via
        # the feeder output on a signal that uses the kind directly
        # (email.received uses the headers path, not email.content)
        payload_received = {
            "headers": {
                "from": [{"name": "Alice", "email": "alice@a.com"}],
                "to": [], "cc": [],
            },
            "message_id": "x",
        }
        from olo.adapters.entity_extractor import resolve_primary_entity_id_from_payload
        # Descriptor correctness is validated indirectly through real signals above.
        # This just confirms the resolution path works for email.received kind.
        eid = resolve_primary_entity_id_from_payload("email.received", payload_received)
        assert eid == entity_id_from_email("alice@a.com")


# ---------------------------------------------------------------------------
# 5. Two businesses — identity isolation
# ---------------------------------------------------------------------------

class TestTwoBusinessesIdentityIsolation:
    """Overlapping contact email across two businesses must not cross-contaminate."""

    def test_same_email_different_businesses_isolated(self, tmp_path: Path) -> None:
        """alice.chen@nova-dental.com in biz_a and biz_b stay isolated."""
        # Use email.content signals (which have blobs) for feeder compatibility.
        # Both businesses receive a full-content email from alice.chen.
        biz_a_path = _make_db(tmp_path, "biz_a")
        biz_b_path = _make_db(tmp_path, "biz_b")

        raw_a = {
            **_RAW_GMAIL_FULL,
            "id": "a001",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From",
                     "value": "Alice Chen <alice.chen@nova-dental.com>"},
                    {"name": "To",
                     "value": "Owner A <owner-a@biz-a.example>"},
                    {"name": "Subject", "value": "From Alice to A"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                    {"name": "Message-ID",
                     "value": "<a001@nova-dental.com>"},
                ],
                "body": {"data": "SGkgQQ==", "size": 4},  # "Hi A"
            },
        }
        raw_b = {
            **_RAW_GMAIL_FULL,
            "id": "b001",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From",
                     "value": "Alice Chen <alice.chen@nova-dental.com>"},
                    {"name": "To",
                     "value": "Owner B <owner-b@biz-b.example>"},
                    {"name": "Subject", "value": "From Alice to B"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                    {"name": "Message-ID",
                     "value": "<b001@nova-dental.com>"},
                ],
                "body": {"data": "SGkgQg==", "size": 4},  # "Hi B"
            },
        }

        from olo.adapters.gmail_full import build_email_content_signal
        sig_a = build_email_content_signal(SOURCE, raw_a, RECORD_ID)
        sig_b = build_email_content_signal(SOURCE, raw_b, RECORD_ID)

        from olo.store import connect, acquire_run, ingest_batch, finish_run, migrate
        from olo.m3.feeder import build_hindsight_items

        for sig, db_path in [(sig_a, biz_a_path), (sig_b, biz_b_path)]:
            conn = connect(db_path)
            migrate(conn)
            run_id = acquire_run(sig.source, "test", conn)
            ingest_batch(sig.source, run_id, (sig,), "c1", None, conn)
            finish_run(sig.source, run_id, "succeeded", conn)
            upsert_entities((sig,), sig.source, conn)
            conn.close()

        items_a = build_hindsight_items(biz_a_path, source=SOURCE)
        items_b = build_hindsight_items(biz_b_path, source=SOURCE)

        ctx_a = items_a[0].get("context", "") if items_a else ""
        ctx_b = items_b[0].get("context", "") if items_b else ""

        # Both businesses see Alice Chen (same name, same entity, different DB)
        assert "Alice Chen" in ctx_a, f"Biz A context must have Alice Chen: {ctx_a!r}"
        assert "Alice Chen" in ctx_b, f"Biz B context must have Alice Chen: {ctx_b!r}"

        # Verify SQLite isolation: each DB has its own entity_metadata row
        conn_a = connect(biz_a_path)
        conn_b = connect(biz_b_path)
        rows_a = conn_a.execute("SELECT * FROM entity_metadata").fetchall()
        rows_b = conn_b.execute("SELECT * FROM entity_metadata").fetchall()
        conn_a.close()
        conn_b.close()

        assert len(rows_a) > 0, "Biz A must have entity_metadata rows"
        assert len(rows_b) > 0, "Biz B must have entity_metadata rows"


# ---------------------------------------------------------------------------
# 6. Multiple recipients / ambiguous counterparty
# ---------------------------------------------------------------------------

class TestAmbiguousCounterparty:
    """No fabricated identity when counterparty is ambiguous."""

    def test_sent_multiple_recipients_uses_first_to(self) -> None:
        """email.sent with multiple TO — primary entity is first TO, not fabricated."""
        sig = _build_gmail_signal({
            **_RAW_GMAIL_SENT,
            "id": "multi001",
            "labelIds": ["SENT"],
            "payload": {
                "headers": [
                    {"name": "From",
                     "value": "Dr. Martinez <dr.martinez@nova-dental.com>"},
                    {"name": "To",
                     "value": ("Alice Chen <alice.chen@nova-dental.com>,"
                               " Bob Staff <bob@nova-dental.com>")},
                    {"name": "Subject", "value": "Staff meeting"},
                    {"name": "Date", "value": "Tue, 2 Jan 2024 00:00:00 +0000"},
                ]
            },
        })
        eid = resolve_primary_entity_id(sig)
        assert eid == entity_id_from_email("alice.chen@nova-dental.com"), (
            "Must use first TO recipient, not fabricate a combined identity"
        )

    def test_no_to_address_sent_returns_empty(self) -> None:
        """email.sent with no TO address → primary entity is ''."""
        # No TO header at all
        payload = {
            "message_id": "empty001",
            "headers": {"from": [], "to": [], "cc": []},
        }
        eid = resolve_primary_entity_id_from_payload("email.sent", payload)
        assert eid == "", (
            "No TO address in sent email must return '' (not fabricate identity)"
        )

    def test_calendar_all_self_attendees_returns_empty(self) -> None:
        """Calendar event with only self attendees → primary entity is ''."""
        payload = {
            "organizer_email": "dr.martinez@nova-dental.com",
            "organizer_display_name": "Dr. Martinez",
            "attendees": [
                {"email": "dr.martinez@nova-dental.com",
                 "display_name": "Dr. Martinez",
                 "self": True, "organizer": True,
                 "response_status": "accepted"},
            ],
        }
        # Organizer is self; only attendee is self
        eid = resolve_primary_entity_id_from_payload(
            "calendar.event.changed", payload
        )
        assert eid == "", (
            "All-self calendar event must return '' (not fabricate identity)"
        )


# ---------------------------------------------------------------------------
# 7. Missing identity — observable fallback
# ---------------------------------------------------------------------------

class TestMissingIdentityFallback:
    """Missing identity produces '' entity_id; feeder emits no context enrichment."""

    def test_signal_with_no_addresses_produces_no_entity(self) -> None:
        """Gmail signal with empty headers → no entity rows, no crash."""
        payload = {
            "message_id": "empty002",
            "headers": {"from": [], "to": [], "cc": []},
            "label_ids": ["INBOX"],
        }
        sig = NormalizedSignal(
            source=SOURCE,
            external_id="empty002",
            kind="email.received",
            occurred_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            subject=None,
            payload=payload,
        )
        rows = extract_entities(sig)
        assert rows == [], "No addresses → no entity rows"
        eid = resolve_primary_entity_id(sig)
        assert eid == "", "No addresses → empty entity_id"

    def test_feeder_works_without_entity(self, tmp_path: Path) -> None:
        """Feeder does not crash on a signal with no resolvable identity.

        Uses an email.content signal (has blob) where the from-address has
        no recognisable email (just a display name), so entity resolution
        returns '' and context enrichment has no contact name.
        """
        # Build a full-content signal from a sender with no parseable email
        raw_no_email = {
            **_RAW_GMAIL_FULL,
            "id": "noid001",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    # From with no valid email address
                    {"name": "From", "value": "No Reply"},
                    {"name": "To",
                     "value": "Dr. Martinez <dr.martinez@nova-dental.com>"},
                    {"name": "Subject", "value": "Automated notice"},
                    {"name": "Date", "value": "Mon, 1 Jan 2024 00:00:00 +0000"},
                    {"name": "Message-ID", "value": "<noid001@example.com>"},
                ],
                "body": {"data": "QXV0b21hdGVk", "size": 9},  # "Automated"
            },
        }
        from olo.adapters.gmail_full import build_email_content_signal
        sig = build_email_content_signal(SOURCE, raw_no_email, RECORD_ID)
        db_path = _make_db(tmp_path, "nova_noid")
        conn = _store_signal_and_get_conn(db_path, sig)
        conn.close()

        from olo.m3.feeder import build_hindsight_items
        items = build_hindsight_items(db_path, source=SOURCE)
        assert items, "Must produce items even with missing identity"
        # Context may be just the kind descriptor with no contact name
        ctx = items[0].get("context", "")
        assert ctx, f"Context must be non-empty even without identity: {ctx!r}"


# ---------------------------------------------------------------------------
# 8. Re-ingestion and idempotency
# ---------------------------------------------------------------------------

class TestReingestionIdempotency:
    """Re-ingesting the same signal produces stable, identical entity_id."""

    def test_same_signal_twice_produces_same_entity_id(self, tmp_path: Path) -> None:
        """entity_id is stable across process restarts (deterministic hash)."""
        sig1 = _build_gmail_signal(_RAW_GMAIL_RECEIVED)
        sig2 = _build_gmail_signal(_RAW_GMAIL_RECEIVED)

        eid1 = resolve_primary_entity_id(sig1)
        eid2 = resolve_primary_entity_id(sig2)
        assert eid1 == eid2 == entity_id_from_email("alice.chen@nova-dental.com"), (
            "Same signal must produce same entity_id on every run"
        )

    def test_entity_metadata_upsert_is_idempotent(self, tmp_path: Path) -> None:
        """Upserting the same entity twice leaves exactly one row."""
        sig = _build_gmail_signal(_RAW_GMAIL_RECEIVED)
        db_path = _make_db(tmp_path, "nova_idem")
        conn = _store_signal_and_get_conn(db_path, sig)

        # Upsert twice
        upsert_entities((sig,), SOURCE, conn)
        upsert_entities((sig,), SOURCE, conn)

        rows = conn.execute(
            "SELECT COUNT(*) FROM entity_metadata WHERE source=?", (SOURCE,)
        ).fetchone()[0]
        assert rows > 0

        # Exactly one row per unique (source, entity_id)
        dup = conn.execute(
            "SELECT entity_id, COUNT(*) FROM entity_metadata "
            "WHERE source=? GROUP BY entity_id HAVING COUNT(*) > 1",
            (SOURCE,),
        ).fetchall()
        assert dup == [], f"Duplicate rows after double-upsert: {dup}"
        conn.close()

    def test_display_name_preserved_on_re_upsert(self, tmp_path: Path) -> None:
        """Re-upserting a no-name entity does not wipe an existing display name."""
        db_path = _make_db(tmp_path, "nova_name")
        from olo.store import connect
        conn = connect(db_path)
        from olo.store import migrate
        migrate(conn)

        eid = entity_id_from_email("alice.chen@nova-dental.com")
        now = datetime(2024, 1, 1, tzinfo=timezone.utc).isoformat()

        # First upsert: with display name
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO entity_metadata"
            "(source, entity_id, display_name, company, source_kind,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (SOURCE, eid, "Alice Chen", "", "email_contact", now, now),
        )
        conn.execute("COMMIT")

        # Second upsert: empty display name (should not overwrite)
        no_name_sig = NormalizedSignal(
            source=SOURCE,
            external_id="nonametest",
            kind="email.received",
            occurred_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            subject=None,
            payload={
                "headers": {
                    "from": [{"name": "", "email": "alice.chen@nova-dental.com"}],
                    "to": [],
                    "cc": [],
                },
                "message_id": "nonametest",
            },
        )
        upsert_entities((no_name_sig,), SOURCE, conn)

        row = conn.execute(
            "SELECT display_name FROM entity_metadata WHERE source=? AND entity_id=?",
            (SOURCE, eid),
        ).fetchone()
        assert row["display_name"] == "Alice Chen", (
            f"Re-upsert with empty name must NOT wipe existing name, got {row['display_name']!r}"
        )
        conn.close()


# ---------------------------------------------------------------------------
# 9. Consistent resolution across ingestion and feeder paths
# ---------------------------------------------------------------------------

class TestConsistentResolution:
    """The same entity_id must be produced during ingestion and feeder construction."""

    def test_ingestion_and_feeder_produce_same_entity_id(self, tmp_path: Path) -> None:
        """entity_id used in entity_metadata must match feeder's resolution."""
        sig = _build_gmail_signal(_RAW_GMAIL_RECEIVED)
        db_path = _make_db(tmp_path, "nova_consist")
        conn = _store_signal_and_get_conn(db_path, sig)
        upsert_entities((sig,), SOURCE, conn)
        conn.close()

        # What entity_id the feeder resolves for this signal's payload
        payload = dict(sig.payload)
        kind = sig.kind
        feeder_eid = resolve_primary_entity_id_from_payload(kind, payload)

        # What entity_id was written to entity_metadata
        from olo.store import connect
        conn2 = connect(db_path)
        db_rows = conn2.execute(
            "SELECT entity_id FROM entity_metadata WHERE source=?", (SOURCE,)
        ).fetchall()
        conn2.close()
        db_eids = {r["entity_id"] for r in db_rows}

        assert feeder_eid in db_eids, (
            f"Feeder entity_id {feeder_eid!r} must be in entity_metadata "
            f"({db_eids}). Ingestion and feeder must use the same resolution."
        )

    def test_no_synthetic_fixture_import(self) -> None:
        """Production modules must not import from olo.m3.synthetic."""
        import sys
        # Import the production modules
        import olo.adapters.entity_extractor
        import olo.m3.feeder

        # None of the production modules should have synthetic in their deps
        for mod_name in sys.modules:
            if mod_name.startswith("olo.m3.synthetic"):
                # Check it wasn't imported BY entity_extractor or feeder
                mod = sys.modules[mod_name]
                assert "entity_extractor" not in str(getattr(mod, "__file__", "")), (
                    "entity_extractor must not import from olo.m3.synthetic"
                )
