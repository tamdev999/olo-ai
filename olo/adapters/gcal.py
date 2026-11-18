"""Olo AI — Google Calendar adapter (read-only, all readable calendars).

§6 (M2): sync algorithm, signal mapping, §5.0 empty-page rule.
B4 (M2b): composite cursor v2, calendarList discovery, per-calendar paging,
  calendar.calendar.added/removed signals, 410 self-heal per calendar,
  v1 cursor migration, lookback_days 0–365.
B4-content (M2b): calendar.event.content signals with full event document blob
  (including description), emitted alongside calendar.event.changed/cancelled.
  Description is in the blob; it is NOT in the metadata payload (§6.3 privacy).

Registered as "gcal"; per-account source = "gcal_<alias>".

Opus audit fixes (M2b):
  - P0-A: round-robin calendar scheduling via cursor-embedded next_cal index;
    prevents secondary calendars from starving when multiple are registered.
  - P0-B: deleted calendars filtered from full-scan registration.
  - P1-D: deduplicate "primary"/email-id after v1 migration.
  - P1-E: optional calendars= option to restrict which calendars are synced.

No side effects at import time.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, UTC
from typing import Optional

from olo.adapters import (
    AdapterConfigError,
    FetchBatch,
    MonitorAdapter,
    SourceError,
)
from olo.signals import NormalizedSignal

_MAX_EMPTY_PAGES = 20
_MAX_SUMMARY = 1024
_MAX_LOCATION = 1024
_MAX_ATTENDEES = 100
_MAX_RECURRENCE = 20
_SIGNAL_SUBJECT_MAX = 256
_MAX_CALENDARS = 50   # calendarList cap; warn if exceeded

# Composite cursor schema version
_CURSOR_V2 = 2

# ── I7: alias pattern ─────────────────────────────────────────────────────────
_ALIAS_RE = re.compile(r"^[a-z0-9]{1,16}$")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rfc3339_to_dt(raw: str) -> datetime:
    """Parse an RFC3339 string to a timezone-aware UTC datetime."""
    s = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _event_external_id(event_id: str, updated: str) -> str:
    """§6.3: sha256(event_id)[:40] + "_" + updated, capped at 256 chars."""
    id_hash = hashlib.sha256(event_id.encode()).hexdigest()[:40]
    ext = f"ev_{id_hash}_{updated}"
    return ext[:256]


def _truncate(s: Optional[str], n: int) -> Optional[str]:
    if s is None:
        return None
    collapsed = re.sub(r"\s+", " ", s).strip()
    return collapsed[:n] if collapsed else None


def _event_to_signal(
    event: dict, source: str, fetch_time: datetime, calendar_id: str
) -> NormalizedSignal:
    """Map a Calendar event dict to a NormalizedSignal with attached content blob.

    §6.3 (M2b): produces one calendar.event.changed or calendar.event.cancelled
    signal with:
      - Metadata payload: event_id, calendar_id, status, summary, location,
        start, end, attendees, recurrence  (description excluded per §6.3)
      - signal.blobs = (ContentBlob,)  — full event document including description
      - payload["content_refs"] = [{sha256, media_type, size, role="event_doc"}]

    One signal per event — no separate calendar.event.content signal is emitted.
    This keeps limit=1 valid and places the blob on the signal it belongs to.
    """
    from olo.adapters.gcal_full import build_event_blob, event_content_ref

    event_id = event.get("id", "")
    status = event.get("status", "confirmed")
    summary_raw = event.get("summary")
    updated_raw = event.get("updated", "")

    # occurred_at
    if updated_raw:
        try:
            occurred_at = _rfc3339_to_dt(updated_raw)
        except Exception:
            occurred_at = fetch_time
    else:
        occurred_at = fetch_time

    # external_id — include calendar_id hash so events on different calendars
    # don't collide even if Google reuses an event id across calendars.
    cal_hash = hashlib.sha256(calendar_id.encode()).hexdigest()[:8]
    base_ext = _event_external_id(event_id, updated_raw or fetch_time.isoformat())
    external_id = f"{base_ext}_{cal_hash}"[:256]

    # kind
    kind = "calendar.event.cancelled" if status == "cancelled" else "calendar.event.changed"

    # subject
    subject: Optional[str] = None
    if summary_raw:
        subject = _truncate(summary_raw, _SIGNAL_SUBJECT_MAX)

    # Content blob (includes description; must NOT appear in payload below)
    try:
        blob = build_event_blob(event, calendar_id)
        blobs: tuple = (blob,)
        content_refs_entry = [event_content_ref(blob)]
    except Exception:
        # If blob construction fails, emit metadata signal without blob
        blob = None
        blobs = ()
        content_refs_entry = []

    # Metadata payload (§6.3: description excluded; lives in the blob only)
    organizer = event.get("organizer", {})
    attendees_raw = event.get("attendees", [])[:_MAX_ATTENDEES]
    recurrence_raw = event.get("recurrence", [])[:_MAX_RECURRENCE]

    start = event.get("start", {})
    end = event.get("end", {})
    start_val = start.get("dateTime") or start.get("date") or ""
    end_val = end.get("dateTime") or end.get("date") or ""

    payload: dict = {
        "event_id": event_id,
        "calendar_id": calendar_id,
        "status": status,
        "summary": _truncate(summary_raw, _MAX_SUMMARY),
        "location": _truncate(event.get("location"), _MAX_LOCATION),
        "start": start_val,
        "end": end_val,
        "created": event.get("created", ""),
        "updated": updated_raw,
        "organizer_email": organizer.get("email", ""),
        "attendees": [
            {
                "email": a.get("email", ""),
                "response_status": a.get("responseStatus", ""),
                "organizer": a.get("organizer", False),
                "self": a.get("self", False),
            }
            for a in attendees_raw
        ],
        "recurring_event_id": event.get("recurringEventId"),
        "recurrence": recurrence_raw,
        "html_link": None,  # §6.3: excluded
    }
    if content_refs_entry:
        payload["content_refs"] = content_refs_entry

    return NormalizedSignal(
        source=source,
        external_id=external_id,
        kind=kind,
        occurred_at=occurred_at,
        subject=subject,
        payload=payload,
        blobs=blobs,
    )


def _cal_list_external_id(cal_id: str, action: str) -> str:
    """External id for calendar.calendar.added / removed signals."""
    h = hashlib.sha256(cal_id.encode()).hexdigest()[:40]
    return f"cal_{action}_{h}"[:256]


def _calendar_to_signal(
    cal_item: dict,
    source: str,
    kind: str,
    fetch_time: datetime,
) -> NormalizedSignal:
    """Build a calendar.calendar.added / calendar.calendar.removed signal."""
    cal_id = cal_item.get("id", "")
    summary = _truncate(cal_item.get("summary"), _SIGNAL_SUBJECT_MAX)
    action = "added" if kind == "calendar.calendar.added" else "removed"
    return NormalizedSignal(
        source=source,
        external_id=_cal_list_external_id(cal_id, action),
        kind=kind,
        occurred_at=fetch_time,
        subject=summary,
        payload={
            "calendar_id": cal_id,
            "summary": _truncate(cal_item.get("summary"), _MAX_SUMMARY),
            "access_role": cal_item.get("accessRole", ""),
            "primary": bool(cal_item.get("primary", False)),
            "time_zone": cal_item.get("timeZone", ""),
            "color_id": cal_item.get("colorId", ""),
        },
    )


# ── Cursor helpers ────────────────────────────────────────────────────────────

def _migrate_v1_cursor(cursor: str) -> dict:
    """Migrate a M2 v1 cursor ({"phase":...}) to composite v2.

    Maps:
      {"phase":"full",...}       → v2 with cals={"primary":{"phase":"full",...}}
      {"phase":"incr",...}       → v2 with cals={"primary":{"phase":"incr",...}}
    list cursor is bootstrapped (None → calendarList full scan next tick).

    The literal key "primary" is a temporary placeholder; it will be replaced
    with the real email-based calendar ID when _fetch_calendar_list runs its
    full scan.  _fetch_calendar_list deduplicates on that scan by detecting
    this sentinel and removing it once the real ID is registered.
    """
    try:
        old = json.loads(cursor)
    except Exception:
        old = {}
    return {
        "v": _CURSOR_V2,
        "list": None,          # calendarList full scan on next fetch
        "cals": {"primary": old},
    }


def _is_v1_cursor(cursor_dict: dict) -> bool:
    """True if the dict looks like a v1 (pre-M2b) per-calendar cursor."""
    return "phase" in cursor_dict and "v" not in cursor_dict


# ── GcalAdapter ───────────────────────────────────────────────────────────────

class GcalAdapter(MonitorAdapter):
    """Read-only Google Calendar adapter (all readable calendars, incremental sync).

    Options: account=<alias> (required), lookback_days (0-365, default 7).
    source = "gcal_<alias>" (instance attribute, V7).

    Composite cursor schema (v2)::

        {
          "v": 2,
          "list": null | {"sync_token": "...", "page_token": null},
          "cals": {
            "<calendarId>": {"phase": "full|incr", "sync_token": "...", ...},
            ...
          }
        }

    v1 cursors ({"phase":...}) are automatically migrated.
    """

    source: str = "gcal_unknown"

    def __init__(
        self, options: Mapping[str, str], *, client: Optional["GoogleClient"] = None
    ) -> None:
        alias = options.get("account", "").strip()
        if not alias:
            raise AdapterConfigError("gcal adapter requires option account=<alias>")

        # I7: enforce alias pattern
        if not _ALIAS_RE.match(alias):
            raise AdapterConfigError(
                f"gcal adapter: invalid account alias {alias!r}; "
                f"must match ^[a-z0-9]{{1,16}}$"
            )

        raw_lb = options.get("lookback_days", "7")
        try:
            lookback = int(raw_lb)
        except (ValueError, TypeError) as exc:
            raise AdapterConfigError(
                f"gcal option lookback_days must be an integer, got {raw_lb!r}"
            ) from exc
        # B4: expanded to 0–365
        if not (0 <= lookback <= 365):
            raise AdapterConfigError(
                f"gcal option lookback_days must be 0–365, got {lookback}"
            )

        # P1-E: optional calendars= filter — comma-separated calendar IDs
        raw_cals = options.get("calendars", "").strip()
        if raw_cals:
            cal_filter: Optional[frozenset[str]] = frozenset(
                c.strip() for c in raw_cals.split(",") if c.strip()
            )
        else:
            cal_filter = None   # None = all calendars

        extra = set(options) - {"account", "lookback_days", "calendars"}
        if extra:
            raise AdapterConfigError(
                f"gcal adapter does not accept options: {sorted(extra)}"
            )

        self._alias = alias
        self._lookback_days = lookback
        self._cal_filter = cal_filter  # frozenset of allowed IDs, or None for all
        self.source = f"gcal_{alias}"
        self._client: Optional["GoogleClient"] = client

    def _get_client(self) -> "GoogleClient":
        if self._client is None:
            from olo.google.credentials import load_credentials
            from olo.google.http import GoogleClient
            creds = load_credentials(self._alias)
            if not creds.has_calendar_scope():
                raise AdapterConfigError(
                    f"account {self._alias!r} has no calendar scope; "
                    f"run: hermes olo google connect --account {self._alias}"
                )
            self._client = GoogleClient(creds)
        return self._client

    # ── §6.2 sync algorithm ───────────────────────────────────────────────────

    def fetch(self, cursor: str | None, *, limit: int) -> FetchBatch:
        """Return signals from at most one signal-bearing page per call.

        B4: cursor is a JSON composite v2 dict; v1 cursors are migrated.
        Each fetch() does at most one of:
          (a) one page of calendarList.list (emits calendar.calendar.* signals), or
          (b) one page of events for one calendar (emits calendar.event.* signals).

        Never returns empty with has_more=True.
        """
        if limit < 1:
            raise AdapterConfigError(f"fetch limit must be ≥ 1, got {limit}")

        client = self._get_client()
        fetch_time = datetime.now(UTC)

        if cursor is None:
            # Fresh bootstrap: v2 cursor, calendarList sync first
            comp = {
                "v": _CURSOR_V2,
                "list": None,
                "cals": {},
            }
        else:
            try:
                comp = json.loads(cursor)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SourceError(f"gcal: invalid cursor {cursor!r}: {exc}") from exc
            # v1 migration
            if _is_v1_cursor(comp):
                comp = _migrate_v1_cursor(cursor)

        return self._fetch_next(client, comp, limit, fetch_time)

    def _fetch_next(
        self,
        client: "GoogleClient",
        comp: dict,
        limit: int,
        fetch_time: datetime,
    ) -> FetchBatch:
        """Decide which sub-fetch to do next and do it.

        Priority:
        1. If list cursor needs syncing (None or has page_token): calendarList bootstrap
           or mid-pagination.
        2. If any calendar has pending work (page_token or phase=full): do that calendar.
        3. Steady-state: calendarList incremental (discovers changes), then update comp
           and proceed to events for one calendar — selected by round-robin via the
           cursor-embedded `next_cal` index (P0-A: prevents secondary calendar starvation).
        4. No calendars registered — return empty.

        Step 3 combines calendarList + events in one fetch() call so the cron runner
        doesn't need an extra tick just for calendarList housekeeping.
        """
        list_state = comp.get("list")
        cals_state: dict = comp.get("cals") or {}

        # 1. calendarList bootstrap or mid-pagination.
        # After calendarList completes, immediately proceed to events if no signals
        # (avoids returning has_more=True with empty signals, which the runner rejects).
        if list_state is None or (isinstance(list_state, dict) and list_state.get("page_token")):
            cal_list_result = self._fetch_calendar_list(client, comp, limit, fetch_time)
            if cal_list_result.signals:
                return cal_list_result
            if not cal_list_result.has_more:
                return cal_list_result
            # No signals but has_more=True (calendars registered, events pending).
            new_comp = json.loads(cal_list_result.next_cursor)
            new_cals = new_comp.get("cals") or {}
            for cal_id, cal_cursor in new_cals.items():
                if isinstance(cal_cursor, dict):
                    if cal_cursor.get("page_token") or cal_cursor.get("phase") == "full":
                        result = self._fetch_calendar_events(
                            client, new_comp, cal_id, cal_cursor, limit, fetch_time
                        )
                        if result.signals or not result.has_more:
                            return result
                        # Empty + still pending → update comp and continue cascade
                        new_comp = json.loads(result.next_cursor)
                        new_cals = new_comp.get("cals") or {}
            for cal_id, cal_cursor in new_cals.items():
                if isinstance(cal_cursor, dict) and cal_cursor.get("phase") == "incr":
                    result = self._fetch_calendar_events(
                        client, new_comp, cal_id, cal_cursor, limit, fetch_time
                    )
                    if result.signals or not result.has_more:
                        return result
                    new_comp = json.loads(result.next_cursor)
                    new_cals = new_comp.get("cals") or {}
            # All cascaded calendars exhausted with no signals
            return FetchBatch(
                signals=(),
                next_cursor=json.dumps(new_comp),
                has_more=self._any_pending(new_comp, exclude_cal=None),
            )

        # 2. Any calendar with pending work (non-incr cursor, or has_more page_token)
        for cal_id, cal_cursor in cals_state.items():
            if isinstance(cal_cursor, dict):
                if cal_cursor.get("page_token"):
                    return self._fetch_calendar_events(
                        client, comp, cal_id, cal_cursor, limit, fetch_time
                    )
                if cal_cursor.get("phase") == "full":
                    return self._fetch_calendar_events(
                        client, comp, cal_id, cal_cursor, limit, fetch_time
                    )

        # 3. Steady-state: calendarList incremental first, then pick the NEXT calendar
        #    by round-robin to prevent starvation of secondary calendars (P0-A).
        if isinstance(list_state, dict) and list_state.get("sync_token"):
            cal_list_result = self._fetch_calendar_list(client, comp, limit, fetch_time)
            new_comp = json.loads(cal_list_result.next_cursor)

            if cal_list_result.signals:
                return cal_list_result

            # Round-robin calendar selection: use next_cal index stored in cursor
            new_cals = new_comp.get("cals") or {}
            incr_cal_ids = sorted(
                cal_id for cal_id, c in new_cals.items()
                if isinstance(c, dict) and c.get("phase") == "incr"
            )

            if not incr_cal_ids:
                # First handle newly registered cals needing full sync
                for cal_id, cal_cursor in new_cals.items():
                    if isinstance(cal_cursor, dict):
                        if cal_cursor.get("page_token") or cal_cursor.get("phase") == "full":
                            return self._fetch_calendar_events(
                                client, new_comp, cal_id, cal_cursor, limit, fetch_time
                            )
                return cal_list_result

            # Advance the round-robin index
            current_idx = comp.get("next_cal", 0)
            next_idx = (current_idx + 1) % len(incr_cal_ids)
            chosen_id = incr_cal_ids[current_idx % len(incr_cal_ids)]
            chosen_cursor = new_cals[chosen_id]
            new_comp["next_cal"] = next_idx

            return self._fetch_calendar_events(
                client, new_comp, chosen_id, chosen_cursor, limit, fetch_time
            )

        # 4. No calendars registered
        return FetchBatch(signals=(), next_cursor=json.dumps(comp), has_more=False)

    # ── calendarList sync ─────────────────────────────────────────────────────

    def _fetch_calendar_list(
        self,
        client: "GoogleClient",
        comp: dict,
        limit: int,
        fetch_time: datetime,
    ) -> FetchBatch:
        """One page of calendarList.list; emits calendar.calendar.* signals.

        On 410 (sync token expired), restarts calendarList full sync.
        """
        from olo.google.http import CalendarSyncTokenExpired

        list_state = comp.get("list")
        sync_token: Optional[str] = None
        page_token: Optional[str] = None

        if isinstance(list_state, dict):
            sync_token = list_state.get("sync_token")
            page_token = list_state.get("page_token")

        try:
            resp = self._do_calendar_list_request(client, sync_token, page_token)
        except CalendarSyncTokenExpired:
            # 410: restart calendarList full sync
            resp = self._do_calendar_list_request(client, None, None)
            sync_token = None

        items = resp.get("items") or []
        next_page_token = resp.get("nextPageToken")
        next_sync_token = resp.get("nextSyncToken")

        # Build signals for new/removed calendars
        # Compare discovered calendar IDs to existing cals_state
        old_cal_ids: set[str] = set((comp.get("cals") or {}).keys())
        new_cal_ids: set[str] = {item.get("id", "") for item in items if item.get("id")}

        signals: list[NormalizedSignal] = []

        if sync_token is not None:
            # Incremental calendarList update: items are changes only
            for item in items:
                cal_id = item.get("id", "")
                if not cal_id:
                    continue
                deleted = item.get("deleted", False)
                if deleted:
                    if cal_id in old_cal_ids:
                        signals.append(_calendar_to_signal(
                            item, self.source, "calendar.calendar.removed", fetch_time
                        ))
                else:
                    if cal_id not in old_cal_ids:
                        signals.append(_calendar_to_signal(
                            item, self.source, "calendar.calendar.added", fetch_time
                        ))
        # On full sync (sync_token was None) we don't emit added signals for
        # the initial discovery; we just register the calendars.

        # Update cals_state with any new calendars (initialize their event cursor)
        new_comp_cals: dict = dict(comp.get("cals") or {})
        time_min = (fetch_time - timedelta(days=self._lookback_days)).isoformat()

        if sync_token is None:
            # Full calendarList sync: register all returned non-deleted calendars.
            # P0-B: skip items with deleted=True (deleted calendars in full scan).
            # P1-D: if the cursor has a migrated "primary" sentinel AND this scan
            #   returns the owner's real email-based ID, absorb the sentinel's incr
            #   cursor under the real ID and remove the "primary" placeholder to
            #   prevent duplicate tracking.
            primary_sentinel = new_comp_cals.pop("primary", None)  # remove sentinel
            for item in items:
                cal_id = item.get("id", "")
                if not cal_id:
                    continue
                # P0-B: skip deleted calendars on full scan
                if item.get("deleted", False):
                    continue
                # P1-E: apply calendars= filter
                if self._cal_filter is not None and cal_id not in self._cal_filter:
                    continue
                if cal_id not in new_comp_cals:
                    # P1-D: if the real ID matches primary (owner's calendar), absorb
                    #   the migrated incremental cursor to avoid re-doing a full scan.
                    if (primary_sentinel is not None
                            and isinstance(primary_sentinel, dict)
                            and primary_sentinel.get("phase") == "incr"):
                        # Promote the migrated cursor under the real ID
                        new_comp_cals[cal_id] = primary_sentinel
                        primary_sentinel = None   # consumed — only use once
                    else:
                        new_comp_cals[cal_id] = {
                            "phase": "full",
                            "time_min": time_min,
                            "page_token": None,
                        }
            # If primary_sentinel was not consumed (account has no matching real ID
            # e.g. during a fake server test where "primary" IS a real ID), re-add it
            # so it isn't lost — but only when "primary" is not already in new_comp_cals.
            if primary_sentinel is not None and "primary" not in new_comp_cals:
                new_comp_cals["primary"] = primary_sentinel
        else:
            # Incremental: add new calendars, remove deleted ones
            for item in items:
                cal_id = item.get("id", "")
                if not cal_id:
                    continue
                deleted = item.get("deleted", False)
                if deleted:
                    new_comp_cals.pop(cal_id, None)
                else:
                    # P1-E: apply calendars= filter
                    if self._cal_filter is not None and cal_id not in self._cal_filter:
                        continue
                    if cal_id not in new_comp_cals:
                        new_comp_cals[cal_id] = {
                            "phase": "full",
                            "time_min": time_min,
                            "page_token": None,
                        }

        # Update list cursor
        if next_sync_token:
            new_list = {"sync_token": next_sync_token, "page_token": None}
        elif next_page_token:
            new_list = {"sync_token": sync_token, "page_token": next_page_token}
        else:
            new_list = {"sync_token": sync_token, "page_token": None}

        new_comp = {
            "v": _CURSOR_V2,
            "list": new_list,
            "cals": new_comp_cals,
        }

        # has_more: True if there are more calendarList pages or calendar events pending
        has_more = bool(next_page_token) or any(
            isinstance(c, dict) and (c.get("phase") == "full" or c.get("page_token"))
            for c in new_comp_cals.values()
        )

        if signals:
            # Cap to limit
            signals = signals[:limit]
            return FetchBatch(
                signals=tuple(signals),
                next_cursor=json.dumps(new_comp),
                has_more=has_more,
            )

        # No calendar signals: has_more still tells runner to come back
        return FetchBatch(
            signals=(),
            next_cursor=json.dumps(new_comp),
            has_more=has_more,
        )

    def _do_calendar_list_request(
        self,
        client: "GoogleClient",
        sync_token: Optional[str],
        page_token: Optional[str],
    ) -> dict:
        """Call calendarList.list with sync or page token.

        Per Google API docs: when syncToken is present, only syncToken (and
        optionally pageToken) may be sent — all other filters are forbidden.
        """
        if sync_token:
            # Incremental: syncToken only (+ optional pageToken)
            params: list[tuple[str, str]] = [("syncToken", sync_token)]
            if page_token:
                params.append(("pageToken", page_token))
        else:
            # Full sync: filters allowed
            params = [
                ("maxResults", "250"),
                ("showDeleted", "true"),
                ("showHidden", "false"),
                ("minAccessRole", "reader"),
            ]
            if page_token:
                params.append(("pageToken", page_token))
        return client.get_json("calendar", "/users/me/calendarList", params)

    # ── Per-calendar events sync ──────────────────────────────────────────────

    def _fetch_calendar_events(
        self,
        client: "GoogleClient",
        comp: dict,
        cal_id: str,
        cal_cursor: dict,
        limit: int,
        fetch_time: datetime,
    ) -> FetchBatch:
        """One signal-bearing page of events for one calendar.

        Each event produces exactly 1 signal (calendar.event.changed or
        calendar.event.cancelled) with the full document blob attached via
        content_refs. limit=1 is therefore valid.

        On 410 (sync token expired), self-heals: restarts full sync for that
        calendar and continues (does not propagate the 410).
        """
        from olo.google.http import CalendarSyncTokenExpired, CalendarPageTokenInvalid

        empty_page_count = 0
        current_cal_cursor = dict(cal_cursor)

        while True:
            try:
                resp = self._do_request(client, current_cal_cursor, limit, cal_id)
            except CalendarSyncTokenExpired:
                # §6 self-heal: restart full sync for this calendar only
                time_min = (fetch_time - timedelta(days=self._lookback_days)).isoformat()
                current_cal_cursor = {
                    "phase": "full",
                    "time_min": time_min,
                    "page_token": None,
                }
                resp = self._do_request(client, current_cal_cursor, limit, cal_id)
            except CalendarPageTokenInvalid:
                # §6.2 step 4: retry once without the page token
                current_cal_cursor = dict(current_cal_cursor)
                current_cal_cursor["page_token"] = None
                resp = self._do_request(client, current_cal_cursor, limit, cal_id)

            events = resp.get("items", [])
            next_page_token = resp.get("nextPageToken")
            next_sync_token = resp.get("nextSyncToken")

            # C1 fix: reject over-sized pages
            if len(events) > limit:
                raise SourceError(
                    f"gcal page exceeds requested maxResults ({len(events)} > {limit})"
                )

            if events:
                signals = tuple(
                    _event_to_signal(ev, self.source, fetch_time, cal_id)
                    for ev in events
                )
                new_comp = self._update_comp(
                    comp, cal_id, next_sync_token, next_page_token, current_cal_cursor
                )
                # has_more: more pages for this calendar, or other calendars pending
                has_more = bool(next_page_token) or self._any_pending(
                    new_comp, exclude_cal=None
                )
                return FetchBatch(
                    signals=signals,
                    next_cursor=json.dumps(new_comp),
                    has_more=has_more,
                )

            # Empty page
            empty_page_count += 1

            if next_sync_token:
                new_comp = self._update_comp(
                    comp, cal_id, next_sync_token, None, current_cal_cursor
                )
                has_more = self._any_pending(new_comp, exclude_cal=None)
                return FetchBatch(
                    signals=(),
                    next_cursor=json.dumps(new_comp),
                    has_more=has_more,
                )

            if next_page_token:
                if empty_page_count >= _MAX_EMPTY_PAGES:
                    new_comp = self._update_comp(
                        comp, cal_id, None, next_page_token, current_cal_cursor
                    )
                    return FetchBatch(
                        signals=(),
                        next_cursor=json.dumps(new_comp),
                        has_more=False,
                    )
                current_cal_cursor = dict(current_cal_cursor)
                current_cal_cursor["page_token"] = next_page_token
                continue

            # Neither token — end of data for this calendar
            new_comp = self._update_comp(comp, cal_id, None, None, current_cal_cursor)
            has_more = self._any_pending(new_comp, exclude_cal=None)
            return FetchBatch(
                signals=(),
                next_cursor=json.dumps(new_comp),
                has_more=has_more,
            )

    def _update_comp(
        self,
        comp: dict,
        cal_id: str,
        next_sync_token: Optional[str],
        next_page_token: Optional[str],
        current_cal_cursor: dict,
    ) -> dict:
        """Return a new composite cursor with the updated state for cal_id.

        Preserves next_cal (round-robin index) from the input comp.
        """
        new_comp = {
            "v": _CURSOR_V2,
            "list": comp.get("list"),
            "cals": dict(comp.get("cals") or {}),
        }
        # P0-A: preserve round-robin index
        if "next_cal" in comp:
            new_comp["next_cal"] = comp["next_cal"]
        if next_sync_token:
            new_comp["cals"][cal_id] = {
                "phase": "incr",
                "sync_token": next_sync_token,
                "page_token": None,
            }
        elif next_page_token:
            new_comp["cals"][cal_id] = dict(current_cal_cursor)
            new_comp["cals"][cal_id]["page_token"] = next_page_token
        else:
            # No token — no change, stay in current phase without page_token
            new_comp["cals"][cal_id] = dict(current_cal_cursor)
            new_comp["cals"][cal_id].pop("page_token", None)
        return new_comp

    def _any_pending(self, comp: dict, exclude_cal: Optional[str]) -> bool:
        """True if any calendar has pending work (phase=full or page_token set)."""
        for cal_id, cal_cursor in (comp.get("cals") or {}).items():
            if exclude_cal and cal_id == exclude_cal:
                continue
            if isinstance(cal_cursor, dict):
                if cal_cursor.get("page_token"):
                    return True
                if cal_cursor.get("phase") == "full":
                    return True
        return False

    def _do_request(
        self,
        client: "GoogleClient",
        phase: dict,
        limit: int,
        cal_id: str = "primary",
    ) -> dict:
        """Build params and call events.list for the given calendar.

        One signal per event (metadata + blob attached via content_refs),
        so maxResults = limit directly.
        """
        params: list[tuple[str, str]] = [
            ("singleEvents", "false"),
            ("showDeleted", "true"),
            ("maxResults", str(min(limit, 2500))),
        ]
        if phase.get("phase") == "full":
            params.append(("timeMin", phase["time_min"]))
        elif phase.get("phase") == "incr":
            params.append(("syncToken", phase["sync_token"]))
        if phase.get("page_token"):
            params.append(("pageToken", phase["page_token"]))

        # Build the correct path for the calendar
        if cal_id == "primary":
            path = "/calendars/primary/events"
        else:
            import urllib.parse
            encoded = urllib.parse.quote(cal_id, safe="")
            path = f"/calendars/{encoded}/events"

        return client.get_json("calendar", path, params)
