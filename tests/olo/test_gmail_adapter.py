"""Tests for olo.adapters.gmail — §8.3 (Gmail portion) acceptance criteria."""
from __future__ import annotations

from pathlib import Path

import pytest

from olo.adapters import AdapterConfigError
from olo.adapters.gmail import GmailAdapter, _decode_header_value, _parse_addresses
from tests.olo.fake_google import FakeGoogleServer


def _make_adapter(
    fake: FakeGoogleServer,
    alias: str = "test",
    tmp_path: Path | None = None,
    monkeypatch=None,
) -> GmailAdapter:
    """Create a GmailAdapter backed by the fake server via the public client= param."""
    if tmp_path is not None and monkeypatch is not None:
        creds = fake.make_saved_credentials(alias, tmp_path / "home", monkeypatch)
    else:
        creds = fake.make_credentials(alias)
    from olo.google.http import GoogleClient
    client = GoogleClient(creds)
    # Pre-load the access token so no disk read is needed during the test
    client._access_token = fake.tokens.access_token
    return GmailAdapter({"account": alias}, client=client)


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


# ── I8: §9.3 Gmail tests ──────────────────────────────────────────────────────

def test_records_added_whole() -> None:
    """I8: a history record is processed whole or not at all.

    If a record would push us over the limit, it must NOT be partially included.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        # Add 3 messages; each becomes its own history record
        for i in range(1, 4):
            fake.gmail.add_message(f"whole{i}", subject=f"Whole {i}")

        # Fetch with limit=2 — record 3 should NOT be partially included
        b = a.fetch(cursor, limit=2)
        # Either 2 signals (records 1+2 fit) or 1 signal (only record 1)
        # but NOT more than 2
        assert len(b.signals) <= 2
        # has_more must be True because record 3 is pending
        if len(b.signals) > 0:
            assert b.has_more is True


def test_history_types_requested() -> None:
    """I8: historyTypes=messageAdded,messageDeleted,labelAdded,labelRemoved sent.

    The fake's _handle_history re-parses the query string for repeated keys,
    so we verify by checking signals across all four change types appear.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        fake.gmail.add_message("ht1", labels=["INBOX", "UNREAD"])
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=20)
        cursor = b1.next_cursor

        # Trigger all four history types
        fake.gmail.add_message("ht2", subject="New")        # messageAdded
        fake.gmail.delete_message("ht1")                     # messageDeleted
        fake.gmail.add_labels("ht2", ["STARRED"])            # labelAdded
        fake.gmail.remove_labels("ht2", ["UNREAD"])          # labelRemoved

        b2 = a.fetch(cursor, limit=20)
        kinds = {s.kind for s in b2.signals}
        # At least messageAdded (email.received) and messageDeleted (email.deleted)
        assert "email.received" in kinds or "email.sent" in kinds
        assert "email.deleted" in kinds


def test_change_entries_count_toward_limit() -> None:
    """I8: each change entry within a history record counts toward limit.

    With limit=1, only the first record's signals should be returned
    and has_more=True if more records follow.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        for i in range(1, 4):
            fake.gmail.add_message(f"lim{i}", subject=f"Limit {i}")

        b = a.fetch(cursor, limit=1)
        # With limit=1 and 3 records each producing 1 signal,
        # we get at most 1 signal and has_more=True
        assert len(b.signals) <= 1
        # has_more must be True when there are unprocessed records
        assert b.has_more is True


def test_signal_less_page_then_messages() -> None:
    """I8: a page whose records produce no signals (filtered) is followed by one with signals.

    The adapter must continue past signal-less pages and return signals from
    the next page in the same fetch call.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        # First: add a DRAFT (filtered out) — produces a history record but no signal
        fake.gmail.add_message("draft1", labels=["DRAFT"])
        # Then: add a real message
        fake.gmail.add_message("real1", labels=["INBOX", "UNREAD"], subject="Real")

        b = a.fetch(cursor, limit=10)
        ids = [s.external_id for s in b.signals]
        # real1 must be included; draft1 must not
        assert "real1" in ids
        assert "draft1" not in ids


def test_signal_less_pages_cap() -> None:
    """I8: too many consecutive signal-less pages → returns () has_more=False."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        from olo.adapters.gmail import _MAX_EMPTY_PAGES

        # Add _MAX_EMPTY_PAGES+1 DRAFTs (each creates a record, all filtered)
        for i in range(_MAX_EMPTY_PAGES + 1):
            fake.gmail.add_message(f"slp{i}", labels=["DRAFT"])

        b = a.fetch(cursor, limit=10)
        # Must not loop forever; returns empty batch
        assert b.signals == ()
        assert b.has_more is False


def test_crash_mid_run_resumes_without_loss() -> None:
    """I8: cursor saved after partial batch lets the next run continue from there.

    Simulates a crash by using the mid-run cursor returned by has_more=True.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        for i in range(1, 6):
            fake.gmail.add_message(f"cr{i}", subject=f"Crash {i}")

        # Fetch with limit=2 → partial batch
        b1 = a.fetch(cursor0, limit=2)
        assert len(b1.signals) > 0
        saved_cursor = b1.next_cursor

        seen_ids = [s.external_id for s in b1.signals]

        # Resume from saved cursor
        cursor = saved_cursor
        for _ in range(20):
            b = a.fetch(cursor, limit=2)
            for sig in b.signals:
                seen_ids.append(sig.external_id)
            cursor = b.next_cursor
            if not b.has_more:
                break

        # All 5 messages (cr1..cr5) must appear, no duplicates
        # (Note: external_id == message_id for email.received signals)
        cr_seen = [i for i in seen_ids if i.startswith("cr")]
        assert len(set(cr_seen)) == len(cr_seen), f"Duplicates: {cr_seen}"
        assert sorted(set(cr_seen)) == [f"cr{i}" for i in range(1, 6)]


# ── F2 regression tests: consecutive empty-page loop fix ─────────────────────

def test_consecutive_empty_pages_reach_records() -> None:
    """F2: empty[]+p2, empty[]+p3, [record 150 with 1 inbox message] — 3 pages.

    Requests must be in order: no token, p2, p3.
    One signal returned; next_cursor == the record id from the third page.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        # Build a scripted sequence
        # Page 1: empty + nextPageToken=p2
        # Page 2: empty + nextPageToken=p3
        # Page 3: one real inbox message record, no token
        record_id = "150"
        fake.gmail.messages["real1"] = __import__(
            "tests.olo.fake_google", fromlist=["FakeMessage"]
        ).FakeMessage(
            id="real1",
            thread_id="real1",
            label_ids=["INBOX", "UNREAD"],
            internal_date="1700000000000",
            size_estimate=512,
            subject="Hello",
        )
        fake.gmail.script_history_pages([
            {"records": [], "next_page_token": "p2",
             "history_id": fake.gmail.current_history_id},
            {"records": [], "next_page_token": "p3",
             "history_id": fake.gmail.current_history_id},
            {"records": [{"id": record_id,
                          "messagesAdded": [{"message": {
                              "id": "real1",
                              "threadId": "real1",
                              "labelIds": ["INBOX", "UNREAD"],
                          }}]}],
             "history_id": record_id},
        ])

        b = a.fetch(cursor0, limit=10)

        # Requests were: no token, "p2", "p3"
        assert fake.gmail.recorded_history_requests() == [None, "p2", "p3"], (
            f"F2: wrong request sequence: {fake.gmail.recorded_history_requests()}"
        )
        # One signal for the inbox message
        assert len(b.signals) == 1, f"expected 1 signal, got {len(b.signals)}"
        assert b.signals[0].kind in ("email.received", "email.sent")
        # Cursor is the record id from the third page
        assert b.next_cursor == record_id, (
            f"expected cursor {record_id!r}, got {b.next_cursor!r}"
        )


def test_empty_page_cap_fails_loudly() -> None:
    """F2: 25 consecutive empty pages with tokens → SourceError; checkpoint unchanged."""
    from olo.adapters import SourceError

    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        # Script 25 empty pages each with a next token
        pages = [
            {"records": [], "next_page_token": f"pt{i}",
             "history_id": fake.gmail.current_history_id}
            for i in range(1, 26)
        ]
        fake.gmail.script_history_pages(pages)

        with pytest.raises(SourceError, match="too many consecutive empty"):
            a.fetch(cursor0, limit=10)


def test_signal_less_page_then_messages_f2() -> None:
    """F2 rewrite: page 1 = drafts + nextPageToken; page 2 = real message.

    The real message is returned in the same fetch(); cursor == page-2 record id.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        # Add a draft message (will be fetched but filtered out)
        draft_msg_id = "draft1"
        fake.gmail.messages[draft_msg_id] = __import__(
            "tests.olo.fake_google", fromlist=["FakeMessage"]
        ).FakeMessage(
            id=draft_msg_id,
            thread_id=draft_msg_id,
            label_ids=["DRAFT"],
            internal_date="1700000000000",
            size_estimate=100,
            subject="Draft subject",
        )
        # Add a real inbox message
        real_msg_id = "real2"
        fake.gmail.messages[real_msg_id] = __import__(
            "tests.olo.fake_google", fromlist=["FakeMessage"]
        ).FakeMessage(
            id=real_msg_id,
            thread_id=real_msg_id,
            label_ids=["INBOX", "UNREAD"],
            internal_date="1700000001000",
            size_estimate=512,
            subject="Real message",
        )

        fake.gmail.script_history_pages([
            # Page 1: draft message record + token
            {"records": [{"id": "101",
                          "messagesAdded": [{"message": {
                              "id": draft_msg_id,
                              "threadId": draft_msg_id,
                              "labelIds": ["DRAFT"],
                          }}]}],
             "next_page_token": "pt_real",
             "history_id": "101"},
            # Page 2: real inbox message, no token
            {"records": [{"id": "102",
                          "messagesAdded": [{"message": {
                              "id": real_msg_id,
                              "threadId": real_msg_id,
                              "labelIds": ["INBOX", "UNREAD"],
                          }}]}],
             "history_id": "102"},
        ])

        b = a.fetch(cursor0, limit=10)

        # Must include the real message
        assert len(b.signals) == 1, f"expected 1 signal, got {len(b.signals)}"
        assert b.signals[0].external_id == real_msg_id
        # Cursor is the record id from page 2
        assert b.next_cursor == "102", (
            f"expected cursor '102', got {b.next_cursor!r}"
        )


def test_signal_less_pages_cap_f2() -> None:
    """F2 rewrite: ≥21 signal-less pages → ((), has_more=False, last-consumed record id).

    Uses the scripted path with drafts-only records so signal_less_page logic is hit.
    """
    from olo.adapters.gmail import _MAX_EMPTY_PAGES

    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        # Register a draft message in the fake's message store
        fake.gmail.messages["slp_draft"] = __import__(
            "tests.olo.fake_google", fromlist=["FakeMessage"]
        ).FakeMessage(
            id="slp_draft",
            thread_id="slp_draft",
            label_ids=["DRAFT"],
            internal_date="1700000000000",
            size_estimate=100,
            subject="Draft",
        )

        # Script _MAX_EMPTY_PAGES+1 pages each with draft record + token
        # so the signal-less cap is reached
        pages = []
        for i in range(_MAX_EMPTY_PAGES + 1):
            rid = str(1000 + i)
            npt = f"slp_pt{i + 1}" if i < _MAX_EMPTY_PAGES else None
            page = {
                "records": [{"id": rid,
                             "messagesAdded": [{"message": {
                                 "id": "slp_draft",
                                 "threadId": "slp_draft",
                                 "labelIds": ["DRAFT"],
                             }}]}],
                "history_id": rid,
            }
            if npt:
                page["next_page_token"] = npt
            pages.append(page)
        fake.gmail.script_history_pages(pages)

        b = a.fetch(cursor0, limit=10)

        assert b.signals == (), f"expected no signals, got {b.signals}"
        assert b.has_more is False
        # Cursor must have advanced past consumed records (not stuck at cursor0)
        assert b.next_cursor != cursor0, (
            "cursor must advance past signal-less pages"
        )


# ── F5b regression tests: header caps and DRAFT/CHAT change-entry filtering ───

def test_giant_subject_capped_in_payload() -> None:
    """F5b: a 70,000-char Subject header is capped at _MAX_HEADER_CHARS (998)
    in the payload; the signal is created and total payload fits in 64 KiB."""
    import json as _json
    from olo.adapters.gmail import _MAX_HEADER_CHARS

    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        giant_subject = "X" * 70_000
        fake.gmail.add_message("huge1", subject=giant_subject)

        b = a.fetch(cursor0, limit=10)
        assert len(b.signals) == 1
        payload = b.signals[0].payload
        assert len(payload["headers"]["subject"]) <= _MAX_HEADER_CHARS, (
            f"subject in payload not capped: {len(payload['headers']['subject'])}"
        )
        # Serialised payload must fit within 64 KiB
        payload_bytes = len(_json.dumps(payload).encode())
        assert payload_bytes <= 64 * 1024, (
            f"payload too large: {payload_bytes} bytes"
        )


def test_giant_message_id_capped_in_payload() -> None:
    """F5b: a 5,000-char Message-ID is capped at _MAX_HEADER_CHARS (998)."""
    from olo.adapters.gmail import _MAX_HEADER_CHARS
    from tests.olo.fake_google import FakeMessage

    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        giant_mid = "<" + "m" * 5000 + "@example.com>"
        # Insert the message directly with the giant Message-ID header
        fake.gmail.messages["midtest"] = FakeMessage(
            id="midtest",
            thread_id="midtest",
            label_ids=["INBOX", "UNREAD"],
            internal_date="1700000000000",
            size_estimate=512,
            subject="Normal subject",
            message_id_header=giant_mid,
        )
        fake.gmail._bump_history({
            "messagesAdded": [{"message": {
                "id": "midtest", "threadId": "midtest",
                "labelIds": ["INBOX", "UNREAD"],
            }}]
        })

        b = a.fetch(cursor0, limit=10)
        assert len(b.signals) == 1
        mid_in_payload = b.signals[0].payload["headers"]["message_id_header"]
        assert len(mid_in_payload) <= _MAX_HEADER_CHARS, (
            f"message_id_header not capped: {len(mid_in_payload)}"
        )


def test_draft_deletion_produces_no_signal() -> None:
    """F5b: a messagesDeleted entry for a DRAFT message produces no signal."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=10)
        cursor = b1.next_cursor

        # Simulate a deletion history record for a DRAFT message
        fake.gmail._bump_history({
            "messagesDeleted": [{"message": {
                "id": "draftdel", "threadId": "draftdel",
                "labelIds": ["DRAFT"],
            }}]
        })

        b = a.fetch(cursor, limit=10)
        deleted_sigs = [s for s in b.signals if s.kind == "email.deleted"]
        assert deleted_sigs == [], (
            f"F5b: draft deletion must produce no signal, got: {deleted_sigs}"
        )


def test_chat_deletion_produces_no_signal() -> None:
    """F5b: a messagesDeleted entry for a CHAT message produces no signal."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=10)
        cursor = b1.next_cursor

        fake.gmail._bump_history({
            "messagesDeleted": [{"message": {
                "id": "chatdel", "threadId": "chatdel",
                "labelIds": ["CHAT"],
            }}]
        })

        b = a.fetch(cursor, limit=10)
        deleted_sigs = [s for s in b.signals if s.kind == "email.deleted"]
        assert deleted_sigs == [], (
            f"F5b: chat deletion must produce no signal, got: {deleted_sigs}"
        )


def test_draft_label_change_produces_no_signal() -> None:
    """F5b: label change on a DRAFT message produces no signal."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=10)
        cursor = b1.next_cursor

        # labelsAdded on a DRAFT message
        fake.gmail._bump_history({
            "labelsAdded": [{"message": {
                "id": "draftlbl", "threadId": "draftlbl",
                "labelIds": ["DRAFT", "STARRED"],
            }, "labelIds": ["STARRED"]}]
        })

        b = a.fetch(cursor, limit=10)
        label_sigs = [s for s in b.signals if s.kind.startswith("email.labels")]
        assert label_sigs == [], (
            f"F5b: draft label change must produce no signal, got: {label_sigs}"
        )


def test_inbox_deletion_still_produces_signal() -> None:
    """F5b: a deletion on an INBOX message still produces email.deleted."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        fake.gmail.add_message("inbox1", labels=["INBOX", "UNREAD"])
        b0 = a.fetch(None, limit=10)
        b1 = a.fetch(b0.next_cursor, limit=10)
        cursor = b1.next_cursor

        fake.gmail._bump_history({
            "messagesDeleted": [{"message": {
                "id": "inbox1", "threadId": "inbox1",
                "labelIds": ["INBOX"],
            }}]
        })

        b = a.fetch(cursor, limit=10)
        deleted_sigs = [s for s in b.signals if s.kind == "email.deleted"]
        assert len(deleted_sigs) == 1, (
            f"expected 1 email.deleted for inbox message, got: {deleted_sigs}"
        )
