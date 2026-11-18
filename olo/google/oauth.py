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
    """Validate the scopes granted by Google.

    Returns the granted frozenset if it exactly equals *requested*.
    Raises ValueError if any scope outside *requested* was granted.

    Callers that care about FORBIDDEN_SCOPES must check separately and
    MUST NOT revoke on scope_union — revoking invalidates all tokens in
    the same project/user combination. The caller should discard the new
    tokens and keep the old file.
    """
    granted = frozenset(s for s in granted_raw.split() if s)
    extra = granted - requested
    if extra:
        raise ValueError(
            f"Google granted extra scopes not requested: {sorted(extra)}. "
            f"Run 'hermes olo google connect --upgrade --revoke-first' if you "
            f"need to clear the existing grant first."
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

    B2: If the granted scopes include any FORBIDDEN_SCOPES (e.g. gmail.metadata
    when requesting v2 scopes), the new tokens are discarded WITHOUT revoking.
    Revoking would invalidate the existing project grant. The caller must handle
    the ValueError by keeping the old credentials and instructing the user to
    rerun with --revoke-first if needed.

    Does NOT save to disk — caller must call save_credentials() under the lock.
    """
    from olo.google.credentials import FORBIDDEN_SCOPES, REQUIRED_SCOPES_V2, \
        SCOPE_PROFILE_FULL_V1, SCOPE_PROFILE_LEGACY, GMAIL_READONLY_SCOPE

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

    # B2: check for FORBIDDEN_SCOPES WITHOUT revoking (revoking kills the project grant)
    # This check only applies when requesting v2 scopes (REQUIRED_SCOPES_V2).
    # Legacy connect (gmail.metadata) is unaffected by this guard.
    granted_set = frozenset(s for s in scope_raw.split() if s)
    if pending.scopes == REQUIRED_SCOPES_V2:
        forbidden_in_grant = granted_set & FORBIDDEN_SCOPES
        if forbidden_in_grant:
            # Discard new tokens; do NOT revoke — that would also kill the old grant
            raise ValueError(
                f"scope_union: Google granted forbidden/incompatible scopes: "
                f"{sorted(forbidden_in_grant)}. "
                f"The new tokens have been discarded (not revoked). "
                f"Rerun with --revoke-first to clear the project grant first."
            )

    # Validate scopes — refuse any extras beyond what was requested
    try:
        granted = _validate_scopes(scope_raw, pending.scopes)
    except ValueError as exc:
        # Discard new tokens WITHOUT revoking (scope_union case)
        raise ValueError(
            f"scope_union: {exc}"
        ) from exc

    # Determine scope_profile from the granted set
    if granted == REQUIRED_SCOPES_V2:
        scope_profile = SCOPE_PROFILE_FULL_V1
    elif GMAIL_METADATA_SCOPE in granted:
        scope_profile = SCOPE_PROFILE_LEGACY
    else:
        scope_profile = None

    # Fetch email address if a Gmail scope was granted
    email = ""
    gmail_scopes = {GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE}
    if granted & gmail_scopes and access_token:
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
        scope_profile=scope_profile,
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
                scope_profile=creds.scope_profile,  # B2: preserve scope_profile
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


# ── B2: credential upgrade (§2.7) ────────────────────────────────────────────

class UpgradeResult:
    """Result returned by upgrade_credentials()."""
    __slots__ = ("success", "reason", "scope_union", "needs_revoke_first")

    def __init__(
        self,
        success: bool = False,
        reason: str = "",
        scope_union: bool = False,
        needs_revoke_first: bool = False,
    ) -> None:
        self.success = success
        self.reason = reason
        self.scope_union = scope_union
        self.needs_revoke_first = needs_revoke_first

    def __repr__(self) -> str:
        return (f"UpgradeResult(success={self.success}, reason={self.reason!r}, "
                f"scope_union={self.scope_union})")


def upgrade_credentials(
    alias: str,
    *,
    revoke_first: bool = False,
    connect_fn=None,
    api_base_override: Optional[str] = None,
    oauth_publishing_status: Optional[str] = None,
    _poll_invalid_grant_wait: Optional[float] = None,
    _poll_max_attempts: int = 60,
) -> UpgradeResult:
    """Upgrade stored credentials from M2 (gmail.metadata) to M2b (gmail.readonly + calendar).

    §2.7 default path (revoke_first=False):
    1. Take the account lock. Back up .pre-upgrade. Audit google.upgrade.started.
    2. Obtain new consent via connect_fn (default: loopback).
    3. If FORBIDDEN_SCOPES in grant (scope_union): discard WITHOUT revoking; return
       UpgradeResult(scope_union=True, needs_revoke_first=True).
    4. If scopes ≠ REQUIRED_SCOPES_V2: discard without revoking; audit + return failure.
    5. Verify: profile, messages.list + messages.get format=full, calendarList.list.
    6. Save new credentials with scope_profile='full_v1'. Drop .pre-upgrade.

    §2.7 --revoke-first path (revoke_first=True):
    1–2 as above, then:
    3. Revoke old token; rewrite file as reauth_required (no refresh_token).
    4. Poll old token for invalid_grant (bounded by _poll_invalid_grant_wait * _poll_max_attempts).
    5. Obtain new consent; verify; save.

    connect_fn: callable(scopes) → (code, api_base) or raises. Default uses loopback.
    _poll_invalid_grant_wait: seconds between poll attempts (injectable for tests).

    Returns UpgradeResult(success=True) on success; always preserves checkpoints.
    """
    import shutil
    import time as _time
    from olo.adapters import SourceError
    from olo.google.credentials import (
        REQUIRED_SCOPES_V2, FORBIDDEN_SCOPES,
        SCOPE_PROFILE_FULL_V1, GMAIL_READONLY_SCOPE,
        credential_path, credential_lock, load_credentials, save_credentials,
    )
    from olo.store import connect as _connect, migrate as _migrate, _audit as _audit_fn
    from olo.paths import db_path
    from olo.signals import format_utc

    poll_wait = _poll_invalid_grant_wait if _poll_invalid_grant_wait is not None else 5.0

    cred_path = credential_path(alias)
    pre_upgrade_path = cred_path.with_suffix(".json.pre-upgrade")

    with credential_lock(alias):
        # 1. Back up and audit
        try:
            existing = load_credentials(alias)
        except FileNotFoundError:
            return UpgradeResult(reason="no credentials to upgrade")

        import shutil as _shutil
        _shutil.copy2(str(cred_path), str(pre_upgrade_path))
        import os
        if os.name != "nt":
            os.chmod(str(pre_upgrade_path), 0o600)

        _db_conn = _connect(db_path())
        _migrate(_db_conn)
        _audit_fn(_db_conn, "cli:google", "google.upgrade.started", {
            "alias": alias,
            "current_profile": existing.scope_profile,
        }, subject_type="account", subject_id=alias)
        _db_conn.execute("COMMIT") if _db_conn.in_transaction else None

        # 1b. For revoke_first: revoke old token now, rewrite as reauth_required
        if revoke_first:
            revoke_token(existing.refresh_token, api_base_override=existing.api_base_override)
            # Rewrite credential file as reauth_required (no refresh_token)
            import dataclasses
            reauth_creds = dataclasses.replace(
                existing,
                refresh_token="",  # signals reauth_required state
                scope_profile="reauth_required",
            )
            save_credentials(reauth_creds)

            # Poll until invalid_grant
            poll_params = {
                "grant_type": "refresh_token",
                "refresh_token": existing.refresh_token,
                "client_id": existing.client_id,
                "client_secret": existing.client_secret,
            }
            confirmed = False
            for _ in range(_poll_max_attempts):
                try:
                    resp = _post_token(poll_params, api_base_override=existing.api_base_override)
                    if resp.get("error") == "invalid_grant":
                        confirmed = True
                        break
                except Exception:
                    pass
                _time.sleep(poll_wait)

            if not confirmed:
                _audit_fn(_db_conn, "cli:google", "google.upgrade.failed", {
                    "alias": alias, "reason": "revoke_propagation_timeout",
                })
                try: _db_conn.execute("COMMIT")
                except Exception: pass
                _db_conn.close()
                return UpgradeResult(
                    reason="revoke did not propagate within timeout; rerun --upgrade --revoke-first"
                )

        # 2. Obtain consent
        result_code: Optional[str] = None
        result_api_base: Optional[str] = api_base_override or existing.api_base_override

        if connect_fn is not None:
            try:
                result_code, result_api_base = connect_fn(REQUIRED_SCOPES_V2)
            except Exception as exc:
                _audit_fn(_db_conn, "cli:google", "google.upgrade.failed", {
                    "alias": alias, "reason": f"consent_error: {exc}",
                })
                try: _db_conn.execute("COMMIT")
                except Exception: pass
                _db_conn.close()
                # Restore pre-upgrade file if revoke_first left reauth_required
                if revoke_first and pre_upgrade_path.exists():
                    _shutil.copy2(str(pre_upgrade_path), str(cred_path))
                return UpgradeResult(reason=f"consent failed: {exc}")
        else:
            # Default: loopback
            url, pending, port = connect_loopback(
                existing.client_id, existing.client_secret, alias,
                REQUIRED_SCOPES_V2,
                api_base_override=result_api_base,
                oauth_publishing_status=oauth_publishing_status or existing.oauth_publishing_status,
            )
            print(f"Open this URL in your browser:\n  {url}")
            print(f"Waiting for redirect on http://127.0.0.1:{port}/ ({_LOOPBACK_TIMEOUT}s timeout)...")
            result_code = await_loopback_code(pending, port)
            if result_code is None:
                _audit_fn(_db_conn, "cli:google", "google.upgrade.failed", {
                    "alias": alias, "reason": "consent_timeout_or_denied",
                })
                try: _db_conn.execute("COMMIT")
                except Exception: pass
                _db_conn.close()
                return UpgradeResult(reason="consent timeout or access denied")

            # Build a PendingOAuth for exchange_code
            pending_obj = pending  # already set above

        # 3. Exchange code for tokens
        try:
            new_creds = exchange_code(
                result_code,
                pending_obj if connect_fn is None else PendingOAuth(
                    state="", code_verifier="", redirect_uri="",
                    scopes=REQUIRED_SCOPES_V2,
                ),
                existing.client_id,
                existing.client_secret,
                alias,
                api_base_override=result_api_base,
                oauth_publishing_status=oauth_publishing_status or existing.oauth_publishing_status,
            )
        except ValueError as exc:
            reason_str = str(exc)
            is_scope_union = "scope_union" in reason_str
            _audit_fn(_db_conn, "cli:google", "google.upgrade.failed", {
                "alias": alias, "reason": reason_str[:200],
            })
            try: _db_conn.execute("COMMIT")
            except Exception: pass
            _db_conn.close()
            return UpgradeResult(
                reason=reason_str,
                scope_union=is_scope_union,
                needs_revoke_first=is_scope_union and not revoke_first,
            )

        # 4. Verify before saving
        try:
            _verify_new_credentials(new_creds, result_api_base)
        except Exception as exc:
            _audit_fn(_db_conn, "cli:google", "google.upgrade.failed", {
                "alias": alias, "reason": f"verification_failed: {exc}",
            })
            try: _db_conn.execute("COMMIT")
            except Exception: pass
            _db_conn.close()
            # P1-G CORRECTED: Do NOT revoke the new token on verification failure.
            #
            # Revocation is project-wide: revoking the new access_token would also
            # invalidate the existing working refresh_token (they share a project
            # grant at Google).  The old credential file (pre_upgrade_path) is still
            # intact; no production access is interrupted.  The candidate tokens
            # are simply discarded (not saved) so they expire naturally (~1 hour).
            #
            # Opus test-infrastructure requirement: verification-failure tests must
            # assert that zero revoke requests were made.
            return UpgradeResult(reason=f"post-consent verification failed: {exc}")

        # 5. Save atomically
        save_credentials(new_creds)
        if pre_upgrade_path.exists():
            try: pre_upgrade_path.unlink()
            except OSError: pass

        _audit_fn(_db_conn, "cli:google", "google.upgrade.completed", {
            "alias": alias,
            "new_profile": new_creds.scope_profile,
            "new_scopes": sorted(new_creds.granted_scopes),
        })
        try: _db_conn.execute("COMMIT")
        except Exception: pass
        _db_conn.close()

        print(f"Connected account {alias!r} (scopes: {sorted(new_creds.granted_scopes)})")
        return UpgradeResult(success=True)


def _verify_new_credentials(
    creds: "GoogleAccountCredentials",
    api_base_override: Optional[str] = None,
) -> str:
    """Verify new credentials work before saving (§2.7 step 5).

    Runs: refresh, profile, messages.list + messages.get format=full,
    calendarList.list. Raises on any failure.

    Returns the access token so the caller can revoke it on post-verification
    failure (P1-G).
    """
    from olo.google.http import GoogleClient

    # Refresh once to confirm refresh_token works
    resp = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    }, api_base_override=api_base_override)
    if "error" in resp:
        raise ValueError(f"refresh failed: {resp.get('error')}")
    access_token = resp.get("access_token", "")
    if not access_token:
        raise ValueError("refresh returned no access_token")

    # Build a client with the in-memory token
    client = GoogleClient(creds)
    client._access_token = access_token

    # Profile: email must match stored email
    try:
        profile_email = _get_profile(access_token, api_base_override=api_base_override)
        if creds.email_address and profile_email != creds.email_address:
            raise ValueError(
                f"profile email {profile_email!r} != stored {creds.email_address!r}"
            )
    except Exception as exc:
        raise ValueError(f"profile check failed: {exc}") from exc

    # messages.list + messages.get format=full (proves gmail.readonly not metadata)
    from olo.google.credentials import GMAIL_READONLY_SCOPE
    if GMAIL_READONLY_SCOPE in creds.granted_scopes:
        try:
            msgs = client.get_json("gmail", "/users/me/messages",
                                   [("maxResults", "1"), ("q", "")])
            msgs_list = msgs.get("messages") or []
            if msgs_list:
                msg_id = msgs_list[0]["id"]
                # format=full — this call would 403 with metadata scope
                client.get_json("gmail", f"/users/me/messages/{msg_id}",
                                [("format", "full")])
        except Exception as exc:
            raise ValueError(f"gmail format=full check failed: {exc}") from exc

    # calendarList.list
    from olo.google.credentials import CALENDAR_LIST_SCOPE
    if CALENDAR_LIST_SCOPE in creds.granted_scopes:
        try:
            client.get_json("calendar", "/users/me/calendarList",
                            [("maxResults", "1")])
        except Exception as exc:
            raise ValueError(f"calendarList check failed: {exc}") from exc

    return access_token  # P1-G: caller may revoke on post-verification failure