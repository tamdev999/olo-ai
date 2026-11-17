"""Olo AI — Google credential/OAuth/HTTP facade.

No side effects at import time.
"""
from __future__ import annotations

from olo.google.credentials import (
    GoogleAccountCredentials,
    GoogleAuthRevoked,
    CredentialPermissionError,
    load_credentials,
    save_credentials,
    remove_credentials,
    list_accounts,
    credential_path,
    lock_path,
)
from olo.google.http import (
    GoogleClient,
    GoogleRateLimited,
    GmailHistoryExpired,
    CalendarSyncTokenExpired,
    CalendarPageTokenInvalid,
)
from olo.google.oauth import (
    build_auth_url,
    exchange_code,
    refresh_access_token,
    revoke_token,
)

__all__ = [
    "CalendarPageTokenInvalid",
    "CalendarSyncTokenExpired",
    "CredentialPermissionError",
    "GmailHistoryExpired",
    # credentials
    "GoogleAccountCredentials",
    "GoogleAuthRevoked",
    # http
    "GoogleClient",
    "GoogleRateLimited",
    # oauth
    "build_auth_url",
    "credential_path",
    "exchange_code",
    "list_accounts",
    "load_credentials",
    "lock_path",
    "refresh_access_token",
    "remove_credentials",
    "revoke_token",
    "save_credentials",
]
