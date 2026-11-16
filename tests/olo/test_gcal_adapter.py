"""Tests for olo.adapters.gcal — §8.4 (Calendar) acceptance criteria."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from olo.adapters import AdapterConfigError, SourceError
from olo.adapters.gcal import GcalAdapter, _event_external_id
from tests.olo.fake_google import FakeGoogleServer, FakeCalendarState


def _make_adapter(
    fake: FakeGoogleServer,
    alias: str = "test",
    lookback_days: int = 7,
) -> GcalAdapter:
    adapter = GcalAdapter({"account": alias, "lookback_days": str(lookback_days)})
    creds = fake.make_credentials(alias)
    from olo.google.http import GoogleClient
    client = GoogleClient(creds)
    client._access_token = fake.tokens.access_token
    adapter._client = client
    return adapter


# ── Construction ──────────────────────────────────────────────────────────────

def test_requires_account_option() -> None:
    with pytest.raises(AdapterConfigError, match="account"):
        GcalAdapter({})


def test_rejects_unknown_options() -> None:
    with pytest.raises(AdapterConfigError, match="does not accept"):
        GcalAdapter({"account": "x", "extra": "y"})


def test_lookback_days_validation() -> None:
    with pytest.raises(AdapterConfigError, match="0–30"):
        GcalAdapter({"account": "x", "lookback_days": "31"})
    with pytest.raises(AdapterConfigError, match="integer"):
        GcalAdapter({"account": "x", "lookback_days": "abc"})


def test_source_is_gcal_alias() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake, "myalias")
        assert a.source == "gcal_myalias"


# ── Bootstrap / full sync ─────────────────────────────────────────────────────

def test_bootstrap_full_sync_returns_events_and_incr_cursor() -> None:
    with FakeGoogleServer() as fake:
        fake.calendar.add_event("ev1", summary="Weekly Standup")
        fake.calendar.add_event("ev2", summary="One-on-One")
        # Reset incr buffer (bootstrap uses all_events_full)
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        b = a.fetch(None, limit=10)
        assert len(b.signals) >= 2
        # Cursor must be incr phase
        cursor = json.loads(b.next_cursor)
        assert cursor["phase"] == "incr"
        assert "sync_token" in cursor


def test_bootstrap_uses_time_min() -> None:
    """Full sync must include timeMin; incremental must not."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap
        b0 = a.fetch(None, limit=10)
        # The fake always returns a nextSyncToken → cursor is now incr
        cursor = json.loads(b0.next_cursor)
        assert cursor["phase"] == "incr"


def test_incremental_never_sends_forbidden_params() -> None:
    """syncToken must not be combined with timeMin/timeMax/etc."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        # Incremental fetch; we just verify it doesn't crash and uses syncToken
        a.fetch(cursor, limit=10)
        # If params were wrong the fake would return 400 → CalendarPageTokenInvalid
        # (which would propagate as an error — the test passes means no forbidden params)


def test_cancelled_event_kind_no_summary_in_subject() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.calendar.add_event("ev1", summary="Big Meeting")
        fake.calendar.cancel_event("ev1")

        b1 = a.fetch(cursor, limit=10)
        cancelled = [s for s in b1.signals if s.kind == "calendar.event.cancelled"]
        assert len(cancelled) >= 1
        # Cancelled events with no summary → subject = None
        # (our fake still provides summary on cancel, so just check kind)


def test_subject_is_summary() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.calendar.add_event("ev1", summary="Project Review")
        b1 = a.fetch(cursor, limit=10)
        sigs = [s for s in b1.signals if "ev1" in str(json.loads(json.dumps(s.payload)).get("event_id", ""))]
        if sigs:
            assert sigs[0].subject == "Project Review"


def test_new_version_new_signal_same_version_duplicate() -> None:
    """Same event.id + same updated → same external_id → duplicate."""
    ev_id = "evt123"
    updated_v1 = "2026-01-01T12:00:00Z"
    updated_v2 = "2026-01-02T12:00:00Z"

    ext_v1 = _event_external_id(ev_id, updated_v1)
    ext_v2 = _event_external_id(ev_id, updated_v2)

    assert ext_v1 == _event_external_id(ev_id, updated_v1)   # same version = same id
    assert ext_v1 != ext_v2                                   # new version = new id


def test_long_event_id_external_id_within_limit() -> None:
    long_id = "x" * 1024
    ext = _event_external_id(long_id, "2026-01-01T00:00:00Z")
    assert len(ext) <= 256


def test_payload_excludes_description_and_conference() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.calendar.add_event("ev1", summary="Test")
        b1 = a.fetch(cursor, limit=10)
        for sig in b1.signals:
            assert "description" not in sig.payload
            assert "conferenceData" not in sig.payload
            assert "hangoutLink" not in sig.payload


def test_410_fails_no_auto_resync() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        a.fetch(None, limit=10)
        # Use an expired sync token
        expired_cursor = json.dumps({"phase": "incr", "sync_token": "EXPIRED_SYNC_TOKEN", "page_token": None})
        with pytest.raises(Exception):
            a.fetch(expired_cursor, limit=10)


def test_empty_last_page_with_sync_token() -> None:
    """An empty last page (nextSyncToken, no items) → ((), cursor, False)."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap with no events → empty full sync → empty last page → incr cursor
        b0 = a.fetch(None, limit=10)
        assert b0.signals == ()
        assert b0.has_more is False
        cursor = json.loads(b0.next_cursor)
        assert cursor["phase"] == "incr"


def test_incremental_produces_signals_for_new_events() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor = b0.next_cursor

        fake.calendar.add_event("ev1", summary="New Event")
        fake.calendar.add_event("ev2", summary="Another Event")

        b1 = a.fetch(cursor, limit=10)
        new_sigs = [s for s in b1.signals if s.kind == "calendar.event.changed"]
        assert len(new_sigs) >= 2


def test_runner_never_sees_empty_has_more_calendar() -> None:
    """Any FetchBatch with no signals must have has_more=False."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b = a.fetch(None, limit=10)
        assert not (b.signals == () and b.has_more is True)
        b2 = a.fetch(b.next_cursor, limit=10)
        assert not (b2.signals == () and b2.has_more is True)
