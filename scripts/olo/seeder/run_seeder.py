"""Entry point: authorize, seed Gmail + Calendar, verify history advanced."""
from __future__ import annotations

import json
import os
import sys

REPO = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
sys.path.insert(0, REPO)
# Add seeder dir to path
sys.path.insert(0, os.path.dirname(__file__))

SEEDER_STATE = "/root/olo_seeder_state.json"


def main():
    # ── Authorize ─────────────────────────────────────────────────────────────
    from auth import get_credentials
    print("Obtaining seeder OAuth credentials...")
    creds = get_credentials()
    print(f"Token valid: {creds.valid}  Scopes: {list(creds.scopes or [])}")

    # ── Build service clients ──────────────────────────────────────────────────
    from googleapiclient.discovery import build

    gmail_service = build("gmail", "v1", credentials=creds)
    cal_service   = build("calendar", "v3", credentials=creds)

    # Get the test account email
    profile = gmail_service.users().getProfile(userId="me").execute()
    test_email = profile["emailAddress"]
    print(f"Test account: {test_email}")

    # Confirm this is not a personal/business account (simple guard)
    hid_now = profile["historyId"]
    print(f"Current historyId: {hid_now}")
    print(f"Olo bootstrap cursor: 1381")
    if int(hid_now) <= 1381:
        print("WARNING: historyId has not advanced past bootstrap cursor.")

    # ── Seed Gmail ─────────────────────────────────────────────────────────────
    from seed_gmail import run_seeder as seed_gmail
    gmail_state = seed_gmail(gmail_service, test_email, SEEDER_STATE)

    # ── Seed Calendar ──────────────────────────────────────────────────────────
    from seed_calendar import run_seeder as seed_calendar
    cal_state = seed_calendar(cal_service, SEEDER_STATE)

    # ── Final verification ─────────────────────────────────────────────────────
    final_hid = gmail_service.users().getProfile(userId="me").execute()["historyId"]
    print(f"\n=== Final historyId: {final_hid} (bootstrap cursor: 1381) ===")
    if int(final_hid) > 1381:
        print("historyId advanced past bootstrap cursor ✓")
    else:
        print("STOP: historyId still at 1381 — seeding may have failed")
        sys.exit(1)

    print("\n=== Seeder complete ===")
    print(f"Gmail messages: P1={gmail_state.get('p1_id')} P2={gmail_state.get('p2_id')} "
          f"P3={gmail_state.get('p3_id')} P4={gmail_state.get('p4_id')} "
          f"P5={gmail_state.get('p5_id')} P6={gmail_state.get('p6_id')} "
          f"P7={gmail_state.get('p7_id')} P8_sent={gmail_state.get('p8_sent_id')} "
          f"P8_recv={gmail_state.get('p8_recv_id')}")
    print(f"Calendar events: E1={cal_state.get('e1_id')} E2={cal_state.get('e2_id')} "
          f"E3={cal_state.get('e3_id')} E4={cal_state.get('e4_id')}")


if __name__ == "__main__":
    main()
