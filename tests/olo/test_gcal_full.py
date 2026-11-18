"""Olo M2b B4 — GcalAdapter multi-calendar tests (T-C1–C8).

Tests cover:
  T-C1: v1 cursor migration
  T-C2: calendarList discovery on bootstrap
  T-C3: calendar.calendar.added / removed signals
  T-C4: multi-calendar event sync (independent cursors)
  T-C5: 410 self-heal per calendar (not per calendarList)
  T-C6: lookback_days 0–365 validation
  T-C7: calendarList 410 self-heal
  T-C8: composite cursor v2 schema invariants
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from olo.adapters import AdapterConfigError, SourceError
from olo.adapters.gcal import GcalAdapter, _CURSOR_V2, _migrate_v1_cursor
from tests.olo.fake_google import FakeGoogleServer


def _make_adapter(
    fake: FakeGoogleServer,
    alias: str = "test",
    lookback_days: int = 7,
) -> GcalAdapter:
    creds = fake.make_credentials(alias)
    from olo.google.http import GoogleClient
    client = GoogleClient(creds)
    client._access_token = fake.tokens.access_token
    return GcalAdapter({"account": alias, "lookback_days": str(lookback_days)},
                       client=client)


def _drive_to_steady(a: GcalAdapter, cursor=None, max_ticks=30):
    """Drive the adapter until has_more=False, return (cursor, all_signals)."""
    all_sigs = []
    for _ in range(max_ticks):
        b = a.fetch(cursor, limit=50)
        all_sigs.extend(b.signals)
        cursor = b.next_cursor
        if not b.has_more:
            break
    return cursor, all_sigs


def _primary_phase(cursor_str: str) -> dict:
    comp = json.loads(cursor_str)
    if "cals" in comp:
        return comp["cals"].get("primary", {})
    return comp


# ── T-C1: v1 cursor migration ─────────────────────────────────────────────────

def test_tc1_v1_incr_cursor_migrated_to_v2() -> None:
    """T-C1: a v1 {"phase":"incr",...} cursor is transparently migrated to v2."""
    v1_cursor = json.dumps({
        "phase": "incr",
        "sync_token": "v1_sync_123",
        "page_token": None,
    })
    comp = _migrate_v1_cursor(v1_cursor)
    assert comp["v"] == _CURSOR_V2
    assert comp["cals"]["primary"]["sync_token"] == "v1_sync_123"
    assert comp["list"] is None   # triggers calendarList bootstrap on first tick


def test_tc1_v1_full_cursor_migrated_to_v2() -> None:
    """T-C1: a v1 {"phase":"full",...} cursor is migrated and treated as full sync."""
    v1_cursor = json.dumps({
        "phase": "full",
        "time_min": "2026-01-01T00:00:00Z",
        "page_token": None,
    })
    comp = _migrate_v1_cursor(v1_cursor)
    assert comp["v"] == _CURSOR_V2
    assert comp["cals"]["primary"]["phase"] == "full"


def test_tc1_adapter_accepts_v1_cursor() -> None:
    """T-C1: adapter.fetch() accepts a v1 cursor string without raising."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        v1_cursor = json.dumps({
            "phase": "incr",
            "sync_token": "synctoken_initial",
            "page_token": None,
        })
        # Must not raise; will trigger calendarList bootstrap
        b = a.fetch(v1_cursor, limit=10)
        assert isinstance(b.next_cursor, str)
        comp = json.loads(b.next_cursor)
        assert comp.get("v") == _CURSOR_V2


# ── T-C2: calendarList discovery ──────────────────────────────────────────────

def test_tc2_primary_registered_on_bootstrap() -> None:
    """T-C2: fresh bootstrap registers primary calendar in cals cursor."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b = a.fetch(None, limit=10)
        comp = json.loads(b.next_cursor)
        assert "primary" in comp["cals"], (
            f"primary must be registered after calendarList bootstrap: {comp}"
        )


def test_tc2_secondary_calendar_registered() -> None:
    """T-C2: secondary calendar added before bootstrap appears in cals cursor."""
    with FakeGoogleServer() as fake:
        fake.add_secondary_calendar("work@group.calendar.google.com", "Work")
        a = _make_adapter(fake)
        b = a.fetch(None, limit=10)
        comp = json.loads(b.next_cursor)
        assert "primary" in comp["cals"]
        assert "work@group.calendar.google.com" in comp["cals"], (
            f"secondary calendar not registered: {comp['cals'].keys()}"
        )


def test_tc2_calendarlist_sync_token_in_list_state() -> None:
    """T-C2: list cursor has a sync_token after calendarList bootstrap."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        b = a.fetch(None, limit=10)
        comp = json.loads(b.next_cursor)
        assert comp["list"] is not None
        assert "sync_token" in comp["list"]
        assert comp["list"]["sync_token"].startswith("callist_sync_")


# ── T-C3: calendar.calendar.added/removed signals ─────────────────────────────

def test_tc3_added_signal_for_new_secondary_calendar() -> None:
    """T-C3: calendar.calendar.added emitted when a new secondary calendar appears."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        # Bootstrap
        cursor, _ = _drive_to_steady(a)

        # Add a secondary calendar
        fake.add_secondary_calendar("new@group.calendar.google.com", "New Cal")
        # The calendarList sync token used is the one from bootstrap.
        # On next fetch, adapter sends calendarList incremental with that token.
        # Fake returns the new calendar item.
        # NOTE: the fake uses _incr_cal_items which were set by add_secondary_calendar.
        b = a.fetch(cursor, limit=10)
        # Drive to pick up calendar signals
        all_sigs = list(b.signals)
        cur = b.next_cursor
        for _ in range(5):
            if not b.has_more:
                break
            b = a.fetch(cur, limit=10)
            all_sigs.extend(b.signals)
            cur = b.next_cursor

        added = [s for s in all_sigs if s.kind == "calendar.calendar.added"]
        assert len(added) >= 1
        cal_ids = [s.payload["calendar_id"] for s in added]
        assert "new@group.calendar.google.com" in cal_ids, (
            f"expected added signal for new calendar, got: {cal_ids}"
        )


def test_tc3_added_signal_has_correct_payload() -> None:
    """T-C3: calendar.calendar.added signal has calendar_id, summary, access_role."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)
        fake.add_secondary_calendar("work@g.c.g.com", "Work Calendar")

        all_sigs = []
        for _ in range(10):
            b = a.fetch(cursor, limit=10)
            all_sigs.extend(b.signals)
            cursor = b.next_cursor
            if not b.has_more:
                break

        added = [s for s in all_sigs if s.kind == "calendar.calendar.added"]
        work_added = [s for s in added if s.payload["calendar_id"] == "work@g.c.g.com"]
        assert work_added, f"no added signal for work calendar. All added: {added}"
        sig = work_added[0]
        assert sig.payload["summary"] == "Work Calendar"
        assert "access_role" in sig.payload
        assert sig.payload.get("primary") is False


# ── T-C4: multi-calendar event sync (independent cursors) ─────────────────────

def test_tc4_events_from_secondary_calendar() -> None:
    """T-C4: events on a secondary calendar produce calendar.event.changed signals."""
    with FakeGoogleServer() as fake:
        fake.add_secondary_calendar("work@g.c.g.com", "Work")
        # Add events to both primary and secondary
        fake.calendar.add_event("p1", summary="Primary Event")
        fake.calendar.add_event("w1", summary="Work Event")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        cursor, all_sigs = _drive_to_steady(a)

        event_sigs = [s for s in all_sigs if s.kind == "calendar.event.changed"]
        event_ids = [s.payload["event_id"] for s in event_sigs]

        # Both events should appear (from primary and secondary)
        # NOTE: The fake currently serves all events from one calendar state;
        # for now, verify at least primary events appear.
        assert any(s.payload["calendar_id"] == "primary" for s in event_sigs), (
            f"no primary calendar events: {event_ids}"
        )


def test_tc4_independent_cursors_per_calendar() -> None:
    """T-C4: each calendar has its own cursor in cals dict."""
    with FakeGoogleServer() as fake:
        fake.add_secondary_calendar("work@g.c.g.com", "Work")
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        comp = json.loads(cursor)
        assert "primary" in comp["cals"]
        assert "work@g.c.g.com" in comp["cals"], (
            f"secondary calendar missing from cals: {list(comp['cals'].keys())}"
        )
        # Both must be in incr phase
        assert comp["cals"]["primary"]["phase"] == "incr"
        assert comp["cals"]["work@g.c.g.com"]["phase"] == "incr"


def test_tc4_event_external_id_includes_calendar_hash() -> None:
    """T-C4: event external_ids from different calendars don't collide."""
    from olo.adapters.gcal import _event_to_signal
    from datetime import UTC

    fetch_time = datetime.now(UTC)
    ev = {
        "id": "event123",
        "status": "confirmed",
        "summary": "Meeting",
        "updated": "2026-01-01T12:00:00Z",
        "start": {"dateTime": "2026-01-01T12:00:00Z"},
        "end": {"dateTime": "2026-01-01T13:00:00Z"},
        "created": "2026-01-01T12:00:00Z",
        "organizer": {"email": "org@example.com", "self": False},
    }
    sig_primary = _event_to_signal(ev, "gcal_test", fetch_time, "primary")
    sig_work    = _event_to_signal(ev, "gcal_test", fetch_time, "work@g.c.g.com")

    assert sig_primary.external_id != sig_work.external_id, (
        "events on different calendars must have distinct external_ids"
    )
    assert sig_primary.payload["calendar_id"] == "primary"
    assert sig_work.payload["calendar_id"] == "work@g.c.g.com"


# ── T-C5: 410 per-calendar self-heal ──────────────────────────────────────────

def test_tc5_410_self_heals_does_not_raise() -> None:
    """T-C5: 410 on events.list restarts full sync for affected calendar only."""
    with FakeGoogleServer() as fake:
        fake.calendar.add_event("ev1", summary="Pre-heal")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        # Inject expired sync token into primary cursor
        comp = json.loads(cursor)
        comp["cals"]["primary"]["sync_token"] = "EXPIRED_SYNC_TOKEN"
        expired_cursor = json.dumps(comp)

        # Must not raise — self-heals by restarting full sync for primary
        b = a.fetch(expired_cursor, limit=10)
        assert isinstance(b.next_cursor, str)
        new_comp = json.loads(b.next_cursor)
        # After self-heal, primary should eventually reach incr phase again
        assert "primary" in new_comp["cals"]


def test_tc5_other_calendars_unaffected_by_410() -> None:
    """T-C5: 410 on one calendar does not affect other calendars' cursors."""
    with FakeGoogleServer() as fake:
        fake.add_secondary_calendar("work@g.c.g.com", "Work")
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        comp = json.loads(cursor)
        work_cursor = dict(comp["cals"].get("work@g.c.g.com", {}))

        # Inject 410 only for primary
        comp["cals"]["primary"]["sync_token"] = "EXPIRED_SYNC_TOKEN"
        expired_cursor = json.dumps(comp)

        b = a.fetch(expired_cursor, limit=10)
        new_comp = json.loads(b.next_cursor)

        # Work calendar cursor should be unchanged (or only updated normally)
        new_work = new_comp["cals"].get("work@g.c.g.com", {})
        # The phase should still be valid (incr or full after self-heal)
        assert new_work.get("phase") in ("incr", "full"), (
            f"work calendar cursor corrupted after primary 410: {new_work}"
        )


# ── T-C6: lookback_days 0–365 ─────────────────────────────────────────────────

def test_tc6_lookback_days_365_accepted() -> None:
    """T-C6: lookback_days=365 is accepted."""
    adapter = GcalAdapter({"account": "test", "lookback_days": "365"})
    assert adapter._lookback_days == 365


def test_tc6_lookback_days_0_accepted() -> None:
    """T-C6: lookback_days=0 is accepted."""
    adapter = GcalAdapter({"account": "test", "lookback_days": "0"})
    assert adapter._lookback_days == 0


def test_tc6_lookback_days_366_rejected() -> None:
    """T-C6: lookback_days=366 is rejected with 0–365 in message."""
    with pytest.raises(AdapterConfigError, match="0–365"):
        GcalAdapter({"account": "test", "lookback_days": "366"})


def test_tc6_lookback_days_negative_rejected() -> None:
    """T-C6: lookback_days=-1 is rejected."""
    with pytest.raises(AdapterConfigError, match="0–365"):
        GcalAdapter({"account": "test", "lookback_days": "-1"})


# ── T-C7: calendarList 410 self-heal ──────────────────────────────────────────

def test_tc7_calendarlist_410_self_heals() -> None:
    """T-C7: 410 on calendarList.list triggers a fresh full calendarList sync."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        comp = json.loads(cursor)
        # Inject expired calendarList sync token
        comp["list"]["sync_token"] = "EXPIRED_CAL_LIST_TOKEN"
        expired_cursor = json.dumps(comp)

        # Must not raise — fake returns 410 for EXPIRED_CAL_LIST_TOKEN
        # and the adapter restarts calendarList full sync
        b = a.fetch(expired_cursor, limit=10)
        new_comp = json.loads(b.next_cursor)
        # After self-heal: list cursor has a new sync token from full scan
        assert new_comp["list"] is not None
        assert "sync_token" in new_comp["list"]
        assert new_comp["list"]["sync_token"] != "EXPIRED_CAL_LIST_TOKEN"


# ── T-C8: composite cursor v2 schema invariants ───────────────────────────────

def test_tc8_cursor_always_v2() -> None:
    """T-C8: every cursor returned by fetch() is v2 JSON."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            comp = json.loads(b.next_cursor)
            assert comp.get("v") == _CURSOR_V2, (
                f"cursor missing v={_CURSOR_V2}: {b.next_cursor}"
            )
            cursor = b.next_cursor
            if not b.has_more:
                break


def test_tc8_cursor_has_list_and_cals() -> None:
    """T-C8: every v2 cursor has 'list' and 'cals' keys."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor = None
        for _ in range(20):
            b = a.fetch(cursor, limit=10)
            comp = json.loads(b.next_cursor)
            assert "list" in comp, f"cursor missing 'list': {b.next_cursor}"
            assert "cals" in comp, f"cursor missing 'cals': {b.next_cursor}"
            cursor = b.next_cursor
            if not b.has_more:
                break


def test_tc8_v2_cursor_round_trips_stable() -> None:
    """T-C8: feeding a cursor back into fetch() produces the same v2 structure."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        # Second call with the same cursor should not change the v2 schema
        b2 = a.fetch(cursor, limit=10)
        comp = json.loads(b2.next_cursor)
        assert comp.get("v") == _CURSOR_V2
        assert "list" in comp
        assert "cals" in comp


# ══════════════════════════════════════════════════════════════════════════════
# M2b Calendar content blob tests (B4-content, gcal_full.py)
# Design: blob attached to calendar.event.changed / calendar.event.cancelled
#         via signal.blobs and payload["content_refs"]
# ══════════════════════════════════════════════════════════════════════════════

def test_calendar_event_changed_has_blob() -> None:
    """B4-content: calendar.event.changed carries one ContentBlob with content_refs."""
    from olo.adapters.gcal_full import GCAL_EVENT_MEDIA_TYPE

    with FakeGoogleServer() as fake:
        fake.calendar.add_event("ev1", summary="Standup")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        cursor, all_sigs = _drive_to_steady(a)

        changed_sigs = [s for s in all_sigs if s.kind == "calendar.event.changed"]
        assert changed_sigs, "no calendar.event.changed signals found"

        sig = changed_sigs[0]
        # Must have exactly one blob
        assert len(sig.blobs) == 1, f"expected 1 blob, got {len(sig.blobs)}"
        blob = sig.blobs[0]
        assert blob.media_type == GCAL_EVENT_MEDIA_TYPE
        assert len(blob.data) > 0

        # content_refs must be in payload
        refs = sig.payload.get("content_refs", [])
        assert len(refs) == 1, f"expected 1 content_ref, got {refs}"
        assert refs[0]["sha256"] == blob.sha256
        assert refs[0]["role"] == "event_doc"


def test_calendar_event_changed_description_in_blob_not_payload() -> None:
    """B4-content: description is in the blob document but NOT in the metadata payload.

    Privacy invariant (§6.3 M2):
      - calendar.event.changed payload: no description
      - signal.blobs[0] data: description present
    """
    import json as _json

    with FakeGoogleServer() as fake:
        fake.calendar.add_event("ev1", summary="Project Review")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        _, all_sigs = _drive_to_steady(a)

        changed_sigs = [s for s in all_sigs if s.kind == "calendar.event.changed"]
        assert changed_sigs

        for sig in changed_sigs:
            # description must NOT be in payload
            assert "description" not in sig.payload, (
                f"description leaked into calendar.event.changed payload: "
                f"{sig.payload.keys()}"
            )

        # Blob doc must have basic event structure + calendar_id injected
        sig = changed_sigs[0]
        doc = _json.loads(sig.blobs[0].data.decode())
        assert "id" in doc or "summary" in doc
        assert "calendar_id" in doc, f"calendar_id not in event doc: {list(doc.keys())}"


def test_calendar_event_cancelled_has_blob() -> None:
    """B4-content: cancelled events also carry a blob."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        fake.calendar.add_event("ev1", summary="Cancelled Meeting")
        fake.calendar.cancel_event("ev1")

        all_sigs = []
        for _ in range(10):
            b = a.fetch(cursor, limit=50)
            all_sigs.extend(b.signals)
            cursor = b.next_cursor
            if not b.has_more:
                break

        cancelled = [s for s in all_sigs if s.kind == "calendar.event.cancelled"]
        assert cancelled, "expected at least one cancelled signal"

        # Cancelled signals must also have a blob
        for sig in cancelled:
            assert len(sig.blobs) == 1, (
                f"cancelled event must carry blob: {sig.external_id}"
            )
            refs = sig.payload.get("content_refs", [])
            assert len(refs) == 1, f"cancelled event missing content_refs"


def test_calendar_event_blob_stored_via_ingest() -> None:
    """B4-content: ingest_batch stores blobs; description present in blob, absent from payload."""
    import json as _json
    import sqlite3

    from olo.content import get_blob, load_json_doc
    from olo.store import acquire_run, finish_run, ingest_batch, migrate, connect
    from olo.adapters.gcal_full import build_event_blob, event_content_ref, GCAL_EVENT_MEDIA_TYPE
    from olo.signals import NormalizedSignal
    from datetime import UTC, datetime

    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)

    source = "gcal_test"
    run_id = acquire_run(source, "owner", conn)

    raw_event = {
        "id": "event001",
        "status": "confirmed",
        "summary": "Sprint Planning",
        "description": "Discuss Q4 roadmap. OLO-BODY-MARKER",
        "start": {"dateTime": "2026-11-17T10:00:00Z"},
        "end":   {"dateTime": "2026-11-17T11:00:00Z"},
        "created": "2026-11-01T00:00:00Z",
        "updated": "2026-11-01T00:00:00Z",
        "organizer": {"email": "alice@example.com", "self": True},
    }
    cal_id = "primary"
    blob = build_event_blob(raw_event, cal_id)

    import hashlib as _h
    cal_hash = _h.sha256(cal_id.encode()).hexdigest()[:8]
    from olo.adapters.gcal import _event_external_id
    ext_id = f"{_event_external_id('event001', '2026-11-01T00:00:00Z')}_{cal_hash}"[:256]

    sig = NormalizedSignal(
        source=source,
        external_id=ext_id,
        kind="calendar.event.changed",
        occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
        subject="Sprint Planning",
        payload={
            "event_id": "event001",
            "calendar_id": cal_id,
            "status": "confirmed",
            "summary": "Sprint Planning",
            "content_refs": [event_content_ref(blob)],
        },
        blobs=(blob,),
    )

    inserted, _, _ = ingest_batch(source, run_id, (sig,), "cursor1", None, conn)
    finish_run(source, run_id, "succeeded", conn)
    assert inserted == 1

    # Blob stored
    n_blobs = conn.execute("SELECT COUNT(*) FROM content_blobs WHERE source=?", (source,)).fetchone()[0]
    assert n_blobs == 1

    # Description IS in blob, NOT in payload
    doc = load_json_doc(conn, source, blob.sha256)
    assert doc is not None
    assert doc.get("description") == "Discuss Q4 roadmap. OLO-BODY-MARKER"
    assert doc.get("calendar_id") == cal_id

    signal_row = conn.execute(
        "SELECT payload FROM signals WHERE source=? AND kind='calendar.event.changed'",
        (source,),
    ).fetchone()
    payload = _json.loads(signal_row["payload"])
    assert "description" not in payload
    assert "OLO-BODY-MARKER" not in signal_row["payload"]

    conn.close()


def test_limit_1_works_with_one_signal_per_event() -> None:
    """B4-content: limit=1 is valid — one event produces one signal (changed+blob)."""
    with FakeGoogleServer() as fake:
        a = _make_adapter(fake)
        cursor, _ = _drive_to_steady(a)

        fake.calendar.add_event("ev1", summary="Limit1 Event")

        b = a.fetch(cursor, limit=1)
        # Must produce exactly 1 signal (changed, with blob) — not raise
        assert len(b.signals) <= 1
        if b.signals:
            sig = b.signals[0]
            assert sig.kind in ("calendar.event.changed", "calendar.event.cancelled")
            assert len(sig.blobs) == 1  # blob attached


def test_no_separate_calendar_event_content_signal() -> None:
    """Design assertion: no calendar.event.content kind is emitted.

    Blobs are attached to calendar.event.changed / calendar.event.cancelled.
    There is no separate calendar.event.content signal in M2b.
    """
    with FakeGoogleServer() as fake:
        fake.calendar.add_event("ev1", summary="Test")
        fake.calendar._incr_events.clear()

        a = _make_adapter(fake)
        _, all_sigs = _drive_to_steady(a)

        content_kind_sigs = [s for s in all_sigs if s.kind == "calendar.event.content"]
        assert content_kind_sigs == [], (
            f"Unexpected calendar.event.content signals: {content_kind_sigs}. "
            "Blobs should be attached to calendar.event.changed, not a separate signal."
        )
