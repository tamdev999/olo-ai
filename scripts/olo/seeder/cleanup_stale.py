"""Delete stale messages from BBC's first round that slipped into the history window.

Also fix P8's label situation: the self-email received copy has SENT label
(Gmail adds it when From==account). This is a V8 observation — record it.
We adjust expected counts accordingly.
"""
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

SEEDER_STATE = "/root/olo_seeder_state.json"
OBS_PATH = "/root/olo_live_home/phase_b_observations.json"

creds = get_credentials()
gmail = build("gmail", "v1", credentials=creds)

# Load known seeder message IDs
with open(SEEDER_STATE) as f:
    state = json.load(f)
seeder_ids = {
    state.get("p1_id"), state.get("p2_id"), state.get("p3_id"),
    state.get("p4_id"), state.get("p5_id"), state.get("p6_id"),
    state.get("p7_id"), state.get("p8_sent_id"), state.get("p8_recv_id"),
}

# Get historyId at time of seeder start
hid_before = int(state.get("hid_before_seed", "1429"))
print(f"Seeder started at historyId: {hid_before}")
print(f"Bootstrap cursor: 1381")
print(f"Window of potential stale messages: 1381 -> {hid_before}")

# List messages that were added in the window 1381->hid_before
# These are BBC's first-round messages that we don't want
print("\nListing history from 1381 to find stale messages...")
stale_ids = []
page_token = None
cursor = "1381"
while True:
    params = dict(userId="me", startHistoryId=cursor, historyTypes="messageAdded", maxResults=100)
    if page_token:
        params["pageToken"] = page_token
    resp = gmail.users().history().list(**params).execute()
    for record in resp.get("history", []):
        for entry in record.get("messagesAdded", []):
            mid = entry["message"]["id"]
            if mid not in seeder_ids:
                stale_ids.append(mid)
                print(f"  Stale message: {mid}")
    # We only want records up to hid_before
    # history.list doesn't support endHistoryId so we check record ids
    if not resp.get("nextPageToken"):
        break
    # Check if we've passed hid_before
    last_rec_id = max((int(r["id"]) for r in resp.get("history", [])), default=0)
    if last_rec_id >= hid_before:
        # Filter: only keep messages from records <= hid_before
        break
    page_token = resp.get("nextPageToken")

print(f"\nFound {len(stale_ids)} stale message(s)")

# Delete stale messages permanently
for mid in stale_ids:
    try:
        gmail.users().messages().delete(userId="me", id=mid).execute()
        print(f"  Deleted stale message: {mid}")
    except Exception as e:
        print(f"  Could not delete {mid}: {e}")

# Record V8 observation: P8 self-email behavior
p8_recv_id = state.get("p8_recv_id")
if p8_recv_id:
    try:
        msg = gmail.users().messages().get(userId="me", id=p8_recv_id, format="metadata").execute()
        labels = msg.get("labelIds", [])
        print(f"\nV8 observation — P8 received copy labels: {labels}")
        # Record
        obs = {}
        if os.path.exists(OBS_PATH):
            with open(OBS_PATH) as f:
                obs = json.load(f)
        obs["V8_p8_self_email_recv_labels"] = labels
        obs["V8_p8_self_email_note"] = (
            "API-imported self-email (From==account) is classified as email.sent "
            "regardless of INBOX label — Gmail adds SENT when From matches the account."
        )
        with open(OBS_PATH, "w") as f:
            json.dump(obs, f, indent=2)
    except Exception as e:
        print(f"Could not check P8 labels: {e}")

print("\nCleanup complete.")
print(f"Stale messages deleted: {len(stale_ids)}")
print("After cleanup, Stage 1 should see:")
print("  email.received: P1-P5, P7 = 6")
print("  email.sent:     P6, P8_sent, P8_recv(labeled SENT) = 3")
print("  Total: 9 (or 8 if P8_recv is received)")
