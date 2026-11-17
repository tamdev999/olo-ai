"""Delete the first batch of calendar events (from before the gmail re-bootstrap)."""
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

# First-batch event IDs (from the initial seeder run)
FIRST_BATCH = [
    "ousgv70g3hkmk1v1hj7tr3rhc4",  # E1
    "43viir91u72dt9dlcedgn9uv54",  # E2
    "o1tmb6ilulardhph45r4fj0nvs",  # E3
    "omrc6v2ijq2rjogq2m6ftfkl74",  # E4
]

creds = get_credentials()
cal = build("calendar", "v3", credentials=creds)

for eid in FIRST_BATCH:
    try:
        cal.events().delete(calendarId="primary", eventId=eid).execute()
        print(f"Deleted event {eid}")
    except Exception as e:
        print(f"Could not delete {eid}: {e}")

# Also reset Olo signals + calendar checkpoint
os.environ["HERMES_HOME"] = "/root/olo_live_home"
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
from olo.store import connect, migrate
from olo.paths import db_path

conn = connect(db_path()); migrate(conn)
n_gcal = conn.execute("SELECT COUNT(*) n FROM signals WHERE source='gcal_livetest'").fetchone()["n"]
conn.execute("DELETE FROM signals WHERE source='gcal_livetest'")
n_gmail = conn.execute("SELECT COUNT(*) n FROM signals WHERE source='gmail_livetest'").fetchone()["n"]
conn.execute("DELETE FROM signals WHERE source='gmail_livetest'")
conn.commit()
conn.close()
print(f"\nDeleted {n_gcal} gcal signals, {n_gmail} gmail signals from Olo DB")
print("Run rebootstrap.sh again to get fresh checkpoints from current state")
