"""Round 2 mutations: archive P1, mark P2 read, star P3, trash P4, rename E1, cancel E3."""
from __future__ import annotations

import json
import os
import sys

REPO        = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
SEEDER_DIR  = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder"
HERMES_HOME = "/root/olo_live_home"
STATE_FILE  = "/root/olo_seeder_state.json"
OBS_PATH    = f"{HERMES_HOME}/phase_b_observations.json"

sys.path.insert(0, REPO)
sys.path.insert(0, SEEDER_DIR)
os.environ["HERMES_HOME"] = HERMES_HOME

from auth import get_credentials
from googleapiclient.discovery import build

creds = get_credentials()
gmail_svc = build("gmail", "v1", credentials=creds)
cal_svc   = build("calendar", "v3", credentials=creds)

with open(STATE_FILE) as f:
    state = json.load(f)

print("=== Round 2 Mutations ===")

# ── Gmail mutations ───────────────────────────────────────────────────────────

def modify(mid: str, add: list[str], remove: list[str]) -> dict:
    return gmail_svc.users().messages().modify(
        userId="me", id=mid,
        body={"addLabelIds": add, "removeLabelIds": remove},
    ).execute()

# P1: archive (remove INBOX)
print("P1: archive (remove INBOX)...")
r = modify(state["p1_id"], [], ["INBOX"])
state["r2_p1_labels_after"] = r.get("labelIds", [])
print(f"  labels after: {state['r2_p1_labels_after']}")

# P2: mark read (remove UNREAD)
print("P2: mark read (remove UNREAD)...")
r = modify(state["p2_id"], [], ["UNREAD"])
state["r2_p2_labels_after"] = r.get("labelIds", [])
print(f"  labels after: {state['r2_p2_labels_after']}")

# P3: star (add STARRED)
print("P3: star (add STARRED)...")
r = modify(state["p3_id"], ["STARRED"], [])
state["r2_p3_labels_after"] = r.get("labelIds", [])
print(f"  labels after: {state['r2_p3_labels_after']}")

# P4: trash (adds TRASH, removes INBOX)
print("P4: trash...")
r = gmail_svc.users().messages().trash(userId="me", id=state["p4_id"]).execute()
state["r2_p4_labels_after"] = r.get("labelIds", [])
print(f"  labels after: {state['r2_p4_labels_after']}")
# Record V8: does the deletion stub carry labelIds?
state["r2_p4_trashed_id"] = state["p4_id"]

# V8 observation: does Gmail include labelIds on the trashed message's
# deletion record when it's permanently deleted? We'll see in the signals.
# For now, record the trash step.

# ── Calendar mutations ────────────────────────────────────────────────────────

# E1: rename (patch summary)
print("E1: rename...")
e1_new_summary = "[OLO-E1] Single event A — RENAMED"
r = cal_svc.events().patch(
    calendarId="primary",
    eventId=state["e1_id"],
    body={"summary": e1_new_summary},
).execute()
state["r2_e1_new_summary"] = r.get("summary")
state["r2_e1_new_updated"] = r.get("updated", "")
print(f"  new summary: {r.get('summary')}")

# E3: cancel (delete event)
print("E3: cancel (delete)...")
cal_svc.events().delete(calendarId="primary", eventId=state["e3_id"]).execute()
print(f"  E3 deleted: {state['e3_id']}")

# Save state
state["r2_done"] = True
with open(STATE_FILE, "w") as f:
    json.dump(state, f, indent=2)

# Record V8 observation
obs = {}
if os.path.exists(OBS_PATH):
    with open(OBS_PATH) as f:
        obs = json.load(f)
obs["V8_p4_trash_labels"] = state["r2_p4_labels_after"]
obs["V8_note_p4_trash"] = "P4 trashed: labelIds after trash recorded above"
with open(OBS_PATH, "w") as f:
    json.dump(obs, f, indent=2)

print("\n=== Mutations complete ===")
print(f"P1 archived: INBOX removed ✓")
print(f"P2 read: UNREAD removed ✓")
print(f"P3 starred: STARRED added ✓")
print(f"P4 trashed: now in TRASH ✓")
print(f"E1 renamed: {state['r2_e1_new_summary']!r} ✓")
print(f"E3 cancelled: {state['e3_id']} ✓")
