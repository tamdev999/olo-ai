"""Olo AI — Google Calendar adapter (read-only, incremental sync).

§6: sync algorithm, signal mapping, §5.0 empty-page rule.
Registered as "gcal"; per-account source = "gcal_<alias>".

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

_EXCLUDED_FIELDS = frozenset({
    "description", "attachments", "conferenceData",
    "extendedProperties", "hangoutLink", "source",
    "gadget", "htmlLink",
})

# ── I7: alias pattern ─────────────────────────────────────────────────────────
import re as _re  # noqa: E402  (re already imported above)
_ALIAS_RE = _re.compile(r"^[a-z0-9]{1,16}$")


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


def _event_to_signal(event: dict, source: str, fetch_time: datetime) -> NormalizedSignal:
    """Map a Calendar event dict to a NormalizedSignal."""
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

    # external_id
    external_id = _event_external_id(event_id, updated_raw or fetch_time.isoformat())

    # kind
    kind = "calendar.event.cancelled" if status == "cancelled" else "calendar.event.changed"

    # subject
    subject: Optional[str] = None
    if summary_raw:
        subject = _truncate(summary_raw, _SIGNAL_SUBJECT_MAX)

    # payload (§6.3 — exclude forbidden fields)
    organizer = event.get("organizer", {})
    attendees_raw = event.get("attendees", [])[:_MAX_ATTENDEES]
    recurrence_raw = event.get("recurrence", [])[:_MAX_RECURRENCE]

    start = event.get("start", {})
    end = event.get("end", {})
    start_val = start.get("dateTime") or start.get("date") or ""
    end_val = end.get("dateTime") or end.get("date") or ""

    payload: dict = {
        "event_id": event_id,
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

    return NormalizedSignal(
        source=source,
        external_id=external_id,
        kind=kind,
        occurred_at=occurred_at,
        subject=subject,
        payload=payload,
    )


class GcalAdapter(MonitorAdapter):
    """Read-only Google Calendar adapter (primary calendar, incremental sync).

    Options: account=<alias> (required), lookback_days (0-30, default 7).
    source = "gcal_<alias>" (instance attribute, V7).
    """

    source: str = "gcal_unknown"

    def __init__(self, options: Mapping[str, str]) -> None:
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
        if not (0 <= lookback <= 30):
            raise AdapterConfigError(
                f"gcal option lookback_days must be 0–30, got {lookback}"
            )

        extra = set(options) - {"account", "lookback_days"}
        if extra:
            raise AdapterConfigError(
                f"gcal adapter does not accept options: {sorted(extra)}"
            )

        self._alias = alias
        self._lookback_days = lookback
        self.source = f"gcal_{alias}"
        self._client: Optional["GoogleClient"] = None

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
        """Return signals from at most ONE signal-bearing page per call.

        C1 fix: a single fetch() may cross any number of empty pages (§5.0),
        but returns immediately after the first page that yields signals.
        A page is NEVER truncated; Google is asked for at most min(limit, 2500)
        items so it cannot return more than limit events.
        """
        if limit < 1:
            raise AdapterConfigError(f"fetch limit must be ≥ 1, got {limit}")

        client = self._get_client()
        fetch_time = datetime.now(UTC)

        if cursor is None:
            time_min = (fetch_time - timedelta(days=self._lookback_days)).isoformat()
            phase = {"phase": "full", "time_min": time_min, "page_token": None}
        else:
            try:
                phase = json.loads(cursor)
            except (json.JSONDecodeError, ValueError) as exc:
                raise SourceError(f"gcal: invalid cursor {cursor!r}: {exc}") from exc

        return self._fetch_one_page(client, phase, limit, fetch_time)

    def _fetch_one_page(
        self,
        client: "GoogleClient",
        phase: dict,
        limit: int,
        fetch_time: datetime,
    ) -> FetchBatch:
        """C1 fix: advance over empty pages; return as soon as one page yields signals.

        Contract:
        - Never span two signal-bearing pages in one call.
        - Never truncate a page (SourceError if Google over-delivers).
        - Empty pages count toward _MAX_EMPTY_PAGES; if the cap is hit, return
          an advanced-cursor empty batch (has_more=False) so the runner commits
          the cursor progress and the next tick continues.
        """
        from olo.google.http import CalendarSyncTokenExpired, CalendarPageTokenInvalid

        empty_page_count = 0
        current_phase = dict(phase)

        while True:
            # ── request ──────────────────────────────────────────────────────
            try:
                resp = self._do_request(client, current_phase, limit)
            except CalendarSyncTokenExpired:
                raise
            except CalendarPageTokenInvalid:
                # §6.2 step 4: retry once without the page token
                current_phase = dict(current_phase)
                current_phase["page_token"] = None
                resp = self._do_request(client, current_phase, limit)

            events = resp.get("items", [])
            next_page_token = resp.get("nextPageToken")
            next_sync_token = resp.get("nextSyncToken")

            # ── C1 fix: reject over-sized pages ───────────────────────────────
            if len(events) > limit:
                raise SourceError(
                    f"gcal page exceeds requested maxResults ({len(events)} > {limit})"
                )

            # ── signal-bearing page ───────────────────────────────────────────
            if events:
                signals = tuple(
                    _event_to_signal(ev, self.source, fetch_time) for ev in events
                )
                # Determine cursor after this page
                if next_sync_token:
                    # Last page of this sync → transition to incr
                    next_cursor = json.dumps({
                        "phase": "incr",
                        "sync_token": next_sync_token,
                        "page_token": None,
                    })
                    return FetchBatch(signals=signals, next_cursor=next_cursor, has_more=False)
                elif next_page_token:
                    # More pages remain
                    next_cursor = json.dumps({
                        **current_phase,
                        "page_token": next_page_token,
                    })
                    return FetchBatch(signals=signals, next_cursor=next_cursor, has_more=True)
                else:
                    # No token at all — shouldn't happen; treat as end
                    next_cursor = json.dumps(current_phase)
                    return FetchBatch(signals=signals, next_cursor=next_cursor, has_more=False)

            # ── empty page ────────────────────────────────────────────────────
            empty_page_count += 1

            if next_sync_token:
                # Empty last page with sync token → cursor advances, done
                next_cursor = json.dumps({
                    "phase": "incr",
                    "sync_token": next_sync_token,
                    "page_token": None,
                })
                return FetchBatch(signals=(), next_cursor=next_cursor, has_more=False)

            if next_page_token:
                if empty_page_count >= _MAX_EMPTY_PAGES:
                    # §5.0 cap: return advanced page-token cursor, no signals
                    next_cursor = json.dumps({
                        **current_phase,
                        "page_token": next_page_token,
                    })
                    return FetchBatch(signals=(), next_cursor=next_cursor, has_more=False)
                # Follow the page token (counts toward cap)
                current_phase = dict(current_phase)
                current_phase["page_token"] = next_page_token
                continue

            # Neither token — treat as end of data
            next_cursor = json.dumps(current_phase)
            return FetchBatch(signals=(), next_cursor=next_cursor, has_more=False)

    def _do_request(self, client: "GoogleClient", phase: dict, limit: int) -> dict:
        """Build params and call events.list."""
        params: list[tuple[str, str]] = [
            ("calendarId", "primary"),
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

        return client.get_json("calendar", "/calendars/primary/events", params)
