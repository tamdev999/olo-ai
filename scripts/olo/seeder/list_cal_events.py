"""List all OLO events on the calendar."""
import sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

creds = get_credentials()
cal = build("calendar", "v3", credentials=creds)

page_token = None
all_events = []
while True:
    # List ALL events including deleted ones to see the full picture
    kwargs = dict(calendarId="primary", maxResults=100, showDeleted=True, showHiddenInvitations=True)
    if page_token:
        kwargs["pageToken"] = page_token
    resp = cal.events().list(**kwargs).execute()
    for ev in resp.get("items", []):
        summary = ev.get("summary", "")
        if "OLO" in summary:
            all_events.append({
                "id": ev["id"],
                "summary": summary,
                "status": ev.get("status"),
                "updated": ev.get("updated", "")[:19],
            })
    page_token = resp.get("nextPageToken")
    if not page_token:
        break

print(f"Found {len(all_events)} OLO events:")
for ev in sorted(all_events, key=lambda e: e["updated"]):
    print(f"  {ev['id'][:20]}  status={ev['status']:<12}  {ev['summary']}")
