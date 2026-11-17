"""Olo AI — Google OAuth 2.0 with PKCE (desktop/loopback flow).

§3.4, §3.6, §4.1 (C2 fix): ALL HTTP through open_credentialed_url.
No side effects at import time.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Optional

from olo.google.credentials import (
    GMAIL_METADATA_SCOPE,
    CALENDAR_EVENTS_OWNED_RO,
    REQUIRED_SCOPES,
    GoogleAccountCredentials,
    GoogleAuthRevoked,
    credential_lock,
    load_credentials,
    save_credentials,
    email_hash as _email_hash,
)

# ── Google endpoints ───────────────────────────────────────────────────────────
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

_LOOPBACK_TIMEOUT = 300.0   # seconds


# ── C2 fix: safe HTTP helper ──────────────────────────────────────────────────

def _safe_open(req: urllib.request.Request, *, timeout: float) -> "http.client.HTTPResponse":
    """C2 fix: all Olo HTTP goes through open_credentialed_url (§4.1).

    This prevents Authorization headers from leaking on redirects.
    """
    from hermes_cli.urllib_security import open_credentialed_url
    return open_credentialed_url(req, timeout=timeout)


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _random_state() -> str:
    return secrets.token_urlsafe(24)


# ── Auth URL ──────────────────────────────────────────────────────────────────

@dataclass
class PendingOAuth:
    """In-memory PKCE state; never persisted."""
    state: str
    code_verifier: str
    redirect_uri: str
    scopes: frozenset[str]


def build_auth_url(
    client_id: str,
    redirect_uri: str,
    scopes: frozenset[str],
    *,
    api_base_override: Optional[str] = None,
) -> tuple[str, PendingOAuth]:
    state = _random_state()
    verifier, challenge = _pkce_pair()
    params = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect_uri),
        ("scope", " ".join(sorted(scopes))),
        ("state", state),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("access_type", "offline"),
        ("prompt", "consent"),
    ]
    if api_base_override:
        auth_base = api_base_override.rstrip("/") + "/o/oauth2/v2/auth"
    else:
        auth_base = _AUTH_URL
    url = auth_base + "?" + urllib.parse.urlencode(params)
    return url, PendingOAuth(
        state=state,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        scopes=scopes,
    )


# ── Loopback code capture ─────────────────────────────────────────────────────

class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    result: Optional[dict] = None
    _done = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        type(self).result = params
        type(self)._done.set()
        body = b"<html><body><p>You can close this tab.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_) -> None:
        pass


def _capture_loopback(port: int, timeout: float = _LOOPBACK_TIMEOUT) -> Optional[dict]:
    _OAuthCallbackHandler.result = None
    _OAuthCallbackHandler._done.clear()
    server = http.server.HTTPServer(("127.0.0.1", port), _OAuthCallbackHandler)
    server.timeout = 1.0
    deadline = __import__("time").monotonic() + timeout
    while not _OAuthCallbackHandler._done.is_set():
        if __import__("time").monotonic() >= deadline:
            server.server_close()
            return None
        server.handle_request()
    server.server_close()
    return _OAuthCallbackHandler.result


def _parse_redirect_url(url: str) -> dict:
    parsed = urllib.parse.urlparse(url.strip())
    return dict(urllib.parse.parse_qsl(parsed.query))


# ── Token exchange and refresh ────────────────────────────────────────────────

def _post_token(
    params: dict,
    *,
    api_base_override: Optional[str] = None,
) -> dict:
    """POST to the token endpoint via open_credentialed_url (C2 fix)."""
    if api_base_override:
        url = api_base_override.rstrip("/") + "/token"
    else:
        url = _TOKEN_URL
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _safe_open(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        try:
            err = json.loads(raw.decode())
        except Exception:
            err = {}
        return {"error": err.get("error", "unknown"), "status": exc.code, "_raw": err}


def _get_profile(
    access_token: str,
    *,
    api_base_override: Optional[str] = None,
) -> str:
    """Return the account email address from Gmail profile.

    C2 fix: uses open_credentialed_url so Authorization is NOT forwarded on redirect.
    C2 fix: maps errors per §4.2 instead of swallowing all exceptions.
    Raises SourceError on any failure so exchange_code() aborts + revokes cleanly.
    """
    from olo.adapters import SourceError

    if api_base_override:
        url = api_base_override.rstrip("/") + "/gmail/v1/users/me/profile"
    else:
        url = _PROFILE_URL
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with _safe_open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            email = data.get("emailAddress", "")
            if not email:
                raise SourceError("gmail profile returned no emailAddress")
            return email
    except urllib.error.HTTPError as exc:
        raise SourceError(
            f"gmail profile fetch failed with HTTP {exc.code}"
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise SourceError(
            f"gmail profile fetch network error: {type(exc).__name__}"
        ) from exc


def _validate_scopes(granted_raw: str, requested: frozenset[str]) -> frozenset[str]:
    granted = frozenset(s for s in granted_raw.split() if s)
    extra = granted - requested
    if extra:
        raise ValueError(
            f"Google granted extra scopes not requested: {sorted(extra)}. "
            f"Revoke and re-authorise without those scopes."
        )
    return granted


def exchange_code(
    code: str,
    pending: PendingOAuth,
    client_id: str,
    client_secret: str,
    alias: str,
    *,
    api_base_override: Optional[str] = None,
    oauth_publishing_status: str = "testing",
) -> GoogleAccountCredentials:
    """Exchange an authorisation code for tokens; return GoogleAccountCredentials.

    C2 fix: _get_profile failures abort connect and revoke the new token.
    Does NOT save to disk — caller must call save_credentials() under the lock.
    """
    resp = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": pending.code_verifier,
    }, api_base_override=api_base_override)

    if "error" in resp:
        raise ValueError(f"token exchange failed: {resp['error']}")

    access_token = resp.get("access_token", "")
    refresh_token = resp.get("refresh_token", "")
    scope_raw = resp.get("scope", "")

    if not refresh_token:
        raise ValueError("token exchange returned no refresh_token")

    # Validate scopes — refuse any extras
    try:
        granted = _validate_scopes(scope_raw, pending.scopes)
    except ValueError as exc:
        if access_token:
            try:
                revoke_token(access_token, api_base_override=api_base_override)
            except Exception:
                pass
        raise exc

    # Fetch email address if Gmail scope was granted (C2: errors abort + revoke)
    email = ""
    if GMAIL_METADATA_SCOPE in granted and access_token:
        try:
            email = _get_profile(access_token, api_base_override=api_base_override)
        except Exception as exc:
            # Abort connect: revoke the new token before raising
            try:
                revoke_token(access_token, api_base_override=api_base_override)
            except Exception:
                pass
            raise ValueError(f"gmail profile fetch failed during connect: {exc}") from exc

    return GoogleAccountCredentials(
        alias=alias,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        granted_scopes=granted,
        email_address=email,
        refresh_token_issued_at=datetime.now(UTC),
        oauth_publishing_status=oauth_publishing_status,
        api_base_override=api_base_override,
    )


def refresh_access_token(alias: str) -> str:
    """Return a fresh access token for *alias*.

    §4.3 + Minor fix: assert replaced with explicit SourceError check.
    """
    from olo.adapters import SourceError as _SE

    with credential_lock(alias):
        creds = load_credentials(alias)
        api_base = creds.api_base_override

        resp = _post_token({
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
        }, api_base_override=api_base)

        if "error" in resp:
            err_code = resp["error"]
            if err_code == "invalid_grant":
                msg = (
                    f"google auth revoked or expired for account {alias!r}: "
                    f"run hermes olo google connect --account {alias}"
                )
                if creds.oauth_publishing_status == "testing":
                    msg += " (Testing-mode tokens expire 7 days after consent)"
                raise GoogleAuthRevoked(msg)
            raise GoogleAuthRevoked(
                f"google token refresh failed for {alias!r}: {err_code}"
            )

        access_token = resp.get("access_token", "")
        new_refresh = resp.get("refresh_token")

        if new_refresh and new_refresh != creds.refresh_token:
            # Minor fix: replace assert with explicit check (removed by -O)
            current_on_disk = load_credentials(alias).refresh_token
            if current_on_disk != creds.refresh_token:
                raise _SE(
                    "refresh_token changed under lock — concurrent write detected"
                )
            updated = GoogleAccountCredentials(
                alias=creds.alias,
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                refresh_token=new_refresh,
                granted_scopes=creds.granted_scopes,
                email_address=creds.email_address,
                refresh_token_issued_at=datetime.now(UTC),
                oauth_publishing_status=creds.oauth_publishing_status,
                api_base_override=creds.api_base_override,
            )
            save_credentials(updated)

        return access_token


def revoke_token(
    token: str,
    *,
    api_base_override: Optional[str] = None,
) -> None:
    """POST revoke via open_credentialed_url (C2 fix; best effort)."""
    if api_base_override:
        url = api_base_override.rstrip("/") + "/revoke"
    else:
        url = _REVOKE_URL
    body = urllib.parse.urlencode({"token": token}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _safe_open(req, timeout=10):
            pass
    except Exception:
        pass


# ── High-level connect flow ───────────────────────────────────────────────────

def connect_loopback(
    client_id: str,
    client_secret: str,
    alias: str,
    scopes: frozenset[str],
    *,
    api_base_override: Optional[str] = None,
    oauth_publishing_status: str = "testing",
) -> tuple[str, PendingOAuth, int]:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    redirect_uri = f"http://127.0.0.1:{port}/"
    url, pending = build_auth_url(
        client_id, redirect_uri, scopes,
        api_base_override=api_base_override,
    )
    return url, pending, port


def await_loopback_code(
    pending: PendingOAuth,
    port: int,
    timeout: float = _LOOPBACK_TIMEOUT,
) -> Optional[str]:
    params = _capture_loopback(port, timeout=timeout)
    if params is None:
        return None
    if params.get("error") == "access_denied":
        return None
    if params.get("state") != pending.state:
        return None
    return params.get("code")


def parse_manual_redirect(url_str: str, pending: PendingOAuth) -> Optional[str]:
    params = _parse_redirect_url(url_str)
    if params.get("error") == "access_denied":
        return None
    if params.get("state") != pending.state:
        return None
    return params.get("code")
