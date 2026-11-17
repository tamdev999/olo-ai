"""Offline unit tests for the B6 live_seed seeder.

These tests run without any network connection or Google credentials.
They verify the MIME builder, account guard, token helpers, and fixture state.
"""
from __future__ import annotations

import base64
import email as _email_mod
import email.parser
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests.olo.live_seed.seeder import (
    BODY_MARKER,
    SeederAccountError,
    _assert_allowed_account,
    _encode_message,
    _fixture_state_path,
    _make_message_id,
    _rfc2822_date,
    _token_path,
    build_mime_message,
    load_fixture_state,
    save_fixture_state,
    CalendarSeeder,
    GmailSeeder,
)


# ── Account guard ─────────────────────────────────────────────────────────────

def test_guard_raises_when_env_unset() -> None:
    """Account guard raises when OLO_SEEDER_ALLOWED_EMAIL is not set."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OLO_SEEDER_ALLOWED_EMAIL", None)
        with pytest.raises(SeederAccountError, match="OLO_SEEDER_ALLOWED_EMAIL"):
            _assert_allowed_account("any@example.com")


def test_guard_raises_on_mismatch() -> None:
    """Account guard raises when actual email does not match allowed."""
    with patch.dict(os.environ, {"OLO_SEEDER_ALLOWED_EMAIL": "allowed@test.example.com"}):
        with pytest.raises(SeederAccountError, match="Account guard"):
            _assert_allowed_account("real@example.com")


def test_guard_passes_on_match() -> None:
    """Account guard passes when actual email matches allowed (case-insensitive)."""
    with patch.dict(os.environ, {"OLO_SEEDER_ALLOWED_EMAIL": "Allowed@Test.EXAMPLE.com"}):
        _assert_allowed_account("allowed@test.example.com")  # must not raise


def test_guard_case_insensitive() -> None:
    """Account guard comparison is case-insensitive on both sides."""
    with patch.dict(os.environ, {"OLO_SEEDER_ALLOWED_EMAIL": "TEST@EXAMPLE.COM"}):
        _assert_allowed_account("test@example.com")  # must not raise


# ── MIME builder ──────────────────────────────────────────────────────────────

def test_build_mime_always_includes_body_marker() -> None:
    """Every message built by build_mime_message contains OLO-BODY-MARKER in the decoded body."""
    raw = build_mime_message(
        subject="Test", from_addr="a@example.com", to_addr="b@example.com",
        body_text="Hello world.",
    )
    msg = _email_mod.message_from_string(raw)
    # Decode body — may be base64-encoded by MIMEText with utf-8
    body = msg.get_payload(decode=True)
    if body is not None:
        decoded = body.decode("utf-8", errors="replace")
    else:
        decoded = msg.get_payload()
    assert BODY_MARKER in decoded, (
        f"BODY_MARKER missing from decoded body. Raw:\n{raw}"
    )


def test_build_mime_subject_set() -> None:
    """Subject header is present and correct."""
    raw = build_mime_message(
        subject="My Subject",
        from_addr="a@example.com", to_addr="b@example.com",
        body_text="body",
    )
    msg = _email_mod.message_from_string(raw)
    assert msg["Subject"] == "My Subject"


def test_build_mime_from_to() -> None:
    """From and To headers are correct."""
    raw = build_mime_message(
        subject="S", from_addr="sender@example.com", to_addr="recipient@example.com",
        body_text="body",
    )
    msg = _email_mod.message_from_string(raw)
    assert "sender@example.com" in msg["From"]
    assert "recipient@example.com" in msg["To"]


def test_build_mime_cc_optional() -> None:
    """Cc is omitted when not provided; present when provided."""
    raw_no_cc = build_mime_message(
        subject="S", from_addr="a@e.com", to_addr="b@e.com", body_text="body",
    )
    msg = _email_mod.message_from_string(raw_no_cc)
    assert msg["Cc"] is None

    raw_cc = build_mime_message(
        subject="S", from_addr="a@e.com", to_addr="b@e.com", body_text="body",
        cc_addrs=["c@e.com", "d@e.com"],
    )
    msg_cc = _email_mod.message_from_string(raw_cc)
    assert "c@e.com" in msg_cc["Cc"]
    assert "d@e.com" in msg_cc["Cc"]


def test_build_mime_message_id() -> None:
    """Message-ID is present when provided."""
    mid = "<test-id-123@example.com>"
    raw = build_mime_message(
        subject="S", from_addr="a@e.com", to_addr="b@e.com", body_text="body",
        message_id=mid,
    )
    msg = _email_mod.message_from_string(raw)
    assert msg["Message-ID"] == mid


def test_build_mime_threading_headers() -> None:
    """In-Reply-To and References are set correctly."""
    parent_mid = "<parent@example.com>"
    raw = build_mime_message(
        subject="Re: S", from_addr="a@e.com", to_addr="b@e.com", body_text="reply",
        in_reply_to=parent_mid,
        references=[parent_mid, "<earlier@example.com>"],
    )
    msg = _email_mod.message_from_string(raw)
    assert msg["In-Reply-To"] == parent_mid
    assert parent_mid in msg["References"]
    assert "<earlier@example.com>" in msg["References"]


def test_build_mime_extra_headers() -> None:
    """Extra headers are injected."""
    raw = build_mime_message(
        subject="S", from_addr="a@e.com", to_addr="b@e.com", body_text="body",
        extra_headers={"X-Custom": "value123"},
    )
    msg = _email_mod.message_from_string(raw)
    assert msg["X-Custom"] == "value123"


def test_encode_message_is_base64url() -> None:
    """_encode_message produces valid base64url without padding."""
    raw = "From: a@e.com\r\nTo: b@e.com\r\n\r\nHello"
    encoded = _encode_message(raw)
    # Must be decodable
    decoded = base64.urlsafe_b64decode(encoded + "==")
    assert decoded.decode("utf-8") == raw
    # Must not contain padding
    assert "=" not in encoded


def test_make_message_id_format() -> None:
    """_make_message_id returns a valid <...@...> format string."""
    mid = _make_message_id("p1-20261116")
    assert mid.startswith("<")
    assert "@" in mid
    assert mid.endswith(">")


def test_rfc2822_date_format() -> None:
    """_rfc2822_date returns an RFC 2822 string."""
    from datetime import UTC, datetime
    dt = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
    result = _rfc2822_date(dt)
    # Must contain day-of-week, month name, year, and timezone
    assert "2026" in result
    assert "Nov" in result or "UTC" in result or "GMT" in result


# ── Token path helpers ────────────────────────────────────────────────────────

def test_token_path_uses_env_var() -> None:
    """OLO_SEEDER_TOKEN_FILE overrides default token path."""
    with tempfile.TemporaryDirectory() as td:
        custom = str(Path(td) / "my_token.json")
        with patch.dict(os.environ, {"OLO_SEEDER_TOKEN_FILE": custom}):
            assert str(_token_path()) == custom


def test_fixture_state_path_uses_env_var() -> None:
    """OLO_SEEDER_STATE_FILE overrides default state path."""
    with tempfile.TemporaryDirectory() as td:
        custom = str(Path(td) / "state.json")
        with patch.dict(os.environ, {"OLO_SEEDER_STATE_FILE": custom}):
            assert str(_fixture_state_path()) == custom


# ── GmailSeeder fixture ordering ──────────────────────────────────────────────

def test_gmail_seeder_p6_requires_p5() -> None:
    """GmailSeeder.seed_p6 raises if P5 not seeded first."""
    seeder = GmailSeeder(token="fake", account_email="test@example.com")
    with pytest.raises(ValueError, match="P5"):
        seeder.seed_p6("sender@example.com", "tag")


def test_gmail_seeder_p7_requires_p5_p6() -> None:
    """GmailSeeder.seed_p7 raises if P5 or P6 not seeded first."""
    seeder = GmailSeeder(token="fake", account_email="test@example.com")
    with pytest.raises(ValueError, match="P5"):
        seeder.seed_p7("sender@example.com", "tag")


def test_gmail_seeder_p5_p6_same_thread() -> None:
    """P6 is recorded with the same thread_id as P5."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")

    # Manually inject P5 without network
    seeder.created["P5"] = {
        "id": "p5id",
        "thread_id": "thread001",
        "message_id_header": "<p5@example.com>",
        "subject": "P5 subject",
    }

    # Patch the _import_message to return a fake response
    def _fake_import(raw, label_ids):
        return {"id": "p6id", "threadId": "thread001"}

    seeder._import_message = _fake_import
    seeder.seed_p6("sender@example.com", "tag")

    assert seeder.created["P6"]["thread_id"] == "thread001"


def test_gmail_seeder_p8_self_email() -> None:
    """P8 is imported with INBOX+SENT labels (self-email)."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")
    captured: list[dict] = []

    def _fake_import(raw, label_ids):
        captured.append({"raw": raw, "label_ids": label_ids})
        return {"id": "p8id", "threadId": "p8id"}

    seeder._import_message = _fake_import
    seeder.seed_p8("tag")

    assert captured, "import_message was not called"
    labels = captured[0]["label_ids"]
    assert "INBOX" in labels, f"INBOX missing from P8 labels: {labels}"
    assert "SENT" in labels, f"SENT missing from P8 labels: {labels}"

    # Raw message should have From == To (self-email)
    raw = captured[0]["raw"]
    msg = _email_mod.message_from_string(raw)
    assert "me@example.com" in msg["From"]
    assert "me@example.com" in msg["To"]


def test_gmail_seeder_all_messages_contain_body_marker() -> None:
    """Every P1–P8 message contains OLO-BODY-MARKER in the decoded MIME body."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")
    raws: list[str] = []

    def _fake_import(raw, label_ids):
        raws.append(raw)
        idx = len(raws)
        return {"id": f"id{idx}", "threadId": f"thread{idx}"}

    seeder._import_message = _fake_import

    sender = "sender@example.com"
    tag = "test"
    seeder.seed_p1(sender, tag)
    seeder.seed_p2(sender, tag)
    seeder.seed_p3(sender, tag)
    seeder.seed_p4(sender, tag)
    seeder.seed_p5(sender, tag)
    seeder.seed_p6(sender, tag)
    seeder.seed_p7(sender, tag)
    seeder.seed_p8(tag)

    assert len(raws) == 8, f"expected 8 messages, got {len(raws)}"
    for i, raw in enumerate(raws):
        msg = _email_mod.message_from_string(raw)
        body_bytes = msg.get_payload(decode=True)
        if body_bytes is not None:
            decoded = body_bytes.decode("utf-8", errors="replace")
        else:
            decoded = msg.get_payload() or ""
        assert BODY_MARKER in decoded, (
            f"P{i+1}: {BODY_MARKER} missing from decoded body"
        )


# ── CalendarSeeder ────────────────────────────────────────────────────────────

def test_calendar_seeder_secondary_requires_create_first() -> None:
    """seed_secondary_event raises if secondary calendar not created yet."""
    seeder = CalendarSeeder(token="fake")
    with pytest.raises(ValueError, match="secondary calendar"):
        seeder.seed_secondary_event("tag")


def test_calendar_seeder_e4_has_recurrence() -> None:
    """E4 body includes RRULE:FREQ=WEEKLY."""
    seeder = CalendarSeeder(token="fake")
    captured: list[dict] = []

    def _fake_post(url, body, token, **kwargs):
        captured.append(body)
        return {"id": f"evid{len(captured)}", "summary": body.get("summary", "")}

    with patch("tests.olo.live_seed.seeder._api_post", side_effect=_fake_post):
        seeder.seed_e4("tag")

    assert captured, "_api_post was not called"
    body = captured[0]
    assert "recurrence" in body
    rrule = " ".join(body["recurrence"])
    assert "RRULE:FREQ=WEEKLY" in rrule


def test_calendar_seeder_events_contain_body_marker() -> None:
    """E1–E4 event descriptions contain OLO-BODY-MARKER."""
    seeder = CalendarSeeder(token="fake")
    captured: list[dict] = []

    def _fake_post(url, body, token, **kwargs):
        if isinstance(body, dict):
            captured.append(body)
        return {"id": f"evid{len(captured)}", "summary": body.get("summary", "") if isinstance(body, dict) else ""}

    with patch("tests.olo.live_seed.seeder._api_post", side_effect=_fake_post):
        seeder.seed_e1("tag")
        seeder.seed_e2("tag")
        seeder.seed_e3("tag")
        seeder.seed_e4("tag")

    for ev in captured:
        assert BODY_MARKER in ev.get("description", ""), (
            f"{BODY_MARKER} missing from event: {ev.get('summary')}"
        )


# ── Fixture state persistence ─────────────────────────────────────────────────

def test_fixture_state_round_trip(tmp_path: Path) -> None:
    """save/load fixture state round-trips correctly."""
    state_file = tmp_path / "state.json"
    with patch.dict(os.environ, {"OLO_SEEDER_STATE_FILE": str(state_file)}):
        g = GmailSeeder(token="fake", account_email="me@example.com")
        g.created["P1"] = {"id": "p1id", "thread_id": "t1", "message_id_header": "<p1>", "subject": "S1"}
        c = CalendarSeeder(token="fake")
        c.created["E1"] = {"id": "e1id", "summary": "Event 1"}

        save_fixture_state(g, c, run_tag="20261116T120000")

        loaded = load_fixture_state()
        assert loaded["run_tag"] == "20261116T120000"
        assert loaded["gmail"]["P1"]["id"] == "p1id"
        assert loaded["calendar"]["E1"]["id"] == "e1id"


# ── CLI guard ──────────────────────────────────────────────────────────────────

def test_client_secret_env_required() -> None:
    """_get_access_token raises if no token file found."""
    with tempfile.TemporaryDirectory() as td:
        fake_path = str(Path(td) / "nonexistent_token.json")
        with patch.dict(os.environ, {"OLO_SEEDER_TOKEN_FILE": fake_path}):
            from tests.olo.live_seed.seeder import _get_access_token
            with pytest.raises(SeederAccountError, match="No seeder token"):
                _get_access_token()
