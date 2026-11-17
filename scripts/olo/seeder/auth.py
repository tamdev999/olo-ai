"""Seeder OAuth: obtain write-scoped tokens for the throwaway test account.

Completely separate from Olo's monitoring tokens. Stored at
/root/olo_seeder_token.json (mode 600, never committed).

Scopes needed:
  gmail.modify  — import/insert/modify messages and labels
  calendar      — create/modify/delete events
"""
from __future__ import annotations

import json
import os
import sys

TOKEN_PATH = "/root/olo_seeder_token.json"
CLIENT_SECRET_PATH = "/root/olo_client_secret.json"

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


def get_credentials():
    """Return valid google.oauth2.credentials.Credentials, refreshing if needed."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
        return creds

    # Need fresh authorization
    creds = _authorize()
    _save(creds)
    return creds


def _save(creds) -> None:
    import stat
    data = {
        "token":         creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri":     creds.token_uri,
        "client_id":     creds.client_id,
        "client_secret": creds.client_secret,
        "scopes":        creds.scopes,
    }
    with open(TOKEN_PATH, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(TOKEN_PATH, stat.S_IRUSR | stat.S_IWUSR)
    print(f"Seeder token saved: {TOKEN_PATH}")


def _authorize():
    """PKCE loopback + --manual fallback."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)

    print("\n=== Seeder OAuth authorization ===")
    print("Scopes:", SCOPES)
    print()

    # Try loopback first (port 0 = OS chooses)
    try:
        creds = flow.run_local_server(port=0, open_browser=False,
                                      prompt="consent",
                                      access_type="offline")
        return creds
    except Exception:
        pass

    # Manual fallback
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="false",
    )
    print(f"Open this URL in your browser:\n  {auth_url}")
    print("\nAfter authorizing, paste the full redirect URL here:")
    redirect = input("  Redirect URL: ").strip()
    flow.fetch_token(authorization_response=redirect)
    return flow.credentials


if __name__ == "__main__":
    creds = get_credentials()
    print(f"Authorized. Token valid: {creds.valid}")
    print(f"Scopes: {creds.scopes}")
