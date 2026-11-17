"""Delete all calendar events except the latest seeder batch, reset Olo calendar signals."""
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

SEEDER_STATE = "/root/olo_seeder_state.json"

creds = get_credentials()
cal = build("calendar", "v3", credentials=creds)

# Keep only the latest batch (current state file)
with open(SEEDER_STATE) as f:
    state = json.load(f)
keep_ids = {
    state.get("e1_id"), state.get("e2_id"),
    state.get("e3_id"), state.get("e4_id"),
}
print(f"Keeping events: {keep_ids}")

# List all OLO-E events on the calendar
page_token = None
to_delete = []
while True:
    resp = cal.events().list(
        calendarId="primary", q="OLO-E",
        maxResults=50, singleEvents=True,
    ).execute()
    for ev in resp.get("items", []):
        if ev["id"] not in keep_ids:
            to_delete.append(ev["id"])
            print(f"  Will delete: {ev['id']}  summary={ev.get('summary','')}")
    if not resp.get("nextPageToken"):
        break
    page_token = resp["nextPageToken"]

for eid in to_delete:
    try:
        cal.events().delete(calendarId="primary", eventId=eid).execute()
        print(f"  Deleted: {eid}")
    except Exception as e:
        print(f"  Could not delete {eid}: {e}")

# Reset Olo calendar signals + checkpoint
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
print(f"\nDeleted {n} gcal signals and checkpoint from Olo DB")
print("Calendar will re-bootstrap cleanly on next run")
