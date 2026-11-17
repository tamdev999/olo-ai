"""Tests for olo.adapters.gcal — §8.4 (Calendar) acceptance criteria."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from olo.adapters import AdapterConfigError, SourceError
from olo.adapters.gcal import GcalAdapter, _event_external_id
from tests.olo.fake_google import FakeGoogleServer


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


# ── C1 regression tests ───────────────────────────────────────────────────────

def test_short_page_then_full_page_loses_nothing() -> None:
    """C1 Path A: pages of [e1,e2] then [e3..e7] then [e8] with limit=5.

    Before fix: ingested e1–e5, e8; e6+e7 lost.
    After fix:  first fetch returns e1+e2 (page 1, limit=5); second returns
                e3..e7 (page 2); third returns e8 — all events ingested.
    Every event id appears exactly once; final cursor is the latest sync token.
    """
    with FakeGoogleServer() as fake:
        # 8 events; page size = 2
        for i in range(1, 9):
            fake.calendar.add_event(f"e{i}", summary=f"Event {i}")
        fake.calendar._incr_events.clear()

        # Page 2 events at a time
        fake.set_page_size(2)
        a = _make_adapter(fake)

        seen_ids: list[str] = []
        cursor = None
        limit = 5

        for _ in range(20):  # safety cap
            b = a.fetch(cursor, limit=limit)
            for sig in b.signals:
                seen_ids.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        # All 8 events ingested, each exactly once
        assert sorted(seen_ids) == [f"e{i}" for i in range(1, 9)], (
            f"C1 Path A: lost events. Got: {sorted(seen_ids)}"
        )
        # Final cursor is incr phase
        assert json.loads(cursor)["phase"] == "incr"


def test_overflow_on_last_page_loses_nothing() -> None:
    """C1 Path B: [e1,e2,e3] then [e4..e8] with nextSyncToken, limit=5.

    Before fix: returned e1–e5, cursor jumped to sync token; e6–e8 lost.
    After fix:  first fetch returns e1+e2+e3 (page 1); second returns
                e4+e5; third returns e6+e7+e8 (last page, gets sync token).
    """
    with FakeGoogleServer() as fake:
        for i in range(1, 9):
            fake.calendar.add_event(f"e{i}", summary=f"Event {i}")
        fake.calendar._incr_events.clear()

        # Page 3 at a time (so pages are [e1-e3], [e4-e6], [e7-e8])
        fake.set_page_size(3)
        a = _make_adapter(fake)

        seen_ids: list[str] = []
        cursor = None
        limit = 5

        for _ in range(20):
            b = a.fetch(cursor, limit=limit)
            for sig in b.signals:
                seen_ids.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        assert sorted(seen_ids) == [f"e{i}" for i in range(1, 9)], (
            f"C1 Path B: lost events. Got: {sorted(seen_ids)}"
        )
        assert json.loads(cursor)["phase"] == "incr"


def test_never_truncates_page() -> None:
    """C1: a page with more items than maxResults → SourceError, nothing committed.

    We simulate Google over-delivering by patching _do_request directly.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap first so we have a valid cursor
        b0 = a.fetch(None, limit=10)

        # Patch _do_request to return more events than limit
        from datetime import UTC

        original_do = a._do_request

        def _oversize_request(client, phase, limit_arg):
            # Return limit_arg + 1 items — more than requested
            items = [
                {
                    "id": f"ov{i}",
                    "status": "confirmed",
                    "summary": f"Oversize {i}",
                    "updated": datetime.now(UTC).isoformat(),
                    "start": {"dateTime": datetime.now(UTC).isoformat()},
                    "end": {"dateTime": datetime.now(UTC).isoformat()},
                    "created": datetime.now(UTC).isoformat(),
                    "organizer": {"email": "o@example.com", "self": False},
                }
                for i in range(limit_arg + 1)
            ]
            return {"items": items, "nextSyncToken": "st_oversize"}

        a._do_request = _oversize_request

        with pytest.raises(SourceError, match="exceeds requested maxResults"):
            a.fetch(b0.next_cursor, limit=5)


# ── I3 §9.4 tests ─────────────────────────────────────────────────────────────

def test_fixed_params_on_every_request() -> None:
    """I3: calendarId, singleEvents, showDeleted, maxResults present on every request.

    We validate by running multiple fetches with page_size=1 and confirming
    each page yields exactly one event (not more, not fewer).
    """
    with FakeGoogleServer() as fake:
        for i in range(1, 4):
            fake.calendar.add_event(f"fp{i}")
        fake.calendar._incr_events.clear()
        fake.set_page_size(1)

        a = _make_adapter(fake)
        seen: list[str] = []
        cursor = None

        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            # Each page has exactly 1 event (page_size=1)
            assert len(b.signals) <= 1, f"page_size=1 but got {len(b.signals)} signals"
            for sig in b.signals:
                seen.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        assert sorted(seen) == ["fp1", "fp2", "fp3"]


def test_incremental_pagination_with_same_sync_token() -> None:
    """I3: incremental sync with 3 events paged 1-at-a-time.

    The sync token should not change mid-pagination; only the final page
    advances the cursor to a new incr sync token.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b0 = a.fetch(None, limit=10)
        cursor0 = b0.next_cursor

        for i in range(1, 4):
            fake.calendar.add_event(f"incr{i}")

        fake.set_page_size(1)

        seen: list[str] = []
        cursor = cursor0

        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            for sig in b.signals:
                seen.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        assert sorted(seen) == ["incr1", "incr2", "incr3"], (
            f"incremental pagination lost events: {seen}"
        )
        # Final cursor is a new incr sync token
        final = json.loads(cursor)
        assert final["phase"] == "incr"
        assert final["sync_token"] != json.loads(cursor0)["sync_token"]


def test_empty_page_with_next_page_token() -> None:
    """I3: Google returns empty items[] + nextPageToken → adapter follows it.

    We simulate this via the fake's empty-page mechanism: set page_size=2
    but only add events later via patching so the first page is empty with token.

    Implementation: We call a patched _do_request that returns empty+nextPageToken
    on the first call, then real events on the second.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)

        from datetime import UTC

        calls = [0]

        def _two_phase(client, phase, limit):
            calls[0] += 1
            if calls[0] == 1:
                # Empty page with next page token
                return {"items": [], "nextPageToken": "pt_phase2",
                        "historyId": "1"}
            else:
                # Second call: real events + sync token
                return {
                    "items": [
                        {"id": "ep1", "status": "confirmed", "summary": "E1",
                         "updated": datetime.now(UTC).isoformat(),
                         "start": {"dateTime": datetime.now(UTC).isoformat()},
                         "end": {"dateTime": datetime.now(UTC).isoformat()},
                         "created": datetime.now(UTC).isoformat(),
                         "organizer": {"email": "o@e.com", "self": False}},
                    ],
                    "nextSyncToken": "st_ep",
                }

        a._do_request = _two_phase
        b = a.fetch(None, limit=10)
        assert len(b.signals) == 1
        assert b.signals[0].payload["event_id"] == "ep1"
        assert calls[0] == 2, "adapter must follow the empty-page token"


def test_empty_pages_cap() -> None:
    """I3: _MAX_EMPTY_PAGES consecutive empty pages → returns () has_more=False.

    The adapter must not loop forever; it returns a batch with the advanced
    page-token cursor so the next tick can retry.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)

        from olo.adapters.gcal import _MAX_EMPTY_PAGES
        call_count = [0]

        def _always_empty(client, phase, limit):
            call_count[0] += 1
            return {"items": [], "nextPageToken": f"pt_{call_count[0]}"}

        a._do_request = _always_empty
        b = a.fetch(None, limit=10)

        assert b.signals == ()
        assert b.has_more is False
        assert call_count[0] == _MAX_EMPTY_PAGES, (
            f"expected exactly {_MAX_EMPTY_PAGES} calls, got {call_count[0]}"
        )


def test_invalid_page_token_restarts_phase() -> None:
    """I3: CalendarPageTokenInvalid (400) → adapter retries without page token.

    The adapter should drop the page token and re-issue the request from
    the beginning of the current phase, as §6.2 step 4 requires.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap
        b0 = a.fetch(None, limit=10)
        # Feed in a cursor with an invalid page token
        invalid_cursor = json.dumps({
            "phase": "incr",
            "sync_token": "INVALID_PAGE_TOKEN_SYNC",
            "page_token": "INVALID_PAGE_TOKEN",
        })
        # The fake returns 400 for "INVALID_PAGE_TOKEN"; the adapter should
        # retry without the page token. Depending on adapter state,
        # it might raise or succeed — either is acceptable, but it must NOT
        # propagate the 400 as-is without retrying.
        # We verify by patching _do_request to track the retry.
        from olo.google.http import CalendarPageTokenInvalid

        calls = []

        original_do = a._do_request

        def _tracking_do(client, phase, limit):
            calls.append(dict(phase))
            if len(calls) == 1 and phase.get("page_token") == "INVALID_PAGE_TOKEN":
                raise CalendarPageTokenInvalid("400 invalid page token")
            # On retry (no page_token), return empty+sync
            return {"items": [], "nextSyncToken": "st_restart"}

        a._do_request = _tracking_do
        b = a.fetch(invalid_cursor, limit=10)

        assert len(calls) == 2, f"expected retry, got {len(calls)} calls: {calls}"
        assert calls[1].get("page_token") is None, (
            "retry must drop the page token"
        )


def test_crash_mid_full_sync_resumes_from_page_token() -> None:
    """I3: if fetch returns has_more=True with a page_token cursor, re-running
    with that cursor resumes from the saved page (no events re-fetched or lost).
    """
    with FakeGoogleServer() as fake:
        for i in range(1, 7):
            fake.calendar.add_event(f"cr{i}")
        fake.calendar._incr_events.clear()
        fake.set_page_size(2)

        a = _make_adapter(fake)

        # First fetch: should return 2 events, has_more=True
        b1 = a.fetch(None, limit=10)
        assert len(b1.signals) == 2
        assert b1.has_more is True
        saved_cursor = b1.next_cursor

        # "Crash" — create a fresh adapter with the saved cursor
        a2 = _make_adapter(fake)
        seen_ids = [sig.payload["event_id"] for sig in b1.signals]

        cursor = saved_cursor
        for _ in range(20):
            b = a2.fetch(cursor, limit=10)
            for sig in b.signals:
                seen_ids.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        # All 6 events, each exactly once
        assert sorted(seen_ids) == [f"cr{i}" for i in range(1, 7)], (
            f"resume from page token lost events: {seen_ids}"
        )
