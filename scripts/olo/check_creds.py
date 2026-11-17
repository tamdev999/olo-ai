#!/usr/bin/env python3
"""Verify Phase B credential file — permissions, scopes, email hash."""
import hashlib
import json
import os
import stat

CRED_FILE = "/root/olo_live_home/olo/credentials/google/livetest.json"
CRED_DIR  = "/root/olo_live_home/olo/credentials/google"

# Permissions
fm = oct(stat.S_IMODE(os.stat(CRED_FILE).st_mode))
dm = oct(stat.S_IMODE(os.stat(CRED_DIR).st_mode))
print(f"File mode: {fm}  (expected 0o600)")
print(f"Dir  mode: {dm}  (expected 0o700)")
assert fm == "0o600", f"STOP: file mode {fm} != 0o600"
assert dm == "0o700", f"STOP: dir mode  {dm} != 0o700"
print("Permissions: OK")

# Contents
with open(CRED_FILE) as f:
    d = json.load(f)

scopes = sorted(d.get("granted_scopes", []))
email  = d.get("email_address", "")
status = d.get("oauth_publishing_status", "")
issued = d.get("refresh_token_issued_at", "")
email_hash = hashlib.sha256(email.strip().lower().encode()).hexdigest()

print(f"\ngrantedScopes: {scopes}")
print(f"status: {status}")
print(f"issued: {issued}")
print(f"email_hash (sha256): {email_hash}")

EXPECTED = {
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/calendar.events.owned.readonly",
}
if set(scopes) == EXPECTED:
    print("\nSCOPES: exact match - OK")
elif set(scopes) > EXPECTED:
    print(f"\nSTOP: extra scopes granted: {set(scopes) - EXPECTED}")
    raise SystemExit(1)
else:
    print(f"\nSTOP: missing scopes: {EXPECTED - set(scopes)}")
    raise SystemExit(1)

# Access token must NOT be on disk
for key in ("access_token", "token"):
    if key in d:
        print(f"\nSTOP: access token found in credential file (key={key!r})")
        raise SystemExit(1)
print("Access token: not on disk - OK")
