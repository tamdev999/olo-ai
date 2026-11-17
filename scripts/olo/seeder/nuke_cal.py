"""Delete ALL OLO test events from the calendar (all statuses)."""
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

SEEDER_STATE = "/root/olo_seeder_state.json"

creds = get_credentials()
cal = build("calendar", "v3", credentials=creds)

# Delete from all known state files
known_ids = set()
if os.path.exists(SEEDER_STATE):
    with open(SEEDER_STATE) as f:
        s = json.load(f)
    for k in ("e1_id", "e2_id", "e3_id", "e4_id"):
        if s.get(k):
            known_ids.add(s[k])

# Also search events list for OLO marker
page_token = None
found = []
while True:
    kwargs = dict(calendarId="primary", q="OLO", maxResults=100, showDeleted=True)
    if page_token:
        kwargs["pageToken"] = page_token
    resp = cal.events().list(**kwargs).execute()
    for ev in resp.get("items", []):
        summary = ev.get("summary", "")
        if "OLO-E" in summary or "OLO-BODY-MARKER" in ev.get("description", ""):
            found.append((ev["id"], ev.get("status"), summary))
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

print(f"Found {len(found)} OLO events:")
for eid, status, summary in found:
    print(f"  {eid}  status={status}  {summary}")

deleted = 0
for eid, status, summary in found:
    if status == "cancelled":
        print(f"  Already cancelled (skip delete): {eid}")
        continue
    try:
        cal.events().delete(calendarId="primary", eventId=eid).execute()
        print(f"  Deleted: {eid}  {summary}")
        deleted += 1
    except Exception as e:
        print(f"  Error deleting {eid}: {e}")

print(f"\nDeleted {deleted} events")

# Reset Olo calendar
os.environ["HERMES_HOME"] = "/root/olo_live_home"
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
from olo.store import connect, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
n = conn.execute("SELECT COUNT(*) n FROM signals WHERE source='gcal_livetest'").fetchone()["n"]
conn.execute("DELETE FROM signals WHERE source='gcal_livetest'")
conn.execute("DELETE FROM sync_checkpoints WHERE source='gcal_livetest'")
conn.commit()
conn.close()
print(f"Cleared {n} Olo calendar signals + checkpoint")

# Clear seeder cal flag
with open(SEEDER_STATE) as f:
    state = json.load(f)
state.pop("cal_seeder_done", None)
state.pop("e1_id", None); state.pop("e2_id", None)
state.pop("e3_id", None); state.pop("e4_id", None)
with open(SEEDER_STATE, "w") as f:
    json.dump(state, f, indent=2)
print("Calendar seeder flag cleared — ready to re-seed")
