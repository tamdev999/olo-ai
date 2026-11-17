"""Delete the stale confirmed calendar event set (dcb5uspo etc.)."""
import json, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

STALE_IDS = [
    "dcb5uspo1f320j2rj2d2uq63p8",
    "v56ldd7o8e249b7ti1pbe4g23c",
    "fhetu0eh81skssvt5d69hqhd9s",
    "osm4clkp7uee6atqkh9r6m6s8o",
]

creds = get_credentials()
cal = build("calendar", "v3", credentials=creds)
for eid in STALE_IDS:
    try:
        cal.events().delete(calendarId="primary", eventId=eid).execute()
        print(f"Deleted: {eid}")
    except Exception as e:
        print(f"Error {eid}: {e}")
print("Done")
