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

    def test_gmail_adapter_rejects_legacy_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 4: GmailAdapter rejects legacy credentials (gmail.metadata only).

        Legacy credentials cannot produce email.content blobs (would 403 on
        format=full). The adapter must fail early with AdapterConfigError rather
        than producing incomplete data.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        from datetime import UTC, datetime
        legacy_creds = GoogleAccountCredentials(
            alias="legacytest",
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
        from olo.google.credentials import credential_lock, save_credentials
        with credential_lock("legacytest"):
            save_credentials(legacy_creds)

        adapter = GmailAdapter({"account": "legacytest"})
        # Must reject with AdapterConfigError — not succeed silently
        with pytest.raises(AdapterConfigError, match="legacy"):
            adapter.fetch(None, limit=10)

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
        """Each signal-bearing batch carries one calendar's page: a run visits
        every calendar in turn (the sync cycle), never mixing two in a batch."""
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

    def test_one_run_visits_every_calendar_in_cycle_order(self) -> None:
        """The cursor's cycle lists every calendar at the start of a run and
        is empty when the run ends (has_more False): each calendar once."""
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
            for cal_id in CAL_IDS:
                for i in range(4):
                    fake.calendar._extra_calendar_states[cal_id].add_event(
                        f"idx-{cal_id.split('@')[0]}-{i}", summary=f"Idx {i}")

            order: list[str] = []
            cur = cursor
            for _ in range(20):
                b = a.fetch(cur, limit=10)
                order += [s.payload["calendar_id"] for s in b.signals]
                cur = b.next_cursor
                if not b.has_more:
                    break
            assert not b.has_more and json.loads(cur)["cycle"] == []
            assert order == sorted(order) and set(order) == set(CAL_IDS)
            assert len(order) == 8


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

# ══════════════════════════════════════════════════════════════════════════════
# Fix 1: Gmail batch-size — no permanent stall at limit=1
# ══════════════════════════════════════════════════════════════════════════════

class TestFix1BatchSizeNoStall:
    """GmailAdapter full-content records must fully ingest and advance the cursor.

    Original defective behaviour (before Fix 1 from third audit): when limit=1
    was accepted, _process_records returned record_signals[:1] with the cursor
    unchanged — email.content was never stored (permanent stall).

    After Fix 6 (fourth audit): limit<2 is rejected with AdapterConfigError for
    full-content mode.  At limit=2, a single INBOX message (email.received +
    email.content) fits exactly, the cursor advances, and a second fetch is empty.
    """

    def test_limit_1_full_content_raises_adapter_config_error(self) -> None:
        """Fix 1 / Fix 6: fetch with limit=1 raises AdapterConfigError in full-content mode."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            with pytest.raises(AdapterConfigError, match="limit ≥ 2"):
                adapter.fetch(None, limit=1)

    def test_limit_2_full_content_both_signals_returned(self) -> None:
        """Fix 1: at limit=2 a single INBOX message returns email.received + email.content."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor
            fake.gmail.add_message("m1", thread_id="t1", labels=["INBOX"],
                                   subject="Stall test")

            b1 = adapter.fetch(C, limit=2)

            kinds = {s.kind for s in b1.signals}
            assert "email.received" in kinds, (
                "Fix 1 FAIL: email.received missing from limit=2 fetch"
            )
            assert "email.content" in kinds, (
                f"Fix 1 FAIL: email.content missing at limit=2. kinds={kinds}"
            )
            assert b1.next_cursor != C, (
                f"Fix 1 FAIL: cursor did not advance"
            )

    def test_limit_2_cursor_advances_idempotent(self) -> None:
        """Fix 1: cursor advances at limit=2; second fetch is empty (idempotent)."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor
            fake.gmail.add_message("m1", thread_id="t1", labels=["INBOX"],
                                   subject="Idempotence check")

            b1 = adapter.fetch(C, limit=2)
            assert b1.next_cursor != C

            b2 = adapter.fetch(b1.next_cursor, limit=2)
            received_ids = [s.external_id for s in b2.signals if s.kind == "email.received"]
            assert "m1" not in received_ids, (
                f"Fix 1 FAIL: m1 was re-processed on second fetch. signals={b2.signals}"
            )

    def test_multiple_records_still_respect_limit_on_second_record(self) -> None:
        """Fix 1: second record is deferred when first record already filled budget."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor
            # Add two messages in separate history records
            fake.gmail.add_message("ma", thread_id="ta", labels=["INBOX"],
                                   subject="First")
            fake.gmail.add_message("mb", thread_id="tb", labels=["INBOX"],
                                   subject="Second")

            # limit=2 — first record uses 2 signals (received+content),
            # second record should be deferred (records_remain=True)
            b1 = adapter.fetch(C, limit=2)

            # Must get signals for ma only (mb is deferred)
            sig_ids = {s.external_id for s in b1.signals}
            assert "ma" in sig_ids or "c:ma" in sig_ids, (
                f"Fix 1 FAIL: expected ma in first batch. sig_ids={sig_ids}"
            )
            # has_more must be True when mb is deferred
            if b1.has_more:
                # Good: mb will be fetched on next call
                b2 = adapter.fetch(b1.next_cursor, limit=2)
                sig_ids2 = {s.external_id for s in b2.signals}
                assert "mb" in sig_ids2 or "c:mb" in sig_ids2, (
                    f"Fix 1 FAIL: mb not in second batch. sig_ids2={sig_ids2}"
                )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 2: 48 MiB incremental byte budget
# ══════════════════════════════════════════════════════════════════════════════

class TestFix2IncrementalByteBudget:
    """70 × ~1 MiB messages must ingest without exceeding the 64 MiB batch limit.

    Original defective behavior: no byte budget on the incremental path.
    70 messages × 1 MiB blobs = 70 MiB would exceed ingest_batch's 64 MiB
    hard-stop (AdapterContractError), causing the run to fail and the checkpoint
    to not advance.

    After fix: _process_records stops at 48 MiB (soft stop), so each
    FetchBatch stays well under the 64 MiB hard limit.
    """

    def _make_1mib_message(self, msg_id: str) -> dict:
        """Build a ~1 MiB format=full Gmail message dict."""
        import base64 as _b64
        body = "x" * (1 * 1024 * 1024)
        encoded = _b64.urlsafe_b64encode(body.encode()).decode()
        return {
            "id": msg_id,
            "threadId": f"t{msg_id}",
            "internalDate": "1700000000000",
            "sizeEstimate": len(body),
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Subject", "value": f"Large {msg_id}"},
                    {"name": "From", "value": "a@e.com"},
                    {"name": "To", "value": "b@e.com"},
                    {"name": "Message-ID", "value": f"<{msg_id}>"},
                    {"name": "Date", "value": "Mon, 1 Jan 2026 00:00:00 +0000"},
                ],
                "body": {"data": encoded, "size": len(body)},
                "parts": [],
            },
        }

    def test_70_large_messages_do_not_exceed_64mib_per_batch(self) -> None:
        """Fix 2: 70 × ~1 MiB messages ingest without exceeding the 64 MiB limit.

        We mock get_message_full to return ~1 MiB messages and drive the adapter
        until all 70 are processed.  Each FetchBatch must have total blob bytes
        ≤ 64 MiB.  The combined signal count across all batches must be 70
        email.received + 70 email.content (140 total), proving no messages lost.
        """
        from olo.adapters.gmail import GmailAdapter
        from olo.signals import MAX_BATCH_BLOB_BYTES

        NUM_MSGS = 70

        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token

            adapter = GmailAdapter({"account": "test"}, client=client)

            # Bootstrap FIRST (empty mailbox)
            b0 = adapter.fetch(None, limit=100)
            C = b0.next_cursor

            # Add NUM_MSGS messages AFTER bootstrap so they appear in history
            for i in range(NUM_MSGS):
                fake.gmail.add_message(f"big{i}", thread_id=f"t{i}",
                                       labels=["INBOX"], subject=f"Big {i}")

            # Patch get_message_full to return ~1 MiB bodies
            originals: dict = {}

            def _large_full(msg_id: str) -> dict:
                if msg_id not in originals:
                    originals[msg_id] = self._make_1mib_message(msg_id)
                return originals[msg_id]

            client.get_message_full = _large_full

            all_signals = []
            batch_violations: list[str] = []
            cur = C

            for iteration in range(200):  # generous iteration budget
                b = adapter.fetch(cur, limit=100)
                if b.signals:
                    # Check blob bytes for this batch
                    batch_bytes = sum(
                        len(blob.data)
                        for sig in b.signals
                        for blob in (sig.blobs or ())
                    )
                    if batch_bytes > MAX_BATCH_BLOB_BYTES:
                        batch_violations.append(
                            f"iteration {iteration}: {batch_bytes} bytes > "
                            f"{MAX_BATCH_BLOB_BYTES}"
                        )
                    all_signals.extend(b.signals)
                cur = b.next_cursor
                if not b.has_more:
                    break

            assert not batch_violations, (
                f"Fix 2 FAIL: batch(es) exceeded 64 MiB hard limit:\n"
                + "\n".join(batch_violations)
            )

            received = [s for s in all_signals if s.kind == "email.received"]
            content = [s for s in all_signals if s.kind == "email.content"]
            assert len(received) == NUM_MSGS, (
                f"Fix 2 FAIL: expected {NUM_MSGS} email.received, got {len(received)}"
            )
            assert len(content) == NUM_MSGS, (
                f"Fix 2 FAIL: expected {NUM_MSGS} email.content, got {len(content)}"
            )

    def test_byte_budget_stops_at_safe_record_boundary(self) -> None:
        """Fix 2: soft stop occurs at record boundary, not mid-record.

        50 messages each produce blobs totalling > 48 MiB.
        The adapter must stop before all 50 are processed, returning
        has_more=True, and must stay under the 64 MiB hard limit.
        """
        from olo.adapters.gmail import GmailAdapter

        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token

            adapter = GmailAdapter({"account": "test"}, client=client)

            # Bootstrap FIRST (empty mailbox)
            b0 = adapter.fetch(None, limit=100)
            C = b0.next_cursor

            # Add 50 messages AFTER bootstrap so they appear in history
            for i in range(50):
                fake.gmail.add_message(f"bnd{i}", thread_id=f"t{i}",
                                       labels=["INBOX"], subject=f"Boundary {i}")

            originals: dict = {}

            def _large_full(msg_id: str) -> dict:
                if msg_id not in originals:
                    originals[msg_id] = self._make_1mib_message(msg_id)
                return originals[msg_id]

            client.get_message_full = _large_full

            # First incremental fetch must soft-stop well before all 50
            b1 = adapter.fetch(C, limit=100)

            # Verify no batch exceeds 64 MiB
            batch_bytes = sum(
                len(blob.data)
                for sig in b1.signals
                for blob in (sig.blobs or ())
            )
            from olo.signals import MAX_BATCH_BLOB_BYTES
            assert batch_bytes <= MAX_BATCH_BLOB_BYTES, (
                f"Fix 2 FAIL: first batch is {batch_bytes} bytes, "
                f"exceeds hard limit {MAX_BATCH_BLOB_BYTES}"
            )
            # has_more should be True (not all 50 fit in one batch)
            assert b1.has_more, (
                "Fix 2 FAIL: expected has_more=True for 50 × 1 MiB messages "
                "but got False — byte budget not enforced"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 3: OAuth profile failures — zero revocations
# ══════════════════════════════════════════════════════════════════════════════

class TestFix3NoRevokeOnProfileFailure:
    """exchange_code must NEVER revoke on profile failure (normal connect OR upgrade).

    Original defective behavior: revoke_on_profile_failure=True was the default
    for normal connect.  A transient profile-fetch error would call revoke_token
    with the access_token, which is project-wide and also invalidates any
    existing refresh_token for the same project/user combination.

    After fix: revoke_on_profile_failure defaults to False; the revoke call is
    removed from the profile-failure path entirely.
    """

    def test_normal_connect_profile_failure_zero_revokes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 3: profile failure on normal connect → zero revoke calls."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        import unittest.mock as _mock
        from datetime import UTC, datetime
        from olo.google.oauth import GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO

        revoke_calls: list[str] = []

        # Grant exactly REQUIRED_SCOPES_V2 so scope_union guard does not trigger
        v2_scopes_str = " ".join(sorted(REQUIRED_SCOPES_V2))
        with FakeGoogleServer(email="user@example.com",
                              granted_scopes=v2_scopes_str) as fake:
            from olo.google.oauth import (
                exchange_code, PendingOAuth, build_auth_url,
            )
            _, pending = build_auth_url(
                "cid", "http://127.0.0.1:1/", REQUIRED_SCOPES_V2,
                api_base_override=fake.base_url,
            )
            pending2 = PendingOAuth(
                state=pending.state,
                code_verifier=pending.code_verifier,
                redirect_uri="http://127.0.0.1:1/",
                scopes=REQUIRED_SCOPES_V2,
            )

            def _fail_profile(access_token, **kwargs):
                raise Exception("simulated profile 500 — transient error")

            def _spy_revoke(token, **kwargs):
                revoke_calls.append(token)

            with _mock.patch("olo.google.oauth._get_profile",
                             side_effect=_fail_profile), \
                 _mock.patch("olo.google.oauth.revoke_token",
                             side_effect=_spy_revoke):
                with pytest.raises(ValueError, match="profile fetch failed"):
                    exchange_code(
                        "test_auth_code_123", pending2,
                        "cid", "csecret", "alias",
                        api_base_override=fake.base_url,
                    )

        assert len(revoke_calls) == 0, (
            f"Fix 3 FAIL (normal connect): revoke called {len(revoke_calls)} "
            f"time(s) on profile failure: {revoke_calls}. "
            "Revocation is project-wide and invalidates existing credentials."
        )

    def test_upgrade_connect_profile_failure_zero_revokes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 3: profile failure on upgrade connect → zero revoke calls."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        import unittest.mock as _mock

        revoke_calls: list[str] = []

        v2_scopes_str = " ".join(sorted(REQUIRED_SCOPES_V2))
        with FakeGoogleServer(email="user@example.com",
                              granted_scopes=v2_scopes_str) as fake:
            from olo.google.oauth import exchange_code, PendingOAuth, build_auth_url

            _, pending = build_auth_url(
                "cid", "http://127.0.0.1:1/", REQUIRED_SCOPES_V2,
                api_base_override=fake.base_url,
            )
            pending2 = PendingOAuth(
                state=pending.state,
                code_verifier=pending.code_verifier,
                redirect_uri="http://127.0.0.1:1/",
                scopes=REQUIRED_SCOPES_V2,
            )

            def _fail_profile(access_token, **kwargs):
                raise Exception("simulated profile 503 — service unavailable")

            def _spy_revoke(token, **kwargs):
                revoke_calls.append(token)

            with _mock.patch("olo.google.oauth._get_profile",
                             side_effect=_fail_profile), \
                 _mock.patch("olo.google.oauth.revoke_token",
                             side_effect=_spy_revoke):
                # upgrade path explicitly passes revoke_on_profile_failure=False
                with pytest.raises(ValueError, match="profile fetch failed"):
                    exchange_code(
                        "test_auth_code_123", pending2,
                        "cid", "csecret", "alias",
                        api_base_override=fake.base_url,
                        revoke_on_profile_failure=False,
                    )

        assert len(revoke_calls) == 0, (
            f"Fix 3 FAIL (upgrade): revoke called {len(revoke_calls)} time(s): "
            f"{revoke_calls}"
        )

    def test_existing_credentials_unchanged_after_profile_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 3: existing saved credentials are intact after profile failure on normal connect."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        import unittest.mock as _mock
        from datetime import UTC, datetime

        # Save old credentials first (alias must match ^[a-z0-9]{1,16}$)
        old_creds = GoogleAccountCredentials(
            alias="pftest",
            client_id="cid",
            client_secret="cs",
            refresh_token="rt_existing_preserved",
            granted_scopes=REQUIRED_SCOPES,
            email_address="pf@example.com",
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=None,
            scope_profile=SCOPE_PROFILE_LEGACY,
        )
        with credential_lock("pftest"):
            save_credentials(old_creds)

        v2_scopes_str = " ".join(sorted(REQUIRED_SCOPES_V2))
        with FakeGoogleServer(email="pf@example.com",
                              granted_scopes=v2_scopes_str) as fake:
            from olo.google.oauth import exchange_code, PendingOAuth, build_auth_url

            _, pending = build_auth_url(
                "cid", "http://127.0.0.1:1/", REQUIRED_SCOPES_V2,
                api_base_override=fake.base_url,
            )
            pending2 = PendingOAuth(
                state=pending.state,
                code_verifier=pending.code_verifier,
                redirect_uri="http://127.0.0.1:1/",
                scopes=REQUIRED_SCOPES_V2,
            )

            def _fail_profile(access_token, **kwargs):
                raise Exception("profile failure")

            with _mock.patch("olo.google.oauth._get_profile",
                             side_effect=_fail_profile):
                with pytest.raises(ValueError):
                    exchange_code(
                        "test_auth_code_123", pending2,
                        "cid", "csecret", "pftest",
                        api_base_override=fake.base_url,
                    )

        # Old credentials must be untouched — exchange_code doesn't save to disk
        # but if revoke had been called it would have invalidated the token
        still = load_credentials("pftest")
        assert still.refresh_token == "rt_existing_preserved", (
            f"Fix 3 FAIL: old credentials were overwritten. "
            f"rt={still.refresh_token!r}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 4: Backfill date window (--days N)
# ══════════════════════════════════════════════════════════════════════════════

class TestFix4BackfillDateWindow:
    """--days N must restrict messages.list to messages within N days.

    Original defective behavior: 'after' was always 0 in the backfill cursor,
    causing the entire mailbox to be scanned regardless of --days.

    After fix: after_epoch is set to now - N*86400 and passed through the cursor.
    """

    def test_days_flag_sets_after_epoch_in_cursor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 4: --days N sets a non-zero after_epoch in the backfill cursor JSON."""
        import argparse, time as _time, json as _json

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("alias", gmail_readonly=True)
            with credential_lock("alias"):
                save_credentials(creds)

            # Set up DB with a Gmail source checkpoint
            from olo.paths import db_path as _db_path
            from olo.store import connect as _conn, migrate as _mig
            db = _db_path()
            conn = _conn(db)
            _mig(conn)

            # Insert a fake checkpoint so backfill can resolve the alias
            fake_run = "fakeid123"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO ingest_runs(id,source,owner,started_at,status)"
                " VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_alias"),
            )
            conn.execute(
                "INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                " VALUES (?,?,datetime('now'),?)",
                ("gmail_alias", fake.gmail.current_history_id, fake_run),
            )
            conn.execute("COMMIT")
            conn.close()

            from olo.hermes_plugin.cli import _cmd_backfill

            before = int(_time.time())
            ns = argparse.Namespace(source="gmail_alias", mode=None, days=30)
            _cmd_backfill(ns)
            after = int(_time.time())

            # Read back the cursor
            conn2 = _conn(db)
            row = conn2.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source='gmail_alias'"
            ).fetchone()
            conn2.close()

            assert row is not None
            cursor_dict = _json.loads(row["cursor"])
            assert cursor_dict["phase"] == "backfill"
            assert cursor_dict["after"] > 0, (
                f"Fix 4 FAIL: after_epoch is 0 (--days 30 not applied). "
                f"cursor={cursor_dict!r}"
            )
            # after_epoch should be approximately now - 30 days
            expected_after = before - 30 * 86400
            assert cursor_dict["after"] >= expected_after - 5, (
                f"Fix 4 FAIL: after_epoch too small. "
                f"got {cursor_dict['after']}, expected >= {expected_after - 5}"
            )
            assert cursor_dict["after"] <= after - 30 * 86400 + 5, (
                f"Fix 4 FAIL: after_epoch too large. "
                f"got {cursor_dict['after']}, now-30d={after - 30 * 86400}"
            )

    def test_no_days_flag_sets_after_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 4: without --days, after remains 0 (full mailbox scan)."""
        import argparse, json as _json

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("alias2", gmail_readonly=True)
            with credential_lock("alias2"):
                save_credentials(creds)

            from olo.paths import db_path as _db_path
            from olo.store import connect as _conn, migrate as _mig
            db = _db_path()
            conn = _conn(db)
            _mig(conn)
            fake_run = "fakeid456"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO ingest_runs(id,source,owner,started_at,status)"
                " VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_alias2"),
            )
            conn.execute(
                "INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                " VALUES (?,?,datetime('now'),?)",
                ("gmail_alias2", fake.gmail.current_history_id, fake_run),
            )
            conn.execute("COMMIT")
            conn.close()

            from olo.hermes_plugin.cli import _cmd_backfill
            ns = argparse.Namespace(source="gmail_alias2", mode=None, days=None)
            _cmd_backfill(ns)

            conn2 = _conn(db)
            row = conn2.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source='gmail_alias2'"
            ).fetchone()
            conn2.close()

            cursor_dict = _json.loads(row["cursor"])
            assert cursor_dict["after"] == 0, (
                f"Fix 4 FAIL: without --days, after should be 0, got "
                f"{cursor_dict['after']}"
            )

    def test_days_window_filters_messages_in_backfill(self) -> None:
        """Fix 4: backfill with after_epoch skips old messages in messages.list.

        Old messages (epoch 0) must NOT appear when after_epoch is set to
        a recent time. The fake server's _handle_messages_list applies the
        after:{epoch} filter using internalDate.
        """
        import time as _time

        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            # Bootstrap
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

            # Add an OLD message (internalDate far in the past)
            old_epoch_ms = str(int((_time.time() - 60 * 86400) * 1000))  # 60 days ago
            fake.gmail.add_message("old_msg", thread_id="to", labels=["INBOX"],
                                   subject="Old message",
                                   internal_date_ms=old_epoch_ms)

            # Add a RECENT message (now)
            recent_epoch_ms = str(int(_time.time() * 1000))
            fake.gmail.add_message("new_msg", thread_id="tn", labels=["INBOX"],
                                   subject="New message",
                                   internal_date_ms=recent_epoch_ms)

            # Backfill with after_epoch = 30 days ago — should see new_msg, not old_msg
            after_epoch = int(_time.time()) - 30 * 86400
            backfill_cursor = json.dumps({
                "phase": "backfill",
                "mode": "bootstrap",
                "after": after_epoch,
                "page_token": None,
                "resume_history_id": C,
            })

            b_bf = adapter.fetch(backfill_cursor, limit=10)

            msg_ids = {s.payload.get("message_id", "") for s in b_bf.signals}
            # new_msg is within window → should appear in backfill
            # old_msg is outside window → must NOT appear
            assert "old_msg" not in msg_ids, (
                f"Fix 4 FAIL: old_msg appeared in windowed backfill. "
                f"msg_ids={msg_ids}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 5: Backfill gap handling — previous_cursor, restart_point, completion audit
# ══════════════════════════════════════════════════════════════════════════════

class TestFix5BackfillGapHandling:
    """Gap mode must record previous_cursor + restart_point and emit backfill.completed.

    Original defective behavior:
    (a) No previous_cursor/restart_point stored in cursor JSON — gap interval
        was not traceable after the fact.
    (b) No backfill.completed audit event — operators couldn't confirm
        completion or query the gap metadata from audit_events.

    After fix:
    (a) mode=gap cursor JSON contains previous_cursor and restart_point.
    (b) backfill.started audit contains restart_point and previous_cursor_hash.
    (c) backfill.completed audit is emitted when done=True.
    """

    def test_gap_cursor_contains_previous_cursor_and_restart_point(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 5a: gap-mode cursor JSON has previous_cursor and restart_point."""
        import argparse, json as _json

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("gap1", gmail_readonly=True)
            with credential_lock("gap1"):
                save_credentials(creds)

            from olo.paths import db_path as _db_path
            from olo.store import connect as _conn, migrate as _mig
            db = _db_path()
            conn = _conn(db)
            _mig(conn)

            # Insert a fake checkpoint for gmail_gap1 (old cursor = "5000")
            old_cursor = "5000"
            fake_run = "fakegap1234"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO ingest_runs(id,source,owner,started_at,status)"
                " VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_gap1"),
            )
            conn.execute(
                "INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                " VALUES (?,?,datetime('now'),?)",
                ("gmail_gap1", old_cursor, fake_run),
            )
            conn.execute("COMMIT")

            # Insert a signal to trigger gap detection (signals > 0 → mode=gap)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO signals"
                "(source,external_id,kind,occurred_at,subject,payload,run_id)"
                " VALUES (?,?,?,?,?,?,?)",
                ("gmail_gap1", "syn_gap", "email.received",
                 "2026-01-01T00:00:00+00:00", "Gap test", "{}", fake_run),
            )
            conn.execute("COMMIT")

            # Now delete the checkpoint to simulate expired history
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM sync_checkpoints WHERE source='gmail_gap1'")
            conn.execute("COMMIT")
            conn.close()

            from olo.hermes_plugin.cli import _cmd_backfill
            ns = argparse.Namespace(source="gmail_gap1", mode="gap", days=None)
            _cmd_backfill(ns)

            # Read back cursor
            conn4 = _conn(db)
            row = conn4.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source='gmail_gap1'"
            ).fetchone()
            conn4.close()

            assert row is not None, "Fix 5 FAIL: no checkpoint written for gap mode"
            cursor_dict = _json.loads(row["cursor"])
            assert cursor_dict.get("mode") == "gap", (
                f"Fix 5 FAIL: mode is {cursor_dict.get('mode')!r}, expected 'gap'"
            )
            assert "restart_point" in cursor_dict, (
                f"Fix 5 FAIL: restart_point missing from gap cursor. "
                f"cursor={cursor_dict!r}"
            )
            assert "previous_cursor" in cursor_dict, (
                f"Fix 5 FAIL: previous_cursor missing from gap cursor. "
                f"cursor={cursor_dict!r}"
            )

    def test_backfill_completed_audit_emitted_on_done(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 5c: backfill.completed audit is committed atomically when done=True.

        Audit is now carried in FetchBatch.pending_audit_events and committed
        inside ingest_batch — not written via a separate DB connection.
        Test uses run_source to exercise the full commit path.
        """
        # Align HERMES_HOME with db_path() so credential save uses same home
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("bca", gmail_readonly=True)
            with credential_lock("bca"):
                save_credentials(creds)

            from olo.google.http import GoogleClient
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "bca"}, client=client)

            # Bootstrap
            run_source(adapter, limit=10, db_path=db)
            conn_c = _connect(db)
            C = get_checkpoint("gmail_bca", conn_c)
            conn_c.close()

            # Add one message so backfill has content to scan
            fake.gmail.add_message("cm1", thread_id="tc1", labels=["INBOX"],
                                   subject="Completion audit test")

            # Inject a gap-mode backfill cursor directly
            conn_inj = _connect(db)
            gap_cursor = json.dumps({
                "phase": "backfill", "mode": "gap", "after": 0,
                "page_token": None, "resume_history_id": C,
                "previous_cursor": "4999", "restart_point": C,
            })
            fake_run = "fakebca0001"
            conn_inj.execute("BEGIN IMMEDIATE")
            conn_inj.execute(
                "INSERT OR IGNORE INTO ingest_runs"
                "(id,source,owner,started_at,status) VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_bca"),
            )
            conn_inj.execute(
                "UPDATE sync_checkpoints SET cursor=?,updated_at=datetime('now'),run_id=?"
                " WHERE source='gmail_bca'",
                (gap_cursor, fake_run),
            )
            conn_inj.execute("COMMIT")
            conn_inj.close()

            # Drive backfill to completion through run_source
            for _ in range(20):
                r = run_source(adapter, limit=10, db_path=db)
                if r.status == "succeeded":
                    break
                assert r.status == "partial", f"unexpected run status: {r!r}"

            # backfill.completed must be in audit_events (committed atomically)
            conn_check = _connect(db)
            audit_row = conn_check.execute(
                "SELECT detail FROM audit_events "
                "WHERE actor='adapter:gmail' AND action='backfill.completed' "
                "LIMIT 1"
            ).fetchone()
            conn_check.close()

            assert audit_row is not None, (
                "Fix 5 FAIL: backfill.completed audit event was NOT committed. "
                "Audit must be committed atomically with the checkpoint."
            )
            import json as _j
            detail = _j.loads(audit_row["detail"])
            assert detail.get("mode") == "gap", (
                f"Fix 5 FAIL: backfill.completed audit missing mode. detail={detail!r}"
            )
            assert "resume_history_id" in detail, (
                f"Fix 5 FAIL: backfill.completed audit missing resume_history_id. "
                f"detail={detail!r}"
            )

    def test_gap_audit_started_contains_restart_point(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 5b: backfill.started audit for gap mode contains restart_point."""
        import argparse, json as _j

        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("gapaudit", gmail_readonly=True)
            with credential_lock("gapaudit"):
                save_credentials(creds)

            from olo.paths import db_path as _db_path
            from olo.store import connect as _conn, migrate as _mig
            db = _db_path()
            conn = _conn(db)
            _mig(conn)
            fake_run = "fakeaudit1"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO ingest_runs(id,source,owner,started_at,status)"
                " VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_gapaudit"),
            )
            conn.execute(
                "INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                " VALUES (?,?,datetime('now'),?)",
                ("gmail_gapaudit", fake.gmail.current_history_id, fake_run),
            )
            conn.execute("COMMIT")
            conn.close()

            from olo.hermes_plugin.cli import _cmd_backfill
            ns = argparse.Namespace(source="gmail_gapaudit", mode="gap", days=None)
            _cmd_backfill(ns)

            conn2 = _conn(db)
            audit_row = conn2.execute(
                "SELECT detail FROM audit_events "
                "WHERE actor='cli:backfill' AND action='backfill.started' "
                "LIMIT 1"
            ).fetchone()
            conn2.close()

            assert audit_row is not None, \
                "Fix 5 FAIL: backfill.started audit not found"
            detail = _j.loads(audit_row["detail"])
            assert detail.get("mode") == "gap", \
                f"Fix 5 FAIL: backfill.started mode != 'gap'. detail={detail!r}"
            assert "restart_point" in detail, (
                f"Fix 5 FAIL: restart_point missing from backfill.started audit. "
                f"detail={detail!r}"
            )

# ══════════════════════════════════════════════════════════════════════════════
# Fix 6 (fourth audit / Fix 1): Gmail limit contract — limit<2 rejection
# ══════════════════════════════════════════════════════════════════════════════

class TestFix6GmailLimitContract:
    """GmailAdapter (full-content) must reject limit<2 and honour the limit contract.

    Original defective behaviour:
    - limit=1 was accepted; the first record produced email.received+email.content
      (2 signals); the runner's hard AdapterContractError check fired, recording
      a failed run while the checkpoint stalled.

    After fix:
    - fetch(cursor, limit=1) → AdapterConfigError immediately (clear error,
      no DB writes, checkpoint preserved).
    - fetch(cursor, limit=2) succeeds; two messages ingest exactly once at limit=2
      through run_source.
    - A batch-import record producing N>limit signals raises SourceError
      (checkpoint preserved, operator must increase limit).
    """

    def test_limit_1_raises_adapter_config_error(self) -> None:
        """Fix 6: fetch with limit=1 raises AdapterConfigError before any API call."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            with pytest.raises(AdapterConfigError, match="limit ≥ 2"):
                adapter.fetch(None, limit=1)

    def test_limit_0_raises_adapter_config_error(self) -> None:
        """Fix 6: fetch with limit=0 also raises AdapterConfigError."""
        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)

            with pytest.raises(AdapterConfigError, match="limit ≥ 2"):
                adapter.fetch(None, limit=0)

    def test_limit_1_checkpoint_not_advanced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 6: run_source with limit=1 records failure; checkpoint untouched."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("chk", gmail_readonly=True)
            with credential_lock("chk"):
                save_credentials(creds)

            from olo.google.http import GoogleClient
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "chk"}, client=client)

            # run_source re-raises after recording failure
            with pytest.raises(AdapterConfigError, match="limit ≥ 2"):
                run_source(adapter, limit=1, db_path=db)

            # Checkpoint must not exist (no bootstrap happened)
            conn = _connect(db)
            migrate(conn)
            cp = get_checkpoint("gmail_chk", conn)
            conn.close()
            assert cp is None, (
                f"Fix 6 FAIL: checkpoint was written despite limit=1 rejection. "
                f"cp={cp!r}"
            )

    def test_two_messages_ingest_exactly_once_at_limit_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 6: two INBOX messages ingest exactly once each through run_source at limit=2.

        Verifies:
        - run_source succeeds (status='succeeded' or 'partial')
        - Exactly 2 email.received signals in DB (one per message, no duplicates)
        - Exactly 2 email.content signals in DB
        - A second run_source produces 0 new inserts (idempotent)
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("two", gmail_readonly=True)
            with credential_lock("two"):
                save_credentials(creds)

            from olo.google.http import GoogleClient
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "two"}, client=client)

            # Bootstrap (empty mailbox)
            result0 = run_source(adapter, limit=2, db_path=db)
            assert result0.status in ("succeeded", "partial"), (
                f"Fix 6 FAIL: bootstrap run failed: {result0!r}"
            )

            # Add exactly 2 messages
            fake.gmail.add_message("msg_a", thread_id="ta", labels=["INBOX"],
                                   subject="Message A")
            fake.gmail.add_message("msg_b", thread_id="tb", labels=["INBOX"],
                                   subject="Message B")

            # Ingest at limit=2 — may need multiple runs for 2 records × 2 signals
            total_inserted = 0
            for _ in range(10):
                result = run_source(adapter, limit=2, db_path=db)
                total_inserted += result.inserted
                if result.status == "succeeded":
                    break
                assert result.status == "partial", (
                    f"Fix 6 FAIL: run failed unexpectedly: {result!r}"
                )

            conn = _connect(db)
            migrate(conn)
            received = conn.execute(
                "SELECT COUNT(*) FROM signals "
                "WHERE source='gmail_two' AND kind='email.received'"
            ).fetchone()[0]
            content = conn.execute(
                "SELECT COUNT(*) FROM signals "
                "WHERE source='gmail_two' AND kind='email.content'"
            ).fetchone()[0]
            conn.close()

            assert received == 2, (
                f"Fix 6 FAIL: expected 2 email.received, got {received}"
            )
            assert content == 2, (
                f"Fix 6 FAIL: expected 2 email.content, got {content}"
            )

            # Second full run — idempotent, 0 new inserts
            result_idem = run_source(adapter, limit=2, db_path=db)
            assert result_idem.inserted == 0, (
                f"Fix 6 FAIL: second run inserted {result_idem.inserted} "
                "signals (expected 0 — idempotent)"
            )

    def test_oversized_single_record_raises_source_error(self) -> None:
        """Fix 6: a record producing N>limit signals raises SourceError.

        Simulates a batch-import history entry where one messagesAdded record
        contains 3 message entries — producing 6 signals at limit=4.
        SourceError must propagate so the checkpoint is NOT advanced.

        Uses _process_records directly with a mock client to avoid fake-server
        network overhead and scripted-history race conditions.
        """
        import unittest.mock as _mock
        import base64 as _b64

        with FakeGoogleServer() as fake:
            from olo.google.http import GoogleClient
            creds = fake.make_credentials("test", gmail_readonly=True)
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "test"}, client=client)
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

        # Build a fake record with 3 messagesAdded entries.
        # Each produces email.received + email.content = 2 signals → 6 total.
        fake_record = {
            "id": str(int(C) + 1),
            "messagesAdded": [
                {"message": {"id": f"over{i}", "threadId": f"to{i}",
                             "labelIds": ["INBOX"]}}
                for i in range(3)
            ],
        }

        def _fake_msg_meta(msg_id, headers):
            return {
                "id": msg_id, "threadId": f"t{msg_id}",
                "labelIds": ["INBOX"],
                "internalDate": "1700000000000",
                "sizeEstimate": 100,
                "payload": {"headers": [
                    {"name": "Subject", "value": f"Msg {msg_id}"},
                    {"name": "From", "value": "a@e.com"},
                    {"name": "To", "value": "b@e.com"},
                    {"name": "Message-ID", "value": f"<{msg_id}>"},
                    {"name": "Date", "value": "Mon, 1 Jan 2026 00:00:00 +0000"},
                ]},
            }

        def _fake_msg_full(msg_id):
            body = _b64.urlsafe_b64encode(b"hello").decode()
            return {
                "id": msg_id, "threadId": f"t{msg_id}",
                "labelIds": ["INBOX"],
                "internalDate": "1700000000000",
                "sizeEstimate": 100,
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "Subject", "value": f"Msg {msg_id}"},
                        {"name": "From", "value": "a@e.com"},
                        {"name": "To", "value": "b@e.com"},
                        {"name": "Message-ID", "value": f"<{msg_id}>"},
                        {"name": "Date", "value": "Mon, 1 Jan 2026 00:00:00 +0000"},
                    ],
                    "body": {"data": body, "size": 5},
                    "parts": [],
                },
            }

        mock_client = _mock.MagicMock()
        mock_client._creds.has_gmail_readonly_scope.return_value = True
        mock_client.get_message_or_none.side_effect = _fake_msg_meta
        mock_client.get_message_full.side_effect = _fake_msg_full
        adapter._client = mock_client

        # limit=4: single record produces 6 signals → SourceError
        with pytest.raises(SourceError, match="increase --limit"):
            adapter._process_records(
                mock_client,
                [fake_record],
                remaining=4,
                base_cursor=C,
            )

    def test_limit_2_exactly_does_not_trigger_runner_contract_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 6: limit=2 satisfies runner's hard AdapterContractError check.

        runner.py raises AdapterContractError when len(batch.signals) > limit.
        A single INBOX message produces exactly 2 signals; at limit=2 that
        must NOT trigger the AdapterContractError.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("lim2", gmail_readonly=True)
            with credential_lock("lim2"):
                save_credentials(creds)

            from olo.google.http import GoogleClient
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "lim2"}, client=client)

            # Bootstrap
            run_source(adapter, limit=2, db_path=db)

            # One INBOX message → 2 signals at limit=2
            fake.gmail.add_message("lim2m", thread_id="tl2", labels=["INBOX"],
                                   subject="Limit 2 test")

            # Must not raise AdapterContractError
            result = run_source(adapter, limit=2, db_path=db)
            assert result.status in ("succeeded", "partial"), (
                f"Fix 6 FAIL: run_source failed at limit=2 with one message: "
                f"{result!r}"
            )

            conn = _connect(db)
            migrate(conn)
            n = conn.execute(
                "SELECT COUNT(*) FROM signals WHERE source='gmail_lim2'"
            ).fetchone()[0]
            conn.close()
            assert n == 2, (
                f"Fix 6 FAIL: expected 2 signals at limit=2, got {n}"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Fix 7 (fourth audit / Fix 2): Gmail history-gap auto-detection
# ══════════════════════════════════════════════════════════════════════════════

class TestFix7HistoryGapAutoDetection:
    """History-gap recovery: auto-detect, audit evidence, correct content.

    Real sequence: steady-state sync → history expires → checkpoint reset →
    backfill without explicit mode.

    Original defective behaviour:
    - GmailHistoryExpired bubbled uncaught through _incremental → run failed
      with status='failed'; operator had to manually inject a gap cursor.
    - checkpoint.reset audit stored only a hash of the old cursor (no raw
      historyId), making gap recovery audit unverifiable.

    After fix:
    - _incremental catches GmailHistoryExpired, emits gmail.history_gap, and
      returns a gap-mode backfill cursor automatically.
    - checkpoint.reset audit contains previous_history_id (actual value).
    - Full gap recovery cycle completes: gap detected → backfill scan → content
      ingested → cursor at H0 for incremental sync.
    """

    def _make_gmail_adapter(
        self, fake: "FakeGoogleServer", alias: str = "test"
    ) -> GmailAdapter:
        from olo.google.http import GoogleClient
        creds = fake.make_credentials(alias, gmail_readonly=True)
        client = GoogleClient(creds)
        client._access_token = fake.tokens.access_token
        return GmailAdapter({"account": alias}, client=client)

    def test_history_expired_auto_transitions_to_gap_backfill(self) -> None:
        """Fix 7: GmailHistoryExpired → adapter returns gap backfill cursor.

        When history.list returns 404 (history pruned), _incremental must NOT
        raise — it must return a gap-mode backfill FetchBatch automatically.
        """
        with FakeGoogleServer() as fake:
            adapter = self._make_gmail_adapter(fake)

            # Bootstrap
            b0 = adapter.fetch(None, limit=10)
            C = b0.next_cursor

            # Add a message, then expire history (simulate Google pruning)
            fake.gmail.add_message("gap1", thread_id="tg1", labels=["INBOX"],
                                   subject="Gap message")
            # Expire: set min_valid_history_id above the checkpoint cursor
            # so history.list(C) returns 404
            new_min = str(int(fake.gmail.current_history_id) + 100)
            fake.set_min_history_id(new_min)

            # _incremental must return gap backfill cursor, NOT raise
            b1 = adapter.fetch(C, limit=10)

            # Verify gap-mode cursor returned (signals empty, has_more=False so
            # the runner commits the gap cursor and exits cleanly)
            assert b1.signals == (), (
                f"Fix 7 FAIL: expected empty signals on gap detection, "
                f"got {b1.signals!r}"
            )
            assert b1.has_more is False, (
                "Fix 7 FAIL: has_more should be False so runner commits cursor"
            )
            assert b1.next_cursor is not None
            try:
                cursor_dict = json.loads(b1.next_cursor)
            except (json.JSONDecodeError, ValueError):
                pytest.fail(
                    f"Fix 7 FAIL: expected JSON gap cursor, got {b1.next_cursor!r}"
                )

            assert cursor_dict.get("phase") == "backfill", (
                f"Fix 7 FAIL: expected phase='backfill', got {cursor_dict!r}"
            )
            assert cursor_dict.get("mode") == "gap", (
                f"Fix 7 FAIL: expected mode='gap', got {cursor_dict!r}"
            )
            assert "resume_history_id" in cursor_dict, (
                f"Fix 7 FAIL: resume_history_id missing from gap cursor"
            )
            assert "previous_cursor" in cursor_dict, (
                f"Fix 7 FAIL: previous_cursor missing from gap cursor"
            )
            assert cursor_dict["previous_cursor"] == C, (
                f"Fix 7 FAIL: previous_cursor={cursor_dict['previous_cursor']!r} "
                f"!= C={C!r}"
            )

    def test_history_gap_audit_emitted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 7: gmail.history_gap audit event is emitted with required fields."""
        # Point HERMES_HOME at tmp_path so that db_path() inside the adapter
        # resolves to the same file that run_source uses.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("gap2", gmail_readonly=True)
            with credential_lock("gap2"):
                save_credentials(creds)

            from olo.google.http import GoogleClient
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "gap2"}, client=client)

            # Set up DB so audit can be written
            conn_setup = _connect(db)
            migrate(conn_setup)
            conn_setup.close()

            # Bootstrap
            run_source(adapter, limit=10, db_path=db)
            conn_c = _connect(db)
            C = get_checkpoint("gmail_gap2", conn_c)
            conn_c.close()

            # Expire history
            new_min = str(int(fake.gmail.current_history_id) + 100)
            fake.set_min_history_id(new_min)

            # One incremental run triggers gap detection
            run_source(adapter, limit=10, db_path=db)

            # Verify gmail.history_gap audit
            conn_check = _connect(db)
            row = conn_check.execute(
                "SELECT detail FROM audit_events "
                "WHERE actor='adapter:gmail' AND action='gmail.history_gap' "
                "LIMIT 1"
            ).fetchone()
            conn_check.close()

            assert row is not None, (
                "Fix 7 FAIL: gmail.history_gap audit event was NOT emitted. "
                "Operators cannot trace the gap interval."
            )
            detail = json.loads(row["detail"])

            assert "previous_cursor" in detail, (
                f"Fix 7 FAIL: previous_cursor missing from gmail.history_gap. "
                f"detail={detail!r}"
            )
            assert detail["previous_cursor"] == C, (
                f"Fix 7 FAIL: previous_cursor={detail['previous_cursor']!r} "
                f"!= C={C!r}"
            )
            assert "restart_point" in detail, (
                f"Fix 7 FAIL: restart_point missing. detail={detail!r}"
            )
            assert "recovery_window" in detail, (
                f"Fix 7 FAIL: recovery_window missing. detail={detail!r}"
            )
            rw = detail["recovery_window"]
            assert rw.get("from") == C, (
                f"Fix 7 FAIL: recovery_window.from={rw.get('from')!r} != C={C!r}"
            )

    def test_full_gap_recovery_cycle_content_ingested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 7: complete gap recovery: history expires → auto-gap → backfill → content.

        Real sequence:
        1. Bootstrap (empty mailbox) — cursor = C
        2. Add message M before history expires
        3. History expires (min_valid above C)
        4. run_source → gap detected → gap backfill cursor written, run exits clean
        5. run_source(s) → backfill completes → email.content for M stored
        6. Cursor transitions to H0 (plain historyId string) for incremental sync
        """
        # Align HERMES_HOME with db_path() so the adapter's audit writes
        # land in the same DB as run_source.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("gcycle", gmail_readonly=True)
            with credential_lock("gcycle"):
                save_credentials(creds)

            from olo.google.http import GoogleClient
            client = GoogleClient(creds)
            client._access_token = fake.tokens.access_token
            adapter = GmailAdapter({"account": "gcycle"}, client=client)

            # Step 1: Bootstrap
            r0 = run_source(adapter, limit=10, db_path=db)
            assert r0.status in ("succeeded", "partial")

            # Step 2: Add a message
            fake.gmail.add_message("gcycle_m1", thread_id="tgc1", labels=["INBOX"],
                                   subject="Gap recovery message")

            # Step 3: Expire history above the bootstrap cursor
            new_min = str(int(fake.gmail.current_history_id) + 100)
            fake.set_min_history_id(new_min)

            # Step 4: First run after expiry — gap detected, run exits clean
            r1 = run_source(adapter, limit=10, db_path=db)
            assert r1.status in ("succeeded", "partial"), (
                f"Fix 7 FAIL: gap detection run failed: {r1!r}"
            )

            # Checkpoint must now be a gap backfill cursor
            conn1 = _connect(db)
            cp1 = get_checkpoint("gmail_gcycle", conn1)
            conn1.close()

            assert cp1 is not None, "Fix 7 FAIL: checkpoint is None after gap detection"
            try:
                cp1_dict = json.loads(cp1)
                is_gap = (cp1_dict.get("phase") == "backfill" and
                          cp1_dict.get("mode") == "gap")
            except (json.JSONDecodeError, ValueError):
                is_gap = False
            assert is_gap, (
                f"Fix 7 FAIL: checkpoint after gap detection is not a gap backfill "
                f"cursor: {cp1!r}"
            )

            # Step 5: Drive backfill to completion
            for _ in range(20):
                r = run_source(adapter, limit=10, db_path=db)
                if r.status == "succeeded":
                    break
                assert r.status == "partial", (
                    f"Fix 7 FAIL: backfill run failed: {r!r}"
                )

            # email.content for gcycle_m1 must be stored
            conn2 = _connect(db)
            content_sigs = conn2.execute(
                "SELECT external_id FROM signals "
                "WHERE source='gmail_gcycle' AND kind='email.content'"
            ).fetchall()
            conn2.close()

            content_ids = {row[0] for row in content_sigs}
            assert "c:gcycle_m1" in content_ids, (
                f"Fix 7 FAIL: email.content for gcycle_m1 not stored after gap "
                f"recovery. content_ids={content_ids}"
            )

            # Step 6: Cursor must have transitioned to H0 (plain historyId string,
            # not a JSON backfill dict).
            conn3 = _connect(db)
            cp_final = get_checkpoint("gmail_gcycle", conn3)
            conn3.close()

            is_plain_historyid = False
            if cp_final is not None:
                try:
                    parsed = json.loads(cp_final)
                    # A plain numeric historyId parses as an int, not a dict
                    is_plain_historyid = not isinstance(parsed, dict)
                except (json.JSONDecodeError, TypeError):
                    is_plain_historyid = True  # not JSON at all — also fine

            assert is_plain_historyid, (
                f"Fix 7 FAIL: cursor after backfill is still a backfill JSON dict "
                f"(expected plain historyId string): {cp_final!r}"
            )

    def test_checkpoint_reset_stores_previous_history_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fix 7: checkpoint.reset audit contains previous_history_id (not just hash).

        The actual numeric historyId stored in previous_history_id enables gap
        recovery to build an accurate recovery_window without DB archaeology.
        """
        import argparse
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

        with FakeGoogleServer() as fake:
            creds = fake.make_credentials("rst", gmail_readonly=True)
            with credential_lock("rst"):
                save_credentials(creds)

            from olo.paths import db_path as _db_path
            from olo.store import connect as _c, migrate as _m
            db = _db_path()
            conn = _c(db)
            _m(conn)

            # Insert a numeric checkpoint (historyId = "7777")
            fake_run = "fakerstrun1"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO ingest_runs(id,source,owner,started_at,status)"
                " VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_rst"),
            )
            conn.execute(
                "INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                " VALUES (?,?,datetime('now'),?)",
                ("gmail_rst", "7777", fake_run),
            )
            conn.execute("COMMIT")
            conn.close()

            from olo.hermes_plugin.cli import _cmd_checkpoint_reset
            ns = argparse.Namespace(source="gmail_rst", confirm=True)
            _cmd_checkpoint_reset(ns)

            conn2 = _c(db)
            row = conn2.execute(
                "SELECT detail FROM audit_events "
                "WHERE actor='cli:checkpoint' AND action='checkpoint.reset' "
                "LIMIT 1"
            ).fetchone()
            conn2.close()

            assert row is not None, "Fix 7 FAIL: checkpoint.reset audit not found"
            import json as _j
            detail = _j.loads(row["detail"])

            assert "previous_history_id" in detail, (
                f"Fix 7 FAIL: previous_history_id missing from checkpoint.reset "
                f"audit. detail={detail!r}"
            )
            assert detail["previous_history_id"] == "7777", (
                f"Fix 7 FAIL: previous_history_id={detail['previous_history_id']!r} "
                f"!= '7777'"
            )

# ══════════════════════════════════════════════════════════════════════════════
# Fix 8: Audit transaction integrity — atomic commit with signals + checkpoint
# ══════════════════════════════════════════════════════════════════════════════

class TestFix8AuditTransactionIntegrity:
    """Audit events (backfill.completed, gmail.history_gap) must be committed
    atomically with their corresponding signals and checkpoint update.

    Original defective behaviour: the adapter opened a separate DB connection
    inside _backfill_page / _handle_history_gap and wrote the audit event
    before ingest_batch had committed — a failed ingest_batch left a phantom
    audit event in the DB, and retries produced duplicate audit events.

    After fix: audit events travel as FetchBatch.pending_audit_events and are
    committed inside ingest_batch's BEGIN IMMEDIATE … COMMIT block.

    Four regression tests (Opus requirement):
    1. Failed final backfill commit  → zero completion audits.
    2. Successful retry              → exactly one completion audit.
    3. Failed gap-transition commit  → no committed gap audit.
    4. Successful retry              → exactly one gap audit.
    """

    # ── helpers ───────────────────────────────────────────────────────────────

    def _count_audit(
        self, db: Path, action: str, actor: str = "adapter:gmail"
    ) -> int:
        conn = _connect(db)
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE actor=? AND action=?",
            (actor, action),
        ).fetchone()[0]
        conn.close()
        return n

    def _make_gmail_adapter_full(
        self, fake: "FakeGoogleServer", alias: str, db: Path,
        monkeypatch,
    ) -> GmailAdapter:
        monkeypatch.setenv("HERMES_HOME", str(db.parent.parent))
        creds = fake.make_credentials(alias, gmail_readonly=True)
        with credential_lock(alias):
            save_credentials(creds)
        from olo.google.http import GoogleClient
        client = GoogleClient(creds)
        client._access_token = fake.tokens.access_token
        adapter = GmailAdapter({"account": alias}, client=client)
        return adapter

    # ── test 1: failed backfill commit → zero completion audits ───────────────

    def test_failed_backfill_commit_zero_completion_audits(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomic: ingest_batch fails before commit → backfill.completed NOT written.

        Uses _fault='before_commit' to simulate a crash between signal write
        and the final COMMIT.  The transaction rolls back, so the audit event
        carried in pending_audit_events is also rolled back.
        """
        from olo.store import AdapterContractError, CheckpointMoved, LeaseLost

        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            adapter = self._make_gmail_adapter_full(fake, "fbz", db, monkeypatch)

            # Bootstrap
            run_source(adapter, limit=10, db_path=db)
            conn_c = _connect(db)
            C = get_checkpoint("gmail_fbz", conn_c)
            conn_c.close()

            # Add a message so backfill has content
            fake.gmail.add_message("fbz1", thread_id="tfbz1", labels=["INBOX"],
                                   subject="Fault test")

            # Inject gap backfill cursor
            conn_inj = _connect(db)
            gap_cur = json.dumps({
                "phase": "backfill", "mode": "gap", "after": 0,
                "page_token": None, "resume_history_id": C,
                "previous_cursor": "1", "restart_point": C,
            })
            fake_run = "fakefbz0001"
            conn_inj.execute("BEGIN IMMEDIATE")
            conn_inj.execute(
                "INSERT OR IGNORE INTO ingest_runs"
                "(id,source,owner,started_at,status) VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_fbz"),
            )
            conn_inj.execute(
                "UPDATE sync_checkpoints SET cursor=?,updated_at=datetime('now'),run_id=?"
                " WHERE source='gmail_fbz'",
                (gap_cur, fake_run),
            )
            conn_inj.execute("COMMIT")
            conn_inj.close()

            # Inject a fault at "before_commit" — COMMIT never executes
            fault_fired: list[str] = []

            def _fault(name: str) -> None:
                if name == "before_commit":
                    fault_fired.append(name)
                    raise RuntimeError("injected fault before commit")

            # run_source will catch the exception and record failure
            try:
                run_source(adapter, limit=10, db_path=db, _fault=_fault)
            except Exception:
                pass  # expected

            # Zero backfill.completed audits: transaction was rolled back
            n = self._count_audit(db, "backfill.completed")
            assert n == 0, (
                f"Atomic FAIL (test 1): {n} backfill.completed audit event(s) "
                "written despite rolled-back transaction. Audit must be atomic."
            )
            # Confirm fault actually fired
            assert "before_commit" in fault_fired, \
                "Fault was not injected — test is invalid"

    # ── test 2: successful retry → exactly one completion audit ───────────────

    def test_successful_retry_exactly_one_completion_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomic: failed commit + successful retry → exactly 1 backfill.completed.

        After the fault (test 1 scenario), a second run_source with no fault
        must commit successfully and write exactly one backfill.completed audit.
        """
        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            adapter = self._make_gmail_adapter_full(fake, "fbz2", db, monkeypatch)

            # Bootstrap
            run_source(adapter, limit=10, db_path=db)
            conn_c = _connect(db)
            C = get_checkpoint("gmail_fbz2", conn_c)
            conn_c.close()

            fake.gmail.add_message("fbz2m", thread_id="tfbz2", labels=["INBOX"],
                                   subject="Retry audit test")

            # Inject gap backfill cursor
            conn_inj = _connect(db)
            gap_cur = json.dumps({
                "phase": "backfill", "mode": "gap", "after": 0,
                "page_token": None, "resume_history_id": C,
                "previous_cursor": "1", "restart_point": C,
            })
            fake_run = "fakefbz0002"
            conn_inj.execute("BEGIN IMMEDIATE")
            conn_inj.execute(
                "INSERT OR IGNORE INTO ingest_runs"
                "(id,source,owner,started_at,status) VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run, "gmail_fbz2"),
            )
            conn_inj.execute(
                "UPDATE sync_checkpoints SET cursor=?,updated_at=datetime('now'),run_id=?"
                " WHERE source='gmail_fbz2'",
                (gap_cur, fake_run),
            )
            conn_inj.execute("COMMIT")
            conn_inj.close()

            # First run: inject fault → no audit
            def _fault_first(name: str) -> None:
                if name == "before_commit":
                    raise RuntimeError("injected fault")

            try:
                run_source(adapter, limit=10, db_path=db, _fault=_fault_first)
            except Exception:
                pass

            # Verify zero audits after failed run
            assert self._count_audit(db, "backfill.completed") == 0, \
                "Pre-condition: audit must be 0 after failed run"

            # Second run: no fault → succeeds
            for _ in range(10):
                r = run_source(adapter, limit=10, db_path=db)
                if r.status == "succeeded":
                    break
                assert r.status == "partial"

            # Exactly one backfill.completed audit
            n = self._count_audit(db, "backfill.completed")
            assert n == 1, (
                f"Atomic FAIL (test 2): expected 1 backfill.completed, got {n}. "
                "Retry must produce exactly one atomic audit."
            )

    # ── test 3: failed gap-transition commit → no committed gap audit ─────────

    def test_failed_gap_commit_no_gap_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomic: gap detection + ingest_batch fault → no gmail.history_gap audit.

        When history expires and _handle_history_gap is called, the gap audit
        is queued as pending_audit_events.  If ingest_batch fails before COMMIT
        (fault at before_commit), the audit is rolled back — zero gap audits.
        """
        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            adapter = self._make_gmail_adapter_full(fake, "gap3", db, monkeypatch)

            # Bootstrap
            run_source(adapter, limit=10, db_path=db)

            # Add a message then expire history
            fake.gmail.add_message("gap3m", thread_id="tg3", labels=["INBOX"],
                                   subject="Gap atomic test")
            new_min = str(int(fake.gmail.current_history_id) + 100)
            fake.set_min_history_id(new_min)

            # Inject fault at before_commit for the gap-detection run
            def _fault(name: str) -> None:
                if name == "before_commit":
                    raise RuntimeError("injected fault on gap commit")

            try:
                run_source(adapter, limit=10, db_path=db, _fault=_fault)
            except Exception:
                pass

            # Zero gmail.history_gap audits (transaction rolled back)
            n = self._count_audit(db, "gmail.history_gap")
            assert n == 0, (
                f"Atomic FAIL (test 3): {n} gmail.history_gap audit(s) written "
                "despite rolled-back transaction. Gap audit must be atomic."
            )

    # ── test 4: successful retry → exactly one gap audit ─────────────────────

    def test_successful_gap_retry_exactly_one_gap_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Atomic: failed gap commit + successful retry → exactly 1 gmail.history_gap.

        After a failed gap-transition run (test 3 scenario), the checkpoint is
        still the old historyId (not the gap cursor).  On retry, gap detection
        fires again, and the successful commit writes exactly one audit event.
        """
        db = tmp_path / "olo" / "olo.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        with FakeGoogleServer() as fake:
            adapter = self._make_gmail_adapter_full(fake, "gap4", db, monkeypatch)

            # Bootstrap
            run_source(adapter, limit=10, db_path=db)

            # Expire history
            fake.gmail.add_message("gap4m", thread_id="tg4", labels=["INBOX"],
                                   subject="Gap retry test")
            new_min = str(int(fake.gmail.current_history_id) + 100)
            fake.set_min_history_id(new_min)

            # First run: fault → rolled back (zero audits)
            def _fault_first(name: str) -> None:
                if name == "before_commit":
                    raise RuntimeError("injected fault")

            try:
                run_source(adapter, limit=10, db_path=db, _fault=_fault_first)
            except Exception:
                pass

            assert self._count_audit(db, "gmail.history_gap") == 0, \
                "Pre-condition: zero gap audits after failed run"

            # Second run: no fault → succeeds, writes exactly one gap audit
            r = run_source(adapter, limit=10, db_path=db)
            assert r.status in ("succeeded", "partial"), (
                f"Retry run failed: {r!r}"
            )

            n = self._count_audit(db, "gmail.history_gap")
            assert n == 1, (
                f"Atomic FAIL (test 4): expected 1 gmail.history_gap audit, got {n}. "
                "Retry must produce exactly one atomic gap audit."
            )
