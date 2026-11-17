"""Calendar seeder: create E1-E4 test events via Calendar API.

v2: no self-cleanup during seeder. Caller (or a separate cleanup script)
handles teardown. This prevents cancellation events from contaminating the
test measurement window.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, UTC


MARKER = "OLO-BODY-MARKER-SEEDER-STAGE1"


def _next_day(tz=UTC) -> datetime:
    return datetime.now(tz).replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def run_seeder(service, state_path: str) -> dict:
    if os.path.exists(state_path):
        with open(state_path) as f:
            s = json.load(f)
        if s.get("cal_seeder_done"):
            print("Calendar seeder already ran; returning existing state.")
            return s

    print(f"\n=== Calendar Seeder v2 ===")
    print(f"Marker: {MARKER}\n")

    base = _next_day()

    def create(summary: str, start: datetime, end: datetime,
                recurrence: list | None = None) -> dict:
        body = {
            "summary":     summary,
            "description": f"Olo Phase B test. {MARKER}",
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end":   {"dateTime": _iso(end),   "timeZone": "UTC"},
        }
        if recurrence:
            body["recurrence"] = recurrence
        return service.events().insert(calendarId="primary", body=body).execute()

    print("E1 (single, to be renamed in Round 2)...")
    e1 = create("[OLO-E1] Single event A", base, base + timedelta(hours=1))
    print(f"  E1: {e1['id']}")

    print("E2 (single)...")
    e2 = create("[OLO-E2] Single event B",
                base + timedelta(hours=2), base + timedelta(hours=3))
    print(f"  E2: {e2['id']}")

    print("E3 (single, to be cancelled in Round 2)...")
    e3 = create("[OLO-E3] Single event C",
                base + timedelta(hours=4), base + timedelta(hours=5))
    print(f"  E3: {e3['id']}")

    print("E4 (weekly recurring)...")
    e4 = create("[OLO-E4] Weekly recurring",
                base + timedelta(hours=6), base + timedelta(hours=7),
                recurrence=["RRULE:FREQ=WEEKLY;COUNT=4"])
    print(f"  E4: {e4['id']}")

    state: dict = {}
    if os.path.exists(state_path):
        with open(state_path) as f:
            state = json.load(f)
    state.update({
        "e1_id": e1["id"], "e1_summary": e1["summary"],
        "e2_id": e2["id"],
        "e3_id": e3["id"],
        "e4_id": e4["id"],
        "cal_seeder_done": True,
    })
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)
    print(f"Calendar seeder state saved: {state_path}")
    return state
