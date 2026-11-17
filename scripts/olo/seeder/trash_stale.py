"""Trash the stale message so it's excluded from INBOX history."""
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build

creds = get_credentials()
gmail = build("gmail", "v1", credentials=creds)

STALE_ID = "1a1e88cb844fbe8f"

try:
    # Trash it — removes INBOX label, adds TRASH label
    result = gmail.users().messages().trash(userId="me", id=STALE_ID).execute()
    print(f"Trashed stale message {STALE_ID}: labels now = {result.get('labelIds', [])}")
except Exception as e:
    print(f"Could not trash {STALE_ID}: {e}")

# Check current historyId
profile = gmail.users().getProfile(userId="me").execute()
print(f"\nCurrent historyId: {profile['historyId']}")
