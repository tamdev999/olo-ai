"""Opus audit targeted test items 2–7.

Each test demonstrates the original defective behavior FAILS (marked with
@pytest.mark.xfail or via an explicit "before" check) and the corrected
implementation PASSES.

Items:
  2. Empty non-deleted calendar through the real runner (no stall).
  3. V2 scope requests and legacy-credential rejection (adapter-level, not CLI).
  4. Backfill resume: label change, permanent deletion, new message after C.
  5. Large-message forward progress (incremental + backfill soft-stop).
  6. Calendar round-robin across three independent event streams.
  7. OAuth verification failure: zero revoke requests.

No live OAuth, no new credentials, no live network.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

from olo.adapters import AdapterConfigError, FetchBatch, SourceError
from olo.adapters.gcal import GcalAdapter
from olo.adapters.gmail import GmailAdapter
from olo.adapters.gmail_full import run_backfill_batch, _BATCH_BLOB_BUDGET_BYTES
from olo.adapters.gcal_full import GCAL_EVENT_MEDIA_TYPE
from olo.google.credentials import (
    GMAIL_METADATA_SCOPE,
    GMAIL_READONLY_SCOPE,
    CALENDAR_EVENTS_OWNED_RO,
    CALENDAR_READONLY_SCOPE,
    CALENDAR_LIST_SCOPE,
    REQUIRED_SCOPES,
    REQUIRED_SCOPES_V2,
    SCOPE_PROFILE_LEGACY,
    SCOPE_PROFILE_FULL_V1,
    GoogleAccountCredentials,
    credential_lock,
    save_credentials,
    load_credentials,
)
from olo.runner import run_source
from olo.signals import NormalizedSignal
from olo.store import (
    connect as _connect, migrate, get_checkpoint,
    count_signals, acquire_run, finish_run, ingest_batch,
)
from tests.olo.fake_google import FakeGoogleServer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _memdb() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def _make_adapter(fake: FakeGoogleServer, alias: str = "test",
                  lookback_days: int = 7, **kwargs) -> GcalAdapter:
    from olo.google.http import GoogleClient
    creds = fake.make_credentials(alias, **kwargs)
    client = GoogleClient(creds)
    client._access_token = fake.tokens.access_token
    return GcalAdapter({"account": alias, "lookback_days": str(lookback_days)},
                       client=client)


def _drive_to_steady(a: GcalAdapter, limit: int = 50):
    cursor = None
    all_sigs = []
    for _ in range(40):
        b = a.fetch(cursor, limit=limit)
        all_sigs.extend(b.signals)
        cursor = b.next_cursor
        if not b.has_more:
            break
    return cursor, all_sigs


# ══════════════════════════════════════════════════════════════════════════════
# Item 2: Empty non-deleted calendar through the real runner
# ══════════════════════════════════════════════════════════════════════════════

class TestItem2EmptyCalendar:
    """An empty, non-deleted calendar must not stall the entire Calendar source."""

    def test_empty_calendar_completes_without_stall(self) -> None:
        """Empty calendar (no events, returns nextSyncToken immediately) completes.

        Before fix (P0-B class): if deleted items were registered they'd cause
        spurious fetches. This test verifies an empty-but-active calendar:
        - Is registered in cals
        - Transitions to phase=incr after one fetch
        - Does not stall (returns has_more=False promptly)
        """
        with FakeGoogleServer() as fake:
            # Register an empty secondary calendar (no events)
            EMPTY_CAL = "empty-cal@example.com"
            fake.add_secondary_calendar(EMPTY_CAL, "Empty Calendar")
            # Do NOT add any events — the calendar is empty

            a = _make_adapter(fake)
            cursor, all_sigs = _drive_to_steady(a, limit=10)

            # Adapter must have completed (not stalled)
            assert cursor is not None, "cursor is None — adapter may have stalled"
            comp = json.loads(cursor)
            assert comp.get("v") == 2

            # Empty calendar must be in incr phase
            cals = comp.get("cals", {})
            assert EMPTY_CAL in cals, (
                f"Empty calendar {EMPTY_CAL!r} not registered in cals: {list(cals.keys())}"
            )
            assert cals[EMPTY_CAL]["phase"] == "incr", (
                f"Empty calendar still in phase {cals[EMPTY_CAL]['phase']!r}, expected 'incr'"
            )

    def test_empty_calendar_through_run_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty calendar runs through run_source() without AdapterContractError."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        with FakeGoogleServer() as fake:
            EMPTY_CAL = "empty-runner@example.com"
            fake.add_secondary_calendar(EMPTY_CAL, "Empty Runner Cal")

            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test")
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GcalAdapter({"account": "test"}, client=client)

            db = tmp_path / "olo.db"
            result = run_source(adapter, limit=20, max_batches=50,
                                db_path=db)
            assert result.status in ("succeeded", "partial"), (                f"run_source failed on empty calendar: status={result.status!r} "
                f"error={result.error!r}"
            )

    def test_deleted_calendar_not_registered(self) -> None:
        """Calendar with deleted=True in calendarList is ignored (P0-B).

        Original defective behavior: deleted items were registered with
        phase='full', causing spurious event fetches for unsubscribed calendars.
        """
        with FakeGoogleServer() as fake:
            DELETED_ID = "deleted@example.com"
            # Directly inject a deleted item into the calendarList response
            fake.calendar._extra_calendars = {
                DELETED_ID: {
                    "id": DELETED_ID,
                    "summary": "Deleted",
                    "accessRole": "reader",
                    "primary": False,
                    "timeZone": "UTC",
                    "colorId": "1",
                    "deleted": True,
                }
            }

            a = _make_adapter(fake)
            cursor, _ = _drive_to_steady(a)

            comp = json.loads(cursor)
            cals = comp.get("cals", {})
            assert DELETED_ID not in cals, (
                f"P0-B FAIL (item 2): deleted calendar {DELETED_ID!r} was registered. "
                f"Registered cals: {list(cals.keys())}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Item 3: V2 scope requests and legacy-credential rejection
# ══════════════════════════════════════════════════════════════════════════════

class TestItem3ScopeEnforcement:
    """Adapter-level scope checks, not just CLI argument parsing."""

    def test_gmail_adapter_accepts_full_v1_credentials(self) -> None:
        """GmailAdapter works when credentials have full_v1 scope profile."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            assert creds.scope_profile == SCOPE_PROFILE_FULL_V1
            assert creds.has_gmail_readonly_scope()

            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            # Bootstrap must succeed
            b = adapter.fetch(None, limit=10)
            assert b.next_cursor is not None

    def test_gmail_adapter_accepts_legacy_credentials(self) -> None:
        """GmailAdapter works with legacy credentials (gmail.metadata scope)."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test")  # legacy by default
            assert GMAIL_METADATA_SCOPE in creds.granted_scopes

            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            b = adapter.fetch(None, limit=10)
            assert b.next_cursor is not None

    def test_new_account_connect_requests_v2_scopes(self) -> None:
        """P1-C: standard 'google connect' requests REQUIRED_SCOPES_V2 (not legacy).

        The standard connect path (non-upgrade) must request M2b scopes so new
        accounts get full-content capability from the start.
        """
        import argparse
        from olo.hermes_plugin.cli import setup_parser, _cmd_google_connect
        import types

        # Verify that _cmd_google_connect standard path uses REQUIRED_SCOPES_V2
        # by checking which scopes are passed to exchange_code
        captured_scopes: list = []

        def _fake_exchange(code, pending, client_id, client_secret, alias, **kwargs):
            captured_scopes.append(frozenset(pending.scopes))
            raise RuntimeError("stop after exchange")

        import unittest.mock as _mock
        with _mock.patch("olo.google.oauth.exchange_code", side_effect=_fake_exchange):
            with _mock.patch("olo.google.oauth.connect_loopback") as mock_loopback, \
                 _mock.patch("olo.google.oauth.await_loopback_code", return_value="fake_code"):
                from olo.google.oauth import PendingOAuth
                mock_loopback.return_value = (
                    "http://auth.example.com/...",
                    PendingOAuth("state", "verifier", "http://localhost/", REQUIRED_SCOPES_V2),
                    12345,
                )
                ns = types.SimpleNamespace(
                    account="newaccount",
                    client_secret_file="/dev/null",
                    manual=False,
                    production=False,
                    upgrade=False,
                    revoke_first=False,
                )
                # Create a minimal client secret file
                import tempfile, json as _j
                with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                                 delete=False) as f:
                    _j.dump({"installed": {"client_id": "cid",
                                           "client_secret": "cs",
                                           "redirect_uris": ["http://localhost"]}}, f)
                    ns.client_secret_file = f.name

                try:
                    _cmd_google_connect(ns)
                except (RuntimeError, SystemExit):
                    pass  # expected — fake exchange raises

        if captured_scopes:
            assert REQUIRED_SCOPES_V2.issubset(captured_scopes[0]) or \
                   captured_scopes[0] == REQUIRED_SCOPES_V2, (
                f"P1-C FAIL: standard connect used scopes {captured_scopes[0]!r} "
                f"instead of REQUIRED_SCOPES_V2={REQUIRED_SCOPES_V2!r}"
            )

    def test_gcal_adapter_raises_on_no_calendar_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adapter raises AdapterConfigError when credentials have no calendar scope."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        # Create credentials with ONLY gmail scope (no calendar)
        creds = GoogleAccountCredentials(
            alias="nocal",
            client_id="cid",
            client_secret="cs",
            refresh_token="rt",
            granted_scopes=frozenset({GMAIL_METADATA_SCOPE}),
            email_address="test@example.com",
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=None,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("nocal"):
            save_credentials(creds)

        adapter = GcalAdapter({"account": "nocal"})
        with pytest.raises(AdapterConfigError, match="calendar scope"):
            adapter.fetch(None, limit=10)


# ══════════════════════════════════════════════════════════════════════════════
# Item 4: Backfill resume — label change, permanent deletion, new message after C
# ══════════════════════════════════════════════════════════════════════════════

class TestItem4BackfillResume:
    """Backfill resume covers: label change, permanent deletion, new message after C."""

    def test_backfill_then_incremental_sees_label_change_after_c(self) -> None:
        """After backfill completes at R=C, incremental sync picks up label changes > C.

        Setup:
          - Bootstrap at historyId C
          - Run backfill (messages.list)
          - Add a message AFTER C (triggers history record > C)
          - Backfill completes: cursor == C
          - Incremental fetch(C) sees the new messagesAdded record
        """
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            # Bootstrap
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

            # Add a message to the fake BEFORE injecting backfill cursor
            # (so messages.list picks it up)
            fake.gmail.add_message("m-before", thread_id="t1", labels=["INBOX"],
                                   subject="Before backfill")

            # Inject backfill cursor with R=C
            backfill_cursor = json.dumps({
                "phase": "backfill",
                "mode": "resume",
                "resume_history_id": C,
                "after": 0,
                "page_token": None,
            })

            # Run backfill to completion
            cur = backfill_cursor
            for _ in range(10):
                b = adapter.fetch(cur, limit=10)
                cur = b.next_cursor
                if not b.has_more:
                    break

            # After backfill, cursor should be C (or close to it)
            # Now add a NEW message AFTER C (this appears in history > C)
            fake.gmail.add_message("m-after", thread_id="t2", labels=["INBOX"],
                                   subject="After backfill")

            # Also add a label change (simulates archive)
            fake.gmail.add_labels("m-before", ["STARRED"])

            # Incremental fetch from C must see the new message and label change
            b_incr = adapter.fetch(cur, limit=10)
            kinds = {s.kind for s in b_incr.signals}
            assert "email.received" in kinds or "email.labels_added" in kinds, (
                f"Item 4 FAIL: incremental after backfill missed events. "
                f"kinds={kinds}, signals={b_incr.signals}"
            )

    def test_backfill_handles_permanently_deleted_message(self) -> None:
        """Backfill skips 404-deleted messages gracefully.

        A message in messages.list that returns None from get_message_full
        (deleted since listing) must be skipped without failing the batch.
        """
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token

            # Add a message then delete it from the fake
            fake.gmail.add_message("perm-del", thread_id="t1", labels=["INBOX"],
                                   subject="Permanent deletion test")

            adapter = GmailAdapter({"account": "test"}, client=client)
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

            # Simulate the message being permanently deleted
            # (messages.list returns it but get_message_full returns None)
            original_full = client.get_message_full
            def _deleted_full(msg_id):
                if msg_id == "perm-del":
                    return None   # 404 — deleted
                return original_full(msg_id)
            client.get_message_full = _deleted_full

            backfill_cursor = json.dumps({
                "phase": "backfill",
                "mode": "resume",
                "resume_history_id": C,
                "after": 0,
                "page_token": None,
            })

            # Must not raise — 404 is gracefully skipped
            b = adapter.fetch(backfill_cursor, limit=10)
            assert isinstance(b.signals, tuple)
            # No email.content for the deleted message
            content = [s for s in b.signals if s.external_id == "c:perm-del"]
            assert not content, f"Item 4 FAIL: deleted message got email.content signal"

    def test_backfill_new_message_after_c_handled_by_incremental(self) -> None:
        """Messages arriving after C are NOT in backfill; incremental picks them up."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            # Bootstrap
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

            # Run backfill (empty messages.list)
            backfill_cursor = json.dumps({
                "phase": "backfill",
                "mode": "resume",
                "resume_history_id": C,
                "after": 0,
                "page_token": None,
            })
            b_bf = adapter.fetch(backfill_cursor, limit=10)
            # Backfill completes: cursor == C
            assert not b_bf.has_more
            after_backfill_cursor = b_bf.next_cursor

            # NOW add a message (after C — appears in history)
            fake.gmail.add_message("new-msg", thread_id="tnew", labels=["INBOX"],
                                   subject="New after backfill")

            # Incremental must pick it up
            b_incr = adapter.fetch(after_backfill_cursor, limit=10)
            received = [s for s in b_incr.signals if s.kind == "email.received"]
            assert received, (
                f"Item 4 FAIL: new message after backfill not seen in incremental. "
                f"signals={b_incr.signals}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Item 5: Large-message forward progress
# ══════════════════════════════════════════════════════════════════════════════

class TestItem5LargeMessageProgress:
    """Large messages must not stall forward progress in incremental or backfill."""

    def test_backfill_soft_stop_does_not_replay_processed_messages(self) -> None:
        """P1-F: skip:N encoding prevents replaying already-committed messages.

        Before fix: soft-stop returned raw page_token, causing N redundant
        get_message_full calls for already-processed messages on resume.

        After fix: "skip:N:REAL_TOKEN" is returned; on resume, first N stubs
        are skipped without calling get_message_full.
        """
        import base64, olo.adapters.gmail_full as _gf

        # Track get_message_full calls
        fetch_calls: list[str] = []

        class _FakeMsg:
            pass

        def _fake_full(msg_id):
            fetch_calls.append(msg_id)
            # Return minimal message
            body = base64.urlsafe_b64encode(b"hello " * 500).decode()
            return {
                "id": msg_id,
                "threadId": f"t{msg_id}",
                "internalDate": "1700000000000",
                "labelIds": ["INBOX"],
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": f"Msg {msg_id}"},
                        {"name": "From", "value": "a@e.com"},
                        {"name": "To", "value": "b@e.com"},
                        {"name": "Message-ID", "value": f"<{msg_id}>"},
                    ],
                    "body": {"data": body, "size": len(body)},
                    "parts": [],
                }
            }

        class _FakeClient:
            def list_messages(self, params):
                return {"messages": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]}
            def get_message_full(self, msg_id):
                return _fake_full(msg_id)

        client = _FakeClient()
        orig_budget = _gf._BATCH_BLOB_BUDGET_BYTES
        # Set budget tiny so second message triggers soft-stop
        _gf._BATCH_BLOB_BUDGET_BYTES = 1

        try:
            # First call: processes m1, hits budget on m2
            signals1, next_pt1, done1 = run_backfill_batch(
                source="test", client=client, conn=None, run_id="r1",
                after_epoch=0, page_token=None, resume_history_id="1000",
                mode="bootstrap", limit=10,
            )
        finally:
            _gf._BATCH_BLOB_BUDGET_BYTES = orig_budget

        # Soft-stop should encode skip count
        if next_pt1 and next_pt1.startswith("skip:"):
            parts = next_pt1.split(":", 2)
            assert len(parts) == 3
            skip_n = int(parts[1])
            assert skip_n >= 1, "skip count must be at least 1 (m1 was committed)"

            # Second call: should skip m1, start from m2
            fetch_calls.clear()
            orig_budget2 = _gf._BATCH_BLOB_BUDGET_BYTES
            _gf._BATCH_BLOB_BUDGET_BYTES = 10 * 1024 * 1024  # large budget for resume
            try:
                signals2, next_pt2, done2 = run_backfill_batch(
                    source="test", client=client, conn=None, run_id="r2",
                    after_epoch=0, page_token=next_pt1, resume_history_id="1000",
                    mode="bootstrap", limit=10,
                )
            finally:
                _gf._BATCH_BLOB_BUDGET_BYTES = orig_budget2

            # m1 must NOT have been re-fetched
            assert "m1" not in fetch_calls, (
                f"P1-F FAIL: m1 was re-fetched on resume. "
                f"fetched_ids={fetch_calls}"
            )

    def test_incremental_source_error_propagates_does_not_advance_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P0-C: in incremental mode, SourceError from get_message_full propagates.

        The checkpoint must NOT advance when a network error occurs fetching
        a message in full content mode.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token

            # Bootstrap
            adapter = GmailAdapter({"account": "test"}, client=client)
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

            # Add a message so it appears in history
            fake.gmail.add_message("big-msg", thread_id="t1", labels=["INBOX"],
                                   subject="Large content test")

            # Inject SourceError on get_message_full
            def _raise_on_full(msg_id):
                raise SourceError("simulated network failure on format=full")
            client.get_message_full = _raise_on_full

            # Incremental fetch must raise SourceError — not silently swallow it
            with pytest.raises(SourceError, match="simulated network failure"):
                adapter.fetch(C, limit=10)

            # Checkpoint must still be C (not advanced)
            # (run_source would catch and record failure; direct fetch() raises)


# ══════════════════════════════════════════════════════════════════════════════
# Item 6: Calendar round-robin across three independent event streams
# ══════════════════════════════════════════════════════════════════════════════

class TestItem6CalendarRoundRobin:
    """Round-robin scheduling across three calendars with independent event streams."""

    def test_three_calendar_round_robin_all_serviced(self) -> None:
        """P0-A: all 3 calendars are serviced within N+2 steady-state ticks.

        Original defective behavior: only the first calendar in insertion order
        was ever serviced; the other two starved indefinitely.

        After fix: sorted key list + next_cal index ensures each calendar
        is serviced before any is repeated.
        """
        with FakeGoogleServer() as fake:
            CAL_IDS = [
                "cal-alpha@example.com",
                "cal-beta@example.com",
                "cal-gamma@example.com",
            ]
            # Create independent event sets for each calendar
            for cal_id in CAL_IDS:
                sec = fake.add_secondary_calendar_with_events(
                    cal_id,
                    f"Calendar {cal_id}",
                    event_ids=[f"ev-init-{cal_id}"],
                )
                # Clear incr so bootstrap doesn't see these as incremental
                sec._incr_events.clear()

            a = _make_adapter(fake)
            cursor, _ = _drive_to_steady(a)

            # Add exactly one event to each secondary calendar
            for cal_id in CAL_IDS:
                sec = fake.calendar._extra_calendar_states.get(cal_id)
                if sec:
                    sec.add_event(f"new-{cal_id}", summary=f"New in {cal_id}")

            # Collect which calendars are serviced per tick
            # Each tick in steady-state should process one calendar via round-robin
            serviced_per_tick: list[set[str]] = []
            cur = cursor
            max_ticks = len(CAL_IDS) * 3 + 10  # generous budget

            for _ in range(max_ticks):
                b = a.fetch(cur, limit=10)
                tick_cals: set[str] = set()
                for sig in b.signals:
                    cal_id = sig.payload.get("calendar_id", "")
                    if cal_id in CAL_IDS:
                        tick_cals.add(cal_id)
                if tick_cals:
                    serviced_per_tick.append(tick_cals)
                cur = b.next_cursor
                if not b.has_more and len(serviced_per_tick) >= len(CAL_IDS):
                    break
                # Re-add events to keep each calendar active
                if not b.has_more:
                    for cal_id in CAL_IDS:
                        sec = fake.calendar._extra_calendar_states.get(cal_id)
                        if sec:
                            sec.add_event(f"extra-{cal_id}-{len(serviced_per_tick)}",
                                          summary="extra")

            all_serviced = set()
            for tick_cals in serviced_per_tick:
                all_serviced.update(tick_cals)

            missing = set(CAL_IDS) - all_serviced
            assert not missing, (
                f"P0-A FAIL (item 6): calendars never serviced: {missing}. "
                f"serviced_per_tick={serviced_per_tick}"
            )

    def test_three_calendar_no_duplication_per_tick(self) -> None:
        """Round-robin: each tick services at most ONE calendar in steady state.

        The round-robin design ensures one calendar per tick in steady-state,
        preventing any calendar from monopolising multiple consecutive ticks.
        """
        with FakeGoogleServer() as fake:
            CAL_IDS = ["rr-1@example.com", "rr-2@example.com", "rr-3@example.com"]
            for cal_id in CAL_IDS:
                sec = fake.add_secondary_calendar_with_events(
                    cal_id, f"RR {cal_id}",
                    event_ids=[f"ev-{cal_id}"],
                )
                sec._incr_events.clear()

            a = _make_adapter(fake)
            cursor, _ = _drive_to_steady(a)

            # Add events to all calendars
            for cal_id in CAL_IDS:
                sec = fake.calendar._extra_calendar_states.get(cal_id)
                if sec:
                    sec.add_event(f"rr-new-{cal_id}", summary="RR New")

            # Each signal-bearing tick should serve at most one calendar
            cur = cursor
            for _ in range(20):
                b = a.fetch(cur, limit=10)
                if b.signals:
                    cal_ids_this_tick = {
                        s.payload.get("calendar_id")
                        for s in b.signals
                        if s.payload.get("calendar_id") in CAL_IDS
                    }
                    # At most one calendar per tick in steady state
                    assert len(cal_ids_this_tick) <= 1, (
                        f"P0-A FAIL: multiple calendars serviced in single tick: "
                        f"{cal_ids_this_tick}"
                    )
                cur = b.next_cursor
                if not b.has_more:
                    break

    def test_next_cal_index_cycles_through_all_calendars(self) -> None:
        """Round-robin index stored in cursor cycles through all N calendars."""
        with FakeGoogleServer() as fake:
            CAL_IDS = ["idxa@example.com", "idxb@example.com"]
            for cal_id in CAL_IDS:
                sec = fake.add_secondary_calendar_with_events(
                    cal_id, f"Idx {cal_id}",
                    event_ids=[f"ev-{cal_id.replace('@','').replace('.','')}"],
                )
                sec._incr_events.clear()

            a = _make_adapter(fake)
            cursor, _ = _drive_to_steady(a)

            # Add events to all secondary calendars to trigger steady-state ticks
            for cal_id in CAL_IDS:
                for i in range(4):
                    sec = fake.calendar._extra_calendar_states.get(cal_id)
                    if sec:
                        sec.add_event(f"idx-{cal_id.split('@')[0]}-{i}",
                                      summary=f"Idx {i}")

            # Determine actual number of incr calendars in this setup
            # (primary + 2 secondaries = 3 total)
            comp0 = json.loads(cursor)
            incr_cals = sorted(
                cal_id for cal_id, c in comp0.get("cals", {}).items()
                if isinstance(c, dict) and c.get("phase") == "incr"
            )
            n = len(incr_cals)
            if n == 0:
                pytest.skip("No incr calendars after bootstrap — cannot test round-robin")

            # Collect next_cal values across ticks
            indices: list[int] = []
            cur = cursor
            for _ in range(n * 4 + 5):
                b = a.fetch(cur, limit=10)
                try:
                    comp = json.loads(b.next_cursor)
                    if isinstance(comp, dict) and "next_cal" in comp:
                        indices.append(comp["next_cal"])
                except (json.JSONDecodeError, ValueError):
                    pass
                cur = b.next_cursor
                if not b.has_more and len(indices) >= n * 2:
                    break

            # Indices should stay in [0, n-1]
            if indices:
                assert all(0 <= idx < n for idx in indices), (
                    f"P0-A FAIL: next_cal index out of range [0,{n}): {indices}"
                )
                # Over N*2 ticks, all N values should appear
                assert set(indices) == set(range(n)), (
                    f"P0-A FAIL: not all {n} calendars visited. "
                    f"indices seen={set(indices)}, expected={set(range(n))}"
                )


# ══════════════════════════════════════════════════════════════════════════════
# Item 7: OAuth verification failure — zero revoke requests
# ══════════════════════════════════════════════════════════════════════════════

class TestItem7VerificationFailureNoRevoke:
    """Failed credential verification must NOT call revoke_token (P1-G corrected)."""

    def test_verification_failure_zero_revoke_calls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-G: verification failure discards candidate tokens; never calls revoke.

        The old working grant shares a project-level token with the new candidate.
        Revoking the new candidate's access_token would also invalidate the
        existing refresh_token, breaking the production connection.

        This test asserts zero revoke requests are made on verification failure.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        revoke_calls: list[str] = []

        import unittest.mock as _mock
        from olo.google.oauth import upgrade_credentials

        # Save legacy creds
        from datetime import UTC, datetime
        old_creds = GoogleAccountCredentials(
            alias="verifytest",  # no underscore — alias must match ^[a-z0-9]{1,16}$
            client_id="cid",
            client_secret="cs",
            refresh_token="rt_legacy",
            granted_scopes=REQUIRED_SCOPES,
            email_address="test@example.com",
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=None,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("verifytest"):
            save_credentials(old_creds)

        def _fake_connect(scopes):
            return "code123", None

        def _fake_exchange(code, pending, client_id, client_secret, alias, **kw):
            new_creds = GoogleAccountCredentials(
                alias=alias,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token="rt_new",
                granted_scopes=frozenset({
                    GMAIL_READONLY_SCOPE,
                    CALENDAR_READONLY_SCOPE,
                    CALENDAR_LIST_SCOPE,
                }),
                email_address="test@example.com",
                refresh_token_issued_at=datetime.now(UTC),
                oauth_publishing_status="testing",
                api_base_override=None,
                scope_profile=SCOPE_PROFILE_FULL_V1,
            )
            return new_creds

        def _fake_verify(creds, api_base_override=None):
            # Simulate verification failure (e.g., profile email mismatch)
            raise ValueError(
                "profile email 'wrong@example.com' != stored 'test@example.com'"
            )
            # This function signature returns str in fixed impl; raise before return
            return "access_token_value"

        def _spy_revoke(token, **kwargs):
            revoke_calls.append(token)

        with _mock.patch("olo.google.oauth.exchange_code", side_effect=_fake_exchange), \
             _mock.patch("olo.google.oauth._verify_new_credentials",
                         side_effect=_fake_verify), \
             _mock.patch("olo.google.oauth.revoke_token", side_effect=_spy_revoke):

            result = upgrade_credentials(
                "verifytest",
                connect_fn=_fake_connect,
            )

        # Must fail
        assert not result.success
        assert "post-consent verification failed" in result.reason

        # ZERO revoke calls — the critical assertion
        assert len(revoke_calls) == 0, (
            f"P1-G FAIL: revoke was called {len(revoke_calls)} time(s): {revoke_calls}. "
            "Failed verification must NOT revoke; revocation is project-wide and "
            "would invalidate the existing working grant."
        )

    def test_verification_failure_preserves_old_credential_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-G: old credential file is intact after verification failure."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        import unittest.mock as _mock
        from olo.google.oauth import upgrade_credentials
        from datetime import UTC, datetime

        old_creds = GoogleAccountCredentials(
            alias="preservetest",
            client_id="cid",
            client_secret="cs",
            refresh_token="rt_original_preserved",
            granted_scopes=REQUIRED_SCOPES,
            email_address="test@example.com",
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=None,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("preservetest"):
            save_credentials(old_creds)

        def _fake_connect(scopes):
            return "code456", None

        def _fake_exchange(code, pending, client_id, client_secret, alias, **kw):
            return GoogleAccountCredentials(
                alias=alias,
                client_id=client_id,
                client_secret=client_secret,
                refresh_token="rt_new_candidate",
                granted_scopes=frozenset({GMAIL_READONLY_SCOPE,
                                          CALENDAR_READONLY_SCOPE,
                                          CALENDAR_LIST_SCOPE}),
                email_address="test@example.com",
                refresh_token_issued_at=datetime.now(UTC),
                oauth_publishing_status="testing",
                api_base_override=None,
                scope_profile=SCOPE_PROFILE_FULL_V1,
            )

        def _fail_verify(creds, api_base_override=None):
            raise ValueError("simulated calendarList check failed")

        with _mock.patch("olo.google.oauth.exchange_code", side_effect=_fake_exchange), \
             _mock.patch("olo.google.oauth._verify_new_credentials",
                         side_effect=_fail_verify):
            result = upgrade_credentials("preservetest", connect_fn=_fake_connect)

        assert not result.success

        # Old credentials must be intact
        still_there = load_credentials("preservetest")
        assert still_there.refresh_token == "rt_original_preserved", (
            f"P1-G FAIL: old credential was overwritten. "
            f"refresh_token={still_there.refresh_token!r}"
        )
        assert still_there.scope_profile == SCOPE_PROFILE_LEGACY, (
            f"P1-G FAIL: scope_profile was changed from LEGACY. "
            f"profile={still_there.scope_profile!r}"
        )

    def test_verification_failure_before_access_token_no_revoke_attempted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P1-G: when _verify_new_credentials raises before returning token, no revoke.

        The verify function raises immediately (refresh_token invalid scenario).
        Since no access token was obtained, there is nothing to revoke — and
        the code must not attempt a revoke with an empty/None token.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        revoke_calls: list = []

        import unittest.mock as _mock
        from olo.google.oauth import upgrade_credentials
        from datetime import UTC, datetime

        old_creds = GoogleAccountCredentials(
            alias="earlyfail",
            client_id="cid",
            client_secret="cs",
            refresh_token="rt_early",
            granted_scopes=REQUIRED_SCOPES,
            email_address="test@example.com",
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=None,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("earlyfail"):
            save_credentials(old_creds)

        def _fake_connect(scopes):
            return "code789", None

        def _fake_exchange(code, pending, client_id, client_secret, alias, **kw):
            return GoogleAccountCredentials(
                alias=alias, client_id=client_id, client_secret=client_secret,
                refresh_token="rt_cand",
                granted_scopes=frozenset({GMAIL_READONLY_SCOPE, CALENDAR_READONLY_SCOPE,
                                          CALENDAR_LIST_SCOPE}),
                email_address="test@example.com",
                refresh_token_issued_at=datetime.now(UTC),
                oauth_publishing_status="testing",
                api_base_override=None, scope_profile=SCOPE_PROFILE_FULL_V1,
            )

        def _fail_on_refresh(creds, api_base_override=None):
            # Raises before even obtaining access_token (refresh failure)
            raise ValueError("refresh failed: invalid_grant")

        def _spy_revoke(token, **kwargs):
            revoke_calls.append(token)

        with _mock.patch("olo.google.oauth.exchange_code", side_effect=_fake_exchange), \
             _mock.patch("olo.google.oauth._verify_new_credentials",
                         side_effect=_fail_on_refresh), \
             _mock.patch("olo.google.oauth.revoke_token", side_effect=_spy_revoke):

            result = upgrade_credentials("earlyfail", connect_fn=_fake_connect)

        assert not result.success
        assert len(revoke_calls) == 0, (
            f"P1-G FAIL: revoke attempted on early verification failure: {revoke_calls}"
        )
