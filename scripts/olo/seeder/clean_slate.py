"""Full clean slate: delete all OLO test data, reset Olo DB, then reseed.

Order matters:
1. Delete all OLO Gmail messages (via labels modify / trash)
2. Delete all OLO Calendar events
3. Reset Olo checkpoints + signals via DB
4. Bootstrap both adapters (cursor = current state, AFTER deletes)
5. Seed fresh Gmail P1-P8 and Calendar E1-E4
6. Verify historyId advanced

The bootstrap after cleanup ensures Olo's cursor is PAST all the
cancellation/deletion history records produced by step 1-2.
Stage 1 then runs incrementally from that cursor and sees only the
newly seeded records.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REPO        = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
SEEDER_DIR  = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder"
HERMES_HOME = "/root/olo_live_home"
HERMES_EXE  = "/root/olo_linux_venv/bin/hermes"
PYTHON      = "/root/olo_linux_venv/bin/python3"
STATE_FILE  = "/root/olo_seeder_state.json"
PHASE_STATE = f"{HERMES_HOME}/phase_b_state.json"

sys.path.insert(0, REPO)
sys.path.insert(0, SEEDER_DIR)
os.environ["HERMES_HOME"] = HERMES_HOME

from auth import get_credentials
from googleapiclient.discovery import build

creds = get_credentials()
gmail_svc = build("gmail", "v1", credentials=creds)
cal_svc   = build("calendar", "v3", credentials=creds)

profile = gmail_svc.users().getProfile(userId="me").execute()
test_email = profile["emailAddress"]
print(f"Account: {test_email}")


# ── Step 1: Delete OLO Gmail messages ────────────────────────────────────────
print("\n=== Step 1: Delete OLO Gmail messages ===")

def _list_olo_messages():
    msgs = []
    page_token = None
    while True:
        kwargs = dict(userId="me", q="subject:[OLO-LIVE", maxResults=100)
        if page_token:
            kwargs["pageToken"] = page_token
        resp = gmail_svc.users().messages().list(**kwargs).execute()
        msgs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return msgs

olo_msgs = _list_olo_messages()
print(f"Found {len(olo_msgs)} OLO Gmail messages")
for m in olo_msgs:
    try:
        gmail_svc.users().messages().trash(userId="me", id=m["id"]).execute()
        print(f"  Trashed: {m['id']}")
    except Exception as e:
        print(f"  Error trashing {m['id']}: {e}")


# ── Step 2: Delete OLO Calendar events ───────────────────────────────────────
print("\n=== Step 2: Delete OLO Calendar events ===")

page_token = None
cal_events = []
while True:
    kwargs = dict(calendarId="primary", q="OLO-E", maxResults=100, showDeleted=False)
    if page_token:
        kwargs["pageToken"] = page_token
    resp = cal_svc.events().list(**kwargs).execute()
    cal_events.extend(resp.get("items", []))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

print(f"Found {len(cal_events)} OLO Calendar events")
for ev in cal_events:
    if ev.get("status") == "cancelled":
        continue
    try:
        cal_svc.events().delete(calendarId="primary", eventId=ev["id"]).execute()
        print(f"  Deleted: {ev['id']}  {ev.get('summary','')}")
    except Exception as e:
        print(f"  Error deleting {ev['id']}: {e}")


# ── Step 3: Reset Olo DB ──────────────────────────────────────────────────────
print("\n=== Step 3: Reset Olo DB ===")
from olo.store import connect, migrate
from olo.paths import db_path

conn = connect(db_path()); migrate(conn)
for src in ("gmail_livetest", "gcal_livetest"):
    n = conn.execute("SELECT COUNT(*) n FROM signals WHERE source=?", (src,)).fetchone()["n"]
    conn.execute("DELETE FROM signals WHERE source=?", (src,))
    conn.execute("DELETE FROM sync_checkpoints WHERE source=?", (src,))
    print(f"  {src}: deleted {n} signals + checkpoint")
conn.commit()
conn.close()


# ── Step 4: Clear seeder state ────────────────────────────────────────────────
print("\n=== Step 4: Clear seeder state ===")
if os.path.exists(STATE_FILE):
    os.remove(STATE_FILE)
    print(f"  Removed: {STATE_FILE}")


# ── Step 5: Bootstrap both adapters ──────────────────────────────────────────
# This establishes cursors AFTER all the delete/trash history records.
# Any history from cleanup (cancellations, label changes) is now before the cursor.
print("\n=== Step 5: Bootstrap (cursors past cleanup history) ===")
env = {**os.environ, "HERMES_HOME": HERMES_HOME,
       "PATH": "/root/olo_linux_venv/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")}

with open(PHASE_STATE) as f:
    phase = json.load(f)
gmail_job = phase["gmail_job_id"]
gcal_job  = phase["gcal_job_id"]

for name, jid in [("gmail", gmail_job), ("gcal", gcal_job)]:
    r = subprocess.run([HERMES_EXE, "cron", "run", jid],
                       capture_output=True, text=True, env=env)
    print(f"  {name}: rc={r.returncode}  {r.stdout.strip()!r}")
    assert r.returncode == 0, f"{name} bootstrap failed: {r.stderr}"
    assert "Ran now: succeeded." in r.stdout

# Record bootstrap cursors
from olo.store import get_checkpoint
conn = connect(db_path()); migrate(conn)
gmail_cursor = get_checkpoint("gmail_livetest", conn)
gcal_cursor  = get_checkpoint("gcal_livetest", conn)
conn.close()
print(f"  gmail cursor: {gmail_cursor!r}")
print(f"  gcal  cursor: {gcal_cursor!r}")


# ── Step 6: Seed fresh data ───────────────────────────────────────────────────
print("\n=== Step 6: Seed P1-P8 and E1-E4 (after bootstrap cursors) ===")

from seed_gmail    import run_seeder as seed_gmail
from seed_calendar import run_seeder as seed_calendar

gmail_state = seed_gmail(gmail_svc, test_email, STATE_FILE)
cal_state   = seed_calendar(cal_svc, STATE_FILE)


# ── Step 7: Verify historyId advanced ────────────────────────────────────────
print("\n=== Step 7: Verify ===")
final_hid = gmail_svc.users().getProfile(userId="me").execute()["historyId"]
gmail_cursor_int = int(gmail_cursor)
print(f"Gmail bootstrap cursor: {gmail_cursor_int}")
print(f"Final historyId:        {final_hid}")
assert int(final_hid) > gmail_cursor_int, (
    f"STOP: historyId {final_hid} did not advance past cursor {gmail_cursor_int}"
)
print(f"historyId advanced by {int(final_hid) - gmail_cursor_int} ✓")

# Threading check
p5t = gmail_state.get("p5_thread")
p6t = gmail_state.get("p6_thread")
p7t = gmail_state.get("p7_thread")
if p5t == p6t == p7t:
    print(f"P5/P6/P7 threading: all in {p5t} ✓")
else:
    print(f"P5/P6/P7 threading: P5={p5t} P6={p6t} P7={p7t}")
    if p6t != p5t or p7t != p5t:
        print("WARNING: P5/P6/P7 not all in same thread")

print(f"\nP8: single id={gmail_state.get('p8_id')}")

print("\n=== Clean slate complete — ready for Stage 1 ===")
print(f"Gmail cursor: {gmail_cursor}")
print(f"Calendar: {cal_state.get('e1_id')} / {cal_state.get('e2_id')} / {cal_state.get('e3_id')} / {cal_state.get('e4_id')}")
