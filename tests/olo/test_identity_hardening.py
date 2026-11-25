"""tests/olo/test_identity_hardening.py — S1–S4 hardening: fail-closed identity.

Regression tests for the reproduced audit cases (baseline 7e0a56d324):

  R1  sent mail to two known customers resolved to the first recipient
  R2  counterparty extraction considered only the first recipient

Plus: the business's own identifiers are never counterparties, and the
signal worker records several plausible customers as 'ambiguous' without
creating a contact.  Existing conflict and DenyAll behaviour is covered by
the S3 and identity-safety suites, which are unchanged.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo.adapters.contacts import (
    IDENTITY_AMBIGUOUS,
    IDENTITY_NEW,
    IDENTITY_NONE,
    IDENTITY_RESOLVED,
    extract_counterparty,
    load_own_identifiers,
    migrate_contacts,
    resolve_contact_from_signal,
    resolve_signal_identity,
)
from olo.store import migrate

pytestmark = pytest.mark.not_integration

OWNER = "owner@olo-business.example"
IMAP = {("email", "alice@x.com"): "cid_alice", ("email", "bob@y.com"): "cid_bob",
        ("email", "alice.alt@x.com"): "cid_alice"}


def _addrs(*emails: str) -> list[dict]:
    return [{"email": e, "name": e.split("@")[0]} for e in emails]


def _sent(*to: str, sender: str = OWNER) -> dict:
    return {"headers": {"from": _addrs(sender), "to": _addrs(*to)}}


def _received(sender: str) -> dict:
    return {"headers": {"from": _addrs(sender), "to": _addrs(OWNER)}}


# ── R1 / R2: several plausible customers are ambiguous ────────────────────────

class TestMultipleCustomers:
    def test_r1_two_known_customers_are_ambiguous(self) -> None:
        ident = resolve_signal_identity("email.sent", _sent("alice@x.com", "bob@y.com"), IMAP)
        assert ident.status == IDENTITY_AMBIGUOUS and ident.contact_id == ""
        assert "alice@x.com" in ident.reason and "bob@y.com" in ident.reason

    def test_r1_legacy_lookup_fails_closed(self) -> None:
        assert resolve_contact_from_signal(
            "email.sent", _sent("alice@x.com", "bob@y.com"), IMAP) == ""

    def test_r2_two_unknown_counterparties_are_ambiguous(self) -> None:
        payload = _sent("carol@z.com", "dave@w.com")
        assert resolve_signal_identity("email.sent", payload, {}).status == IDENTITY_AMBIGUOUS
        assert extract_counterparty("email.sent", payload) is None

    def test_known_plus_unknown_is_ambiguous(self) -> None:
        ident = resolve_signal_identity("email.sent", _sent("alice@x.com", "carol@z.com"), IMAP)
        assert ident.status == IDENTITY_AMBIGUOUS

    def test_two_addresses_of_one_contact_resolve(self) -> None:
        ident = resolve_signal_identity(
            "email.sent", _sent("alice@x.com", "alice.alt@x.com"), IMAP)
        assert ident.status == IDENTITY_RESOLVED and ident.contact_id == "cid_alice"

    def test_single_recipient_unchanged(self) -> None:
        assert resolve_contact_from_signal("email.sent", _sent("bob@y.com"), IMAP) == "cid_bob"
        assert extract_counterparty("email.sent", _sent("carol@z.com")) == (
            "email", "carol@z.com", "carol")

    def test_duplicate_recipient_is_one_party(self) -> None:
        ident = resolve_signal_identity("email.sent", _sent("bob@y.com", "BOB@y.com"), IMAP)
        assert ident.status == IDENTITY_RESOLVED

    def test_automated_recipient_is_not_a_party(self) -> None:
        ident = resolve_signal_identity(
            "email.sent", _sent("alice@x.com", "noreply@x.com"), IMAP)
        assert ident.status == IDENTITY_RESOLVED and ident.contact_id == "cid_alice"

    def test_calendar_with_two_external_attendees_is_ambiguous(self) -> None:
        payload = {"organizer_email": OWNER, "organizer_self": True, "attendees": [
            {"email": "alice@x.com", "self": False}, {"email": "bob@y.com", "self": False},
            {"email": OWNER, "self": True}]}
        assert resolve_signal_identity(
            "calendar.event.created", payload, IMAP).status == IDENTITY_AMBIGUOUS
        assert resolve_contact_from_signal("calendar.event.created", payload, IMAP) == ""

    def test_calendar_external_organizer_resolves(self) -> None:
        payload = {"organizer_email": "bob@y.com", "organizer_self": False,
                   "attendees": [{"email": "alice@x.com", "self": False}]}
        assert resolve_contact_from_signal(
            "calendar.event.changed", payload, IMAP) == "cid_bob"


# ── Own identifiers are never counterparties ──────────────────────────────────

class TestOwnIdentifiers:
    def test_configured_own_address_excluded(self) -> None:
        ident = resolve_signal_identity(
            "email.sent", _sent("alice@x.com", "team@olo-business.example"), IMAP,
            own_identifiers={("email", "team@olo-business.example")})
        assert ident.status == IDENTITY_RESOLVED and ident.contact_id == "cid_alice"

    def test_sent_mail_sender_is_own(self) -> None:
        ident = resolve_signal_identity("email.sent", _sent(OWNER, "alice@x.com"), IMAP)
        assert ident.status == IDENTITY_RESOLVED and ident.contact_id == "cid_alice"

    def test_mail_to_self_has_no_counterparty(self) -> None:
        assert resolve_signal_identity("email.sent", _sent(OWNER), {}).status == IDENTITY_NONE
        assert extract_counterparty("email.sent", _sent(OWNER)) is None

    def test_received_from_own_address_has_no_counterparty(self) -> None:
        ident = resolve_signal_identity("email.received", _received(OWNER), {},
                                        own_identifiers={("email", OWNER)})
        assert ident.status == IDENTITY_NONE

    def test_content_kind_uses_direction(self) -> None:
        sent = {"direction": "sent", "from": _addrs(OWNER), "to": _addrs("alice@x.com", OWNER)}
        assert resolve_signal_identity("email.content", sent, IMAP).contact_id == "cid_alice"
        recv = {"direction": "received", "from": _addrs("carol@z.com"), "to": _addrs(OWNER)}
        ident = resolve_signal_identity("email.content", recv, {})
        assert ident.status == IDENTITY_NEW and ident.counterparty[1] == "carol@z.com"

    def test_own_identifiers_learned_from_stored_sent_mail(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE signals (kind TEXT, payload TEXT)")
        conn.executemany("INSERT INTO signals VALUES (?, ?)", [
            ("email.sent", json.dumps(_sent("alice@x.com"))),
            ("email.content", json.dumps({"direction": "sent", "from": _addrs("ops@biz.example")})),
            ("email.content", json.dumps({"direction": "received", "from": _addrs("x@y.com")})),
            ("email.received", json.dumps(_received("bob@y.com"))),
        ])
        assert load_own_identifiers(conn) == {("email", OWNER), ("email", "ops@biz.example")}

    def test_load_own_identifiers_without_tables(self) -> None:
        assert load_own_identifiers(sqlite3.connect(":memory:")) == frozenset()


# ── Worker records ambiguity and creates nothing ──────────────────────────────

class TestWorkerAmbiguity:
    def _run(self, tmp_path: Path, payload: dict, kind: str = "email.sent"):
        from olo.adapters import FetchBatch
        from olo.runner import run_source
        from olo.signals import NormalizedSignal

        class _Adapter:
            source = "gmail_owner"
            _done = False

            def fetch(self, cursor, *, limit):
                if self._done:
                    return FetchBatch(signals=[], has_more=False, next_cursor=cursor)
                self._done = True
                sig = NormalizedSignal(
                    source="gmail_owner", external_id="sig-1", kind=kind,
                    occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                    subject=None, payload=payload,
                )
                return FetchBatch(signals=[sig], has_more=False, next_cursor="c1")

        db = tmp_path / "olo.db"
        conn = sqlite3.connect(str(db), isolation_level=None)
        migrate(conn)
        migrate_contacts(conn)
        conn.close()
        result = run_source(_Adapter(), db_path=db)
        conn = sqlite3.connect(str(db), isolation_level=None)
        conn.row_factory = sqlite3.Row
        return result, conn

    def test_two_recipients_recorded_ambiguous_without_contacts(self, tmp_path) -> None:
        result, conn = self._run(tmp_path, _sent("carol@z.com", "dave@w.com"))
        assert result.status == "succeeded" and result.identity_resolved == 0
        row = conn.execute("SELECT identity_status, contact_id, identity_reason"
                           " FROM signal_resolution WHERE signal_id='sig-1'").fetchone()
        assert row["identity_status"] == "ambiguous" and row["contact_id"] is None
        assert "carol@z.com" in row["identity_reason"]
        assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0

    def test_single_recipient_still_creates_contact(self, tmp_path) -> None:
        result, conn = self._run(tmp_path, _sent("carol@z.com", OWNER))
        assert result.identity_resolved == 1
        row = conn.execute("SELECT identity_status FROM signal_resolution"
                           " WHERE signal_id='sig-1'").fetchone()
        assert row["identity_status"] == "resolved"
        emails = {r[0] for r in conn.execute(
            "SELECT identifier_value FROM contact_identifiers")}
        assert emails == {"carol@z.com"}  # the owner is never stored as a contact
