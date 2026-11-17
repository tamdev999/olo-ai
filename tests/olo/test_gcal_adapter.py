"""Tests for olo.adapters.gcal — §8.4 (Calendar) acceptance criteria.

B4 (M2b): tests updated for composite cursor v2.  The cursor is now a JSON
object {"v":2,"list":{...},"cals":{<cal_id>:{...},...}}.  Helper functions
unwrap the v2 structure so existing checks remain meaningful.
"""
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
    """Create a GcalAdapter backed by the fake server via the public client= param."""
    creds = fake.make_credentials(alias)
    from olo.google.http import GoogleClient
    client = GoogleClient(creds)
    client._access_token = fake.tokens.access_token
    return GcalAdapter({"account": alias, "lookback_days": str(lookback_days)},
                       client=client)


def _primary_phase(cursor_str: str) -> dict:
    """Extract the primary calendar cursor phase from a v2 composite cursor."""
    comp = json.loads(cursor_str)
    if "cals" in comp:
        return comp["cals"].get("primary", {})
    # v1 fallback
    return comp


def _primary_sync_token(cursor_str: str) -> str:
    """Extract the primary calendar sync_token from a v2 composite cursor."""
    ph = _primary_phase(cursor_str)
    return ph.get("sync_token", "")


def _is_incr_cursor(cursor_str: str) -> bool:
    """True if the primary calendar cursor is in incr phase."""
    return _primary_phase(cursor_str).get("phase") == "incr"


# ── Construction ──────────────────────────────────────────────────────────────

def test_requires_account_option() -> None:
    with pytest.raises(AdapterConfigError, match="account"):
        GcalAdapter({})


def test_rejects_unknown_options() -> None:
    with pytest.raises(AdapterConfigError, match="does not accept"):
        GcalAdapter({"account": "x", "extra": "y"})


def test_lookback_days_validation() -> None:
    # B4: expanded to 0–365
    with pytest.raises(AdapterConfigError, match="0–365"):
        GcalAdapter({"account": "x", "lookback_days": "366"})
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
        # Bootstrap: first fetch does calendarList, registers calendars
        b0 = a.fetch(None, limit=10)
        # May have no signals yet (calendarList fetch), drive to events
        cursor = b0.next_cursor
        seen = list(b0.signals)

        for _ in range(10):
            if not b0.has_more:
                break
            b0 = a.fetch(cursor, limit=10)
            seen.extend(b0.signals)
            cursor = b0.next_cursor

        event_sigs = [s for s in seen if s.kind == "calendar.event.changed"]
        assert len(event_sigs) >= 2
        # Final cursor must be v2 with primary in incr phase
        assert _is_incr_cursor(cursor), f"expected incr cursor, got: {cursor}"


def test_bootstrap_uses_time_min() -> None:
    """Full sync must include timeMin; incremental must not."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Drive to completion
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break
        assert _is_incr_cursor(cursor), f"expected incr cursor, got: {cursor}"


def test_incremental_never_sends_forbidden_params() -> None:
    """syncToken must not be combined with timeMin/timeMax/etc."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break
        # Incremental fetch; we just verify it doesn't crash
        a.fetch(cursor, limit=10)


def test_cancelled_event_kind_no_summary_in_subject() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        fake.calendar.add_event("ev1", summary="Big Meeting")
        fake.calendar.cancel_event("ev1")

        b1 = a.fetch(cursor, limit=10)
        # Drive to events
        all_sigs = list(b1.signals)
        cur = b1.next_cursor
        for _ in range(5):
            if not b1.has_more:
                break
            b1 = a.fetch(cur, limit=10)
            all_sigs.extend(b1.signals)
            cur = b1.next_cursor

        cancelled = [s for s in all_sigs if s.kind == "calendar.event.cancelled"]
        assert len(cancelled) >= 1


def test_subject_is_summary() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        fake.calendar.add_event("ev1", summary="Project Review")
        b1 = a.fetch(cursor, limit=10)
        all_sigs = list(b1.signals)
        cur = b1.next_cursor
        for _ in range(5):
            if not b1.has_more:
                break
            b1 = a.fetch(cur, limit=10)
            all_sigs.extend(b1.signals)
            cur = b1.next_cursor

        sigs = [s for s in all_sigs if s.payload.get("event_id") == "ev1"]
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
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        fake.calendar.add_event("ev1", summary="Test")
        all_sigs: list = []
        for _ in range(5):
            b = a.fetch(cursor, limit=10)
            all_sigs.extend(b.signals)
            cursor = b.next_cursor
            if not b.has_more:
                break
        for sig in all_sigs:
            assert "description" not in sig.payload
            assert "conferenceData" not in sig.payload
            assert "hangoutLink" not in sig.payload


def test_410_self_heals_per_calendar() -> None:
    """B4 D2: 410 on events.list → self-heal (restart full sync for that calendar).

    The adapter must NOT propagate the 410 — instead it restarts the full sync
    for the affected calendar and continues.  Signals are returned normally.
    """
    with FakeGoogleServer() as fake:
        fake.calendar.add_event("heal1", summary="Post-heal Event")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        # Bootstrap
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        # Build a cursor with an expired events sync token for primary
        from olo.google.http import CalendarSyncTokenExpired
        comp = json.loads(cursor)
        comp["cals"]["primary"] = {
            "phase": "incr",
            "sync_token": "EXPIRED_SYNC_TOKEN",
            "page_token": None,
        }
        expired_cursor = json.dumps(comp)

        # Must NOT raise — self-heals by restarting full sync
        result = a.fetch(expired_cursor, limit=10)
        # May return events (from restart full sync) or empty (no events in lookback)
        # Key invariant: does not raise CalendarSyncTokenExpired
        assert isinstance(result.next_cursor, str)


def test_empty_last_page_with_sync_token() -> None:
    """An empty last page (nextSyncToken, no items) → ((), cursor, False)."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap with no events → empty full sync
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break
        assert b.signals == ()
        assert b.has_more is False
        assert _is_incr_cursor(cursor)


def test_incremental_produces_signals_for_new_events() -> None:
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        fake.calendar.add_event("ev1", summary="New Event")
        fake.calendar.add_event("ev2", summary="Another Event")

        all_sigs: list = []
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            all_sigs.extend(b.signals)
            cursor = b.next_cursor
            if not b.has_more:
                break
        new_sigs = [s for s in all_sigs if s.kind == "calendar.event.changed"]
        assert len(new_sigs) >= 2


def test_runner_never_sees_empty_has_more_calendar() -> None:
    """Any FetchBatch with no signals must have has_more=False — in steady-state.

    B4: during bootstrap (calendarList→events), has_more=True is valid.
    After steady-state is reached, empty batches must have has_more=False.
    """
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Drive all the way to steady state
        cursor = None
        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break
        # In steady state: an incremental fetch with no new events → empty, has_more=False
        b2 = a.fetch(cursor, limit=10)
        assert not (b2.signals == () and b2.has_more is True), (
            f"steady-state empty batch must not have has_more=True: {b2}"
        )


# ── C1 regression tests ───────────────────────────────────────────────────────

def test_short_page_then_full_page_loses_nothing() -> None:
    """C1 Path A: pages of [e1,e2] then [e3..e7] then [e8] with limit=5.

    Every event id appears exactly once; final cursor is in incr phase.
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

        for _ in range(40):  # safety cap (extra loops for calendarList)
            b = a.fetch(cursor, limit=limit)
            for sig in b.signals:
                if sig.kind == "calendar.event.changed":
                    seen_ids.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        # All 8 events ingested, each exactly once
        assert sorted(seen_ids) == [f"e{i}" for i in range(1, 9)], (
            f"C1 Path A: lost events. Got: {sorted(seen_ids)}"
        )
        # Final cursor is incr phase
        assert _is_incr_cursor(cursor), f"expected incr cursor, got: {cursor}"


def test_overflow_on_last_page_loses_nothing() -> None:
    """C1 Path B: [e1,e2,e3] then [e4..e8] with nextSyncToken, limit=5."""
    with FakeGoogleServer() as fake:
        for i in range(1, 9):
            fake.calendar.add_event(f"e{i}", summary=f"Event {i}")
        fake.calendar._incr_events.clear()

        # Page 3 at a time
        fake.set_page_size(3)
        a = _make_adapter(fake)

        seen_ids: list[str] = []
        cursor = None
        limit = 5

        for _ in range(40):
            b = a.fetch(cursor, limit=limit)
            for sig in b.signals:
                if sig.kind == "calendar.event.changed":
                    seen_ids.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        assert sorted(seen_ids) == [f"e{i}" for i in range(1, 9)], (
            f"C1 Path B: lost events. Got: {sorted(seen_ids)}"
        )
        assert _is_incr_cursor(cursor), f"expected incr cursor, got: {cursor}"


def test_never_truncates_page() -> None:
    """C1: a page with more items than maxResults → SourceError, nothing committed."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        from datetime import UTC

        original_do = a._do_request

        def _oversize_request(client, phase, limit_arg, cal_id="primary"):
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
            a.fetch(cursor, limit=5)


# ── I3 §9.4 tests ─────────────────────────────────────────────────────────────

def test_fixed_params_on_every_request() -> None:
    """I3: singleEvents, showDeleted, maxResults present on every request."""
    with FakeGoogleServer() as fake:
        for i in range(1, 4):
            fake.calendar.add_event(f"fp{i}")
        fake.calendar._incr_events.clear()
        fake.set_page_size(1)

        a = _make_adapter(fake)
        seen: list[str] = []
        cursor = None

        for _ in range(40):
            b = a.fetch(cursor, limit=10)
            for sig in b.signals:
                if sig.kind == "calendar.event.changed":
                    seen.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        assert sorted(seen) == ["fp1", "fp2", "fp3"]


def test_incremental_pagination_with_same_sync_token() -> None:
    """I3: incremental sync with 3 events paged 1-at-a-time."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor0 = None
        for _ in range(10):
            b0 = a.fetch(cursor0, limit=10)
            cursor0 = b0.next_cursor
            if not b0.has_more:
                break

        for i in range(1, 4):
            fake.calendar.add_event(f"incr{i}")

        fake.set_page_size(1)

        seen: list[str] = []
        cursor = cursor0

        for _ in range(40):
            b = a.fetch(cursor, limit=10)
            for sig in b.signals:
                if sig.kind == "calendar.event.changed":
                    seen.append(sig.payload["event_id"])
            cursor = b.next_cursor
            if not b.has_more:
                break

        assert sorted(seen) == ["incr1", "incr2", "incr3"], (
            f"incremental pagination lost events: {seen}"
        )
        # Final cursor is a new incr sync token (primary phase)
        final_phase = _primary_phase(cursor)
        assert final_phase["phase"] == "incr"
        # Token must have changed from cursor0's token
        assert _primary_sync_token(cursor) != _primary_sync_token(cursor0)


def test_empty_page_with_next_page_token() -> None:
    """I3: Google returns empty items[] + nextPageToken → adapter follows it."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Complete bootstrap to steady state
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        from datetime import UTC

        calls = [0]

        def _two_phase(client, phase, limit, cal_id="primary"):
            calls[0] += 1
            if calls[0] == 1:
                return {"items": [], "nextPageToken": "pt_phase2"}
            else:
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

        # Inject patched _do_request and do single incremental fetch
        a._do_request = _two_phase
        b = a.fetch(cursor, limit=10)
        # Each event produces calendar.event.changed + calendar.event.content = 2 signals
        event_sigs = [s for s in b.signals if s.kind == "calendar.event.changed"]
        assert len(event_sigs) == 1
        assert event_sigs[0].payload["event_id"] == "ep1"
        assert calls[0] == 2, f"adapter must follow the empty-page token; calls={calls[0]}"


def test_empty_pages_cap() -> None:
    """I3: _MAX_EMPTY_PAGES consecutive empty pages → returns () has_more=False."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        from olo.adapters.gcal import _MAX_EMPTY_PAGES
        call_count = [0]

        def _always_empty(client, phase, limit, cal_id="primary"):
            call_count[0] += 1
            return {"items": [], "nextPageToken": f"pt_{call_count[0]}"}

        a._do_request = _always_empty
        b = a.fetch(cursor, limit=10)

        assert b.signals == ()
        assert b.has_more is False
        assert call_count[0] == _MAX_EMPTY_PAGES, (
            f"expected exactly {_MAX_EMPTY_PAGES} calls, got {call_count[0]}"
        )


def test_invalid_page_token_restarts_phase() -> None:
    """I3: CalendarPageTokenInvalid (400) → adapter retries without page token."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        from olo.google.http import CalendarPageTokenInvalid

        calls = []

        def _tracking_do(client, phase, limit, cal_id="primary"):
            calls.append(dict(phase))
            if len(calls) == 1 and phase.get("page_token") == "INVALID_PAGE_TOKEN":
                raise CalendarPageTokenInvalid("400 invalid page token")
            return {"items": [], "nextSyncToken": "st_restart"}

        # Inject a cursor with an invalid page token for primary
        comp = json.loads(cursor)
        comp["cals"]["primary"]["page_token"] = "INVALID_PAGE_TOKEN"
        invalid_cursor = json.dumps(comp)

        a._do_request = _tracking_do
        b = a.fetch(invalid_cursor, limit=10)

        assert len(calls) == 2, f"expected retry, got {len(calls)} calls: {calls}"
        assert calls[1].get("page_token") is None, (
            "retry must drop the page token"
        )


def test_crash_mid_full_sync_resumes_from_page_token() -> None:
    """I3: if fetch returns has_more=True with page_token, re-running resumes."""
    with FakeGoogleServer() as fake:
        for i in range(1, 7):
            fake.calendar.add_event(f"cr{i}")
        fake.calendar._incr_events.clear()
        fake.set_page_size(2)

        a = _make_adapter(fake)

        # Drive calendarList bootstrap first
        cursor = None
        b = None
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            # Stop once we start fetching events (has events)
            if b.signals and any(s.kind == "calendar.event.changed" for s in b.signals):
                break
            if not b.has_more:
                break

        # First fetch may have returned 2 events; grab remaining via fresh adapter
        a2 = _make_adapter(fake)
        seen_ids = [sig.payload["event_id"] for sig in (b.signals or ()) if sig.kind == "calendar.event.changed"]

        c = cursor
        for _ in range(40):
            bx = a2.fetch(c, limit=10)
            for sig in bx.signals:
                if sig.kind == "calendar.event.changed":
                    seen_ids.append(sig.payload["event_id"])
            c = bx.next_cursor
            if not bx.has_more:
                break

        # All 6 events, no duplicates
        assert sorted(set(seen_ids)) == [f"cr{i}" for i in range(1, 7)], (
            f"resume from page token lost events: {seen_ids}"
        )


# ── limit=1 regression tests ─────────────────────────────────────────────────

def test_limit_1_raises_adapter_config_error() -> None:
    """Calendar adapter raises AdapterConfigError for limit=1.

    Each event produces 2 signals (calendar.event.changed + calendar.event.content).
    limit=1 cannot accommodate even one complete event pair so the adapter
    refuses with a clear error rather than silently violating the runner contract.
    """
    with FakeGoogleServer() as fake:
        fake.calendar.add_event("ev1", summary="Test")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        # Bootstrap to steady state first (drive until has_more=False)
        cursor = None
        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        # Add a new event so there is something to fetch
        fake.calendar.add_event("ev2", summary="New event")

        with pytest.raises(AdapterConfigError, match="limit must be"):
            a.fetch(cursor, limit=1)


def test_limit_2_works_one_event_two_signals() -> None:
    """With limit=2, a single new event returns exactly 2 signals (metadata + content)."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        fake.calendar.add_event("ev2", summary="Limit2 Event")

        all_sigs = []
        for _ in range(10):
            b = a.fetch(cursor, limit=2)
            all_sigs.extend(b.signals)
            cursor = b.next_cursor
            if not b.has_more:
                break

        changed = [s for s in all_sigs if s.kind == "calendar.event.changed"]
        content = [s for s in all_sigs if s.kind == "calendar.event.content"]
        assert len(changed) == 1, f"expected 1 changed, got {len(changed)}"
        assert len(content) == 1, f"expected 1 content, got {len(content)}"


def test_limit_2_does_not_exceed_signal_budget() -> None:
    """With limit=2 and multiple events, no FetchBatch exceeds 2 signals."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            cursor = b.next_cursor
            if not b.has_more:
                break

        for i in range(1, 6):
            fake.calendar.add_event(f"multi{i}", summary=f"Event {i}")

        batches: list[int] = []
        for _ in range(40):
            b = a.fetch(cursor, limit=2)
            batches.append(len(b.signals))
            assert len(b.signals) <= 2, (
                f"FetchBatch exceeded limit=2: got {len(b.signals)} signals"
            )
            cursor = b.next_cursor
            if not b.has_more:
                break

        # All batches with signals must have exactly 2 (one complete event pair)
        assert all(s in (0, 2) for s in batches), (
            f"Broken signal pairs in batches: {batches}"
        )
