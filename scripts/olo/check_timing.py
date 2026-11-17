#!/usr/bin/env python3
"""Diagnose why Stage 1 found 0 signals."""
import sys, os, json
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"

from olo.store import connect, get_checkpoint, migrate
from olo.paths import db_path

conn = connect(db_path())
migrate(conn)
chk_gmail = get_checkpoint("gmail_livetest", conn)
chk_gcal  = get_checkpoint("gcal_livetest", conn)
conn.close()

print(f"Gmail checkpoint (historyId): {chk_gmail!r}")
print(f"Gcal checkpoint: {chk_gcal!r}")

# Now check what history.list returns for startHistoryId=chk_gmail
from olo.google.credentials import load_credentials
from olo.google.http import GoogleClient

creds = load_credentials("livetest")
client = GoogleClient(creds)

print(f"\nQuerying history.list from historyId={chk_gmail}...")
params = [
    ("startHistoryId", chk_gmail),
    ("historyTypes", "messageAdded"),
    ("historyTypes", "messageDeleted"),
    ("historyTypes", "labelAdded"),
    ("historyTypes", "labelRemoved"),
    ("maxResults", "10"),
]
try:
    resp = client.get_json("gmail", "/users/me/history", params)
    print(f"historyId in response: {resp.get('historyId')}")
    records = resp.get("history", [])
    print(f"Records returned: {len(records)}")
    for r in records:
        print(f"  record id={r.get('id')} keys={list(r.keys())}")
    if not records:
        print("No history since checkpoint — messages pre-date the bootstrap.")
except Exception as e:
    print(f"Error: {e}")

# Also check profile historyId
print("\nQuerying profile for current mailbox historyId...")
try:
    profile = client.get_json("gmail", "/users/me/profile", [])
    current_hid = profile.get("historyId")
    print(f"Current mailbox historyId: {current_hid}")
    print(f"Bootstrap checkpoint:      {chk_gmail}")
    if current_hid and chk_gmail:
        diff = int(current_hid) - int(chk_gmail)
        print(f"Difference: {diff} history records")
        if diff == 0:
            print("→ Mailbox unchanged since bootstrap (messages existed before bootstrap)")
        else:
            print(f"→ {diff} records added since bootstrap")
except Exception as e:
    print(f"Error: {e}")
