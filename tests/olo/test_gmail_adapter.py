"""Tests for olo.adapters.gmail — §8.3 (Gmail portion) acceptance criteria."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from olo.adapters import AdapterConfigError, SourceError
from olo.adapters.gmail import GmailAdapter, _decode_header_value, _parse_addresses
from tests.olo.fake_google import FakeGoogleServer


def _make_adapter(
    fake: FakeGoogleServer,
    alias: str = "test",
    tmp_path: Path | None = None,
    monkeypatch=None,
) -> GmailAdapter:
    """Create a GmailAdapter backed by the fake server.

    If tmp_path/monkeypatch are provided, credentials are saved to disk so
    refresh_access_token can reload them.  Otherwise the client is injected
    directly (for tests that only need the fetch layer).
    """
    adapter = GmailAdapter({"account": alias})
    if tmp_path is not None and monkeypatch is not None:
        creds = fake.make_saved_credentials(alias, tmp_path / "home", monkeypatch)
    else:
        creds = fake.make_credentials(alias)
    from olo.google.http import GoogleClient
    client = GoogleClient(creds)
    # Pre-load the access token so no disk read is needed
    client._access_token = fake.tokens.access_token
    adapter._client = client
    return adapter


# ── Construction ──────────────────────────────────────────────────────────────

def test_requires_account_option() -> None:
    with pytest.raises(AdapterConfigError, match="account"):
        GmailAdapter({})


def test_rejects_unknown_options() -> None:
    with pytest.raises(AdapterConfigError, match="does not accept"):
        GmailAdapter({"account": "x", "extra": "y"})


def test_source_is_gmail_alias() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake, "myalias")
        assert a.source == "gmail_myalias"


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def test_bootstrap_sets_history_id_no_signals() -> None:
    with FakeGoogleServer() as fake:
        fake.gmail.current_history_id = "5000"
        a = _make_adapter(fake)
        batch = a.fetch(None, limit=10)
        assert batch.signals == ()
        assert batch.next_cursor == "5000"
        assert batch.has_more is False


# ── Incremental ───────────────────────────────────────────────────────────────

def test_empty_history_advances_to_mailbox_history_id() -> None:
    with FakeGoogleServer() as fake:
        fake.gmail.current_history_id = "9999"
        a = _make_adapter(fake)
        # Incremental with no new messages
        batch = a.fetch("5000", limit=10)
        assert batch.signals == ()
        assert batch.next_cursor == "9999"
        assert batch.has_more is False


def test_incremental_produces_signals_for_new_messages() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        for i in range(1, 4):
            fake.gmail.add_message(f"msg{i}", subject=f"Message {i}")

        b1 = a.fetch(cursor, limit=10)
        assert len(b1.signals) == 3
        kinds = {s.kind for s in b1.signals}
        assert kinds == {"email.received"}


def test_sent_vs_received_kind() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.gmail.add_message("sent1", labels=["SENT"], subject="Sent Mail")
        fake.gmail.add_message("recv1", labels=["INBOX", "UNREAD"], subject="Received Mail")

        b1 = a.fetch(cursor, limit=10)
        kinds = {s.kind for s in b1.signals}
        assert "email.sent" in kinds
        assert "email.received" in kinds


def test_subject_is_decoded_and_in_signal_subject() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.gmail.add_message("m1", subject="Hello World")
        b1 = a.fetch(cursor, limit=10)
        assert len(b1.signals) == 1
        assert b1.signals[0].subject == "Hello World"


def test_subject_truncated_at_256() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        long_subject = "X" * 300
        fake.gmail.add_message("m1", subject=long_subject)
        b1 = a.fetch(cursor, limit=10)
        assert b1.signals[0].subject == "X" * 256


def test_no_address_in_signal_subject() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.gmail.add_message(
            "m1",
            subject="Meeting request",
            from_="boss@company.com",
            to="me@company.com",
        )
        b1 = a.fetch(cursor, limit=10)
        sig = b1.signals[0]
        assert "@" not in (sig.subject or "")
        assert "from" in sig.payload["headers"]


def test_skips_draft_spam_trash_chat() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        for lbl in ["DRAFT", "SPAM", "TRASH", "CHAT"]:
            fake.gmail.add_message(f"msg_{lbl}", labels=[lbl])

        b1 = a.fetch(cursor, limit=10)
        assert b1.signals == ()


def test_deleted_message_produces_no_received_signal() -> None:
    """When messagesAdded references a 404 message, skip it."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.gmail.add_message("gone", subject="Gone")
        fake.gmail.delete_message("gone")

        b1 = a.fetch(cursor, limit=10)
        received = [s for s in b1.signals if s.external_id == "gone"]
        assert received == []


# ── Change events ─────────────────────────────────────────────────────────────

def test_deleted_message_change_event() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        fake.gmail.add_message("msg1", subject="Hello")
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=10)
        cursor = b1.next_cursor

        fake.gmail.delete_message("msg1")
        b2 = a.fetch(cursor, limit=10)
        deleted = [s for s in b2.signals if s.kind == "email.deleted"]
        assert len(deleted) >= 1
        assert ":msg1:deleted" in deleted[0].external_id
        assert deleted[0].subject is None


def test_archive_and_mark_read_produce_label_removed_signals() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        fake.gmail.add_message("msg1", labels=["INBOX", "UNREAD"])
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=20)
        cursor = b1.next_cursor

        fake.gmail.remove_labels("msg1", ["INBOX"])
        fake.gmail.remove_labels("msg1", ["UNREAD"])

        b2 = a.fetch(cursor, limit=20)
        removed = [s for s in b2.signals if s.kind == "email.labels_removed"]
        assert len(removed) >= 1
        all_labels = [lbl for s in removed for lbl in s.payload.get("labels", [])]
        assert "INBOX" in all_labels or "UNREAD" in all_labels


def test_user_label_only_changes_ignored() -> None:
    """Labels not in CHANGE_SIGNAL_LABELS produce no signal."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        fake.gmail.add_message("msg1", labels=["INBOX"])
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=20)
        cursor = b1.next_cursor

        # User-defined label not in system set
        fake.gmail.history_records.append({
            "id": str(int(fake.gmail.current_history_id) + 1),
            "labelsAdded": [{"message": {
                "id": "msg1", "threadId": "msg1", "labelIds": ["INBOX"],
            }, "labelIds": ["Label_123"]}],
        })
        fake.gmail.current_history_id = str(int(fake.gmail.current_history_id) + 1)

        b2 = a.fetch(cursor, limit=20)
        label_sigs = [s for s in b2.signals if s.kind.startswith("email.labels")]
        # No signals with Label_123 in their labels list
        for sig in label_sigs:
            assert "Label_123" not in sig.payload.get("labels", [])


# ── Deduplication ─────────────────────────────────────────────────────────────

def test_duplicate_message_ids_in_batch_deduped() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        # First record adds dup1
        fake.gmail.add_message("dup1", subject="Dup")
        # Manually add another record also referencing dup1
        fake.gmail.history_records.append({
            "id": str(int(fake.gmail.current_history_id) + 1),
            "messagesAdded": [{"message": {
                "id": "dup1", "threadId": "dup1", "labelIds": ["INBOX"],
            }}],
        })
        fake.gmail.current_history_id = str(int(fake.gmail.current_history_id) + 1)

        b1 = a.fetch(cursor, limit=20)
        dup_sigs = [s for s in b1.signals if s.external_id == "dup1"]
        assert len(dup_sigs) == 1


# ── History 404 ───────────────────────────────────────────────────────────────

def test_history_404_fails_no_rebootstrap() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        fake.set_min_history_id("9999999")
        with pytest.raises(Exception):
            a.fetch("100", limit=10)


# ── §5.0 empty-page rule ──────────────────────────────────────────────────────

def test_runner_never_sees_empty_has_more() -> None:
    """Any FetchBatch with no signals must have has_more=False."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b = a.fetch(None, limit=10)
        assert not (b.signals == () and b.has_more is True)
        b2 = a.fetch(b.next_cursor, limit=10)
        assert not (b2.signals == () and b2.has_more is True)


# ── metadata_only_payload ─────────────────────────────────────────────────────

def test_metadata_only_payload() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.gmail.add_message("m1", subject="Test")
        b1 = a.fetch(cursor, limit=10)
        assert len(b1.signals) == 1
        payload = b1.signals[0].payload
        assert "body" not in payload
        assert "snippet" not in payload
        assert "headers" in payload


# ── Helper unit tests ─────────────────────────────────────────────────────────

def test_decode_header_rfc2047() -> None:
    encoded = "=?UTF-8?B?SGVsbG8gV29ybGQ=?="
    result = _decode_header_value(encoded)
    assert result == "Hello World"


def test_decode_header_truncates() -> None:
    result = _decode_header_value("A" * 300)
    assert result is not None
    assert len(result) == 256


def test_parse_addresses_multiple() -> None:
    raw = "Alice <alice@example.com>, Bob <bob@example.com>"
    result = _parse_addresses(raw)
    assert len(result) == 2
    assert result[0]["email"] == "alice@example.com"
    assert result[1]["name"] == "Bob"


def test_parse_addresses_caps_at_50() -> None:
    # Use short addresses to stay under _MAX_HEADER_CHARS (998)
    # each "uN <uN@e.com>" is ~15 chars; 60 × 16 chars = 960 < 998
    many = ", ".join(f"u{i} <u{i}@e.com>" for i in range(1, 61))
    result = _parse_addresses(many)
    assert len(result) == 50
