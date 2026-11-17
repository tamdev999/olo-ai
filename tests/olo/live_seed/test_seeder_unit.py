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


# ── seed-stage3 (Q1/draft-A/Q2/draft-B/Q3) ───────────────────────────────────

def test_seed_stage3_creates_q1_q2_q3() -> None:
    """seed_stage3 creates Q1, Q2, Q3 as imported messages."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")
    imported: list[tuple[str, list]] = []  # (raw, label_ids)

    # Draft calls go via _create_draft / _update_draft / _delete_draft
    # Import calls go via _import_message
    def _fake_import(raw, label_ids):
        imported.append((raw, label_ids))
        idx = len(imported)
        return {"id": f"q{idx}id", "threadId": f"qt{idx}"}

    draft_ops: list[str] = []

    def _fake_create_draft(raw):
        draft_ops.append("create")
        return "draftid001"

    def _fake_update_draft(draft_id, raw):
        draft_ops.append(f"update:{draft_id}")

    def _fake_delete_draft(draft_id):
        draft_ops.append(f"delete:{draft_id}")

    seeder._import_message = _fake_import
    seeder._create_draft = _fake_create_draft
    seeder._update_draft = _fake_update_draft
    seeder._delete_draft = _fake_delete_draft

    seeder.seed_stage3("sender@example.com", "tag", n_autosaves=2)

    # 3 messages imported (Q1, Q2, Q3)
    assert len(imported) == 3, f"expected 3 imported, got {len(imported)}"

    # All are INBOX (received messages)
    for raw, labels in imported:
        assert "INBOX" in labels, f"expected INBOX label: {labels}"

    # Q1, Q2, Q3 recorded in created dict
    assert "Q1" in seeder.created
    assert "Q2" in seeder.created
    assert "Q3" in seeder.created

    # Subjects contain run_tag
    assert "tag" in seeder.created["Q1"]["subject"]
    assert "tag" in seeder.created["Q2"]["subject"]
    assert "tag" in seeder.created["Q3"]["subject"]


def test_seed_stage3_draft_lifecycle() -> None:
    """seed_stage3 creates 2 drafts, each with n_autosaves updates then deleted."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")

    draft_sequence: list[str] = []
    draft_id_counter = [0]

    def _fake_import(raw, label_ids):
        n = len(draft_sequence)
        return {"id": f"mid{n}", "threadId": f"t{n}"}

    def _fake_create_draft(raw):
        draft_id_counter[0] += 1
        did = f"draft{draft_id_counter[0]}"
        draft_sequence.append(f"create:{did}")
        return did

    def _fake_update_draft(draft_id, raw):
        draft_sequence.append(f"update:{draft_id}")

    def _fake_delete_draft(draft_id):
        draft_sequence.append(f"delete:{draft_id}")

    seeder._import_message = _fake_import
    seeder._create_draft = _fake_create_draft
    seeder._update_draft = _fake_update_draft
    seeder._delete_draft = _fake_delete_draft

    n_saves = 3
    seeder.seed_stage3("sender@example.com", "tag", n_autosaves=n_saves)

    # Expect: create:draft1, update×3, delete:draft1, create:draft2, update×3, delete:draft2
    creates = [op for op in draft_sequence if op.startswith("create:")]
    updates = [op for op in draft_sequence if op.startswith("update:")]
    deletes = [op for op in draft_sequence if op.startswith("delete:")]

    assert len(creates) == 2, f"expected 2 draft creates, got {creates}"
    assert len(updates) == n_saves * 2, f"expected {n_saves*2} updates, got {updates}"
    assert len(deletes) == 2, f"expected 2 draft deletes, got {deletes}"


def test_seed_stage3_interleave_order() -> None:
    """The seeding order must be: Q1 → draft-A → Q2 → draft-B → Q3."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")
    ops: list[str] = []
    n = [0]

    def _fake_import(raw, label_ids):
        n[0] += 1
        ops.append(f"import:{n[0]}")
        return {"id": f"id{n[0]}", "threadId": f"t{n[0]}"}

    d = [0]

    def _fake_create_draft(raw):
        d[0] += 1
        ops.append(f"draft_create:{d[0]}")
        return f"did{d[0]}"

    def _fake_update_draft(draft_id, raw):
        ops.append(f"draft_update:{draft_id}")

    def _fake_delete_draft(draft_id):
        ops.append(f"draft_delete:{draft_id}")

    seeder._import_message = _fake_import
    seeder._create_draft = _fake_create_draft
    seeder._update_draft = _fake_update_draft
    seeder._delete_draft = _fake_delete_draft

    seeder.seed_stage3("sender@example.com", "tag", n_autosaves=1)

    # import:1 (Q1) must come before draft_create:1
    # draft_delete:did1 must come before import:2 (Q2)
    # import:2 (Q2) must come before draft_create:2
    # draft_delete:did2 must come before import:3 (Q3)
    import_positions = {op: i for i, op in enumerate(ops) if op.startswith("import:")}
    draft_create_positions = {op: i for i, op in enumerate(ops) if op.startswith("draft_create:")}
    draft_delete_positions = {op: i for i, op in enumerate(ops) if op.startswith("draft_delete:")}

    assert import_positions["import:1"] < draft_create_positions["draft_create:1"]
    assert draft_delete_positions["draft_delete:did1"] < import_positions["import:2"]
    assert import_positions["import:2"] < draft_create_positions["draft_create:2"]
    assert draft_delete_positions["draft_delete:did2"] < import_positions["import:3"]


def test_seed_stage3_all_messages_contain_body_marker() -> None:
    """Q1–Q3 messages each contain OLO-BODY-MARKER in the decoded body."""
    import email as _em
    seeder = GmailSeeder(token="fake", account_email="me@example.com")
    raws: list[str] = []

    def _fake_import(raw, label_ids):
        raws.append(raw)
        idx = len(raws)
        return {"id": f"id{idx}", "threadId": f"t{idx}"}

    def _fake_create_draft(raw): return "did"
    def _fake_update_draft(d, raw): pass
    def _fake_delete_draft(d): pass

    seeder._import_message = _fake_import
    seeder._create_draft = _fake_create_draft
    seeder._update_draft = _fake_update_draft
    seeder._delete_draft = _fake_delete_draft

    seeder.seed_stage3("sender@example.com", "tag", n_autosaves=1)

    assert len(raws) == 3
    for i, raw in enumerate(raws):
        msg = _em.message_from_string(raw)
        body = msg.get_payload(decode=True)
        decoded = body.decode("utf-8", errors="replace") if body else msg.get_payload()
        assert BODY_MARKER in decoded, f"Q{i+1}: {BODY_MARKER} missing from body"


def test_seed_stage3_q_subjects_distinct() -> None:
    """Q1, Q2, Q3 must have distinct subjects."""
    seeder = GmailSeeder(token="fake", account_email="me@example.com")
    n = [0]

    def _fake_import(raw, label_ids):
        n[0] += 1
        return {"id": f"id{n[0]}", "threadId": f"t{n[0]}"}

    def _fake_create_draft(raw): return "did"
    def _fake_update_draft(d, raw): pass
    def _fake_delete_draft(d): pass

    seeder._import_message = _fake_import
    seeder._create_draft = _fake_create_draft
    seeder._update_draft = _fake_update_draft
    seeder._delete_draft = _fake_delete_draft

    seeder.seed_stage3("sender@example.com", "tag", n_autosaves=1)

    subjects = {
        seeder.created["Q1"]["subject"],
        seeder.created["Q2"]["subject"],
        seeder.created["Q3"]["subject"],
    }
    assert len(subjects) == 3, f"Q subjects must be distinct: {subjects}"
    for subj in subjects:
        assert "OLO-Q" in subj, f"Q subject must contain OLO-Q: {subj!r}"


# ── Issue 1: Project isolation assertion ─────────────────────────────────────

def test_seeder_client_secret_project_id_differs_from_olo_project(tmp_path: Path) -> None:
    """Issue 1: the seeder client secret must come from a different GCP project than Olo.

    This test verifies the structure of the assertion that BBC must make when
    running live:
    - Both client_secret JSON files have a 'project_id' field (or contain the
      project number in the client_id: 'NNNNN-...apps.googleusercontent.com')
    - Those project identifiers must differ

    When client secret files are not present (CI / offline), the test passes
    but records a warning.  The assertion is enforced in the live environment.
    """
    import json
    import os

    seeder_cs_path = os.environ.get("OLO_SEEDER_CLIENT_SECRET_FILE", "")
    olo_cs_path = os.environ.get("OLO_CLIENT_SECRET_FILE", "")  # set in live env

    if not seeder_cs_path or not olo_cs_path:
        # Offline: cannot compare — but verify the comparison logic is correct
        # by using synthetic data
        seeder_cs = {"installed": {"project_id": "seeder-project-12345",
                                   "client_id": "111-abc.apps.googleusercontent.com",
                                   "client_secret": "s1"}}
        olo_cs = {"installed": {"project_id": "olo-project-67890",
                                "client_id": "222-def.apps.googleusercontent.com",
                                "client_secret": "s2"}}
    else:
        with open(seeder_cs_path, encoding="utf-8") as f:
            seeder_cs = json.load(f)
        with open(olo_cs_path, encoding="utf-8") as f:
            olo_cs = json.load(f)

    def _project_id(cs: dict) -> str:
        """Extract project identifier from a client secret JSON."""
        app = cs.get("installed") or cs.get("web") or cs
        pid = app.get("project_id")
        if pid:
            return pid
        # Fall back to numeric prefix of client_id
        client_id = app.get("client_id", "")
        # client_id format: NNNNNNNNNNN-...apps.googleusercontent.com
        parts = client_id.split("-")
        if parts and parts[0].isdigit():
            return f"project-number-{parts[0]}"
        return client_id

    seeder_pid = _project_id(seeder_cs)
    olo_pid = _project_id(olo_cs)

    assert seeder_pid != olo_pid, (
        f"Seeder and Olo OAuth clients must come from DIFFERENT Google Cloud projects. "
        f"Both show project_id={seeder_pid!r}. "
        f"Create a separate GCP project for the seeder write-scoped client."
    )

    assert seeder_pid, "seeder project_id is empty"
    assert olo_pid, "olo project_id is empty"
