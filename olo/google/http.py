"""Olo AI — Google API HTTP client.

§4.1–4.4: authorised GET JSON, error mapping, redaction, credential refresh.
Only the §4.4 HTTP calls may be made.

No side effects at import time.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Optional

from olo import __version__
from olo.adapters import SourceError
from olo.google.credentials import GoogleAccountCredentials

# ── Error types ───────────────────────────────────────────────────────────────

class GoogleRateLimited(SourceError):
    """HTTP 429 or 403 rateLimitExceeded."""


class GmailHistoryExpired(SourceError):
    """Gmail history.list 404 — historyId expired."""


class CalendarSyncTokenExpired(SourceError):
    """Calendar events.list 410 — syncToken expired."""


class CalendarPageTokenInvalid(SourceError):
    """Calendar events.list 400 with pageToken."""


class _GoogleScopeMissing(SourceError):
    """403 insufficientPermissions."""


# ── Base URLs ─────────────────────────────────────────────────────────────────
_BASES = {
    "gmail": "https://gmail.googleapis.com/gmail/v1",
    "calendar": "https://www.googleapis.com/calendar/v3",
}

# ── §4.4 allow-list (method, path-prefix) ─────────────────────────────────────
_ALLOWED_PATHS: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/token"),
    ("POST", "/revoke"),
    ("GET", "/gmail/v1/users/me/profile"),
    ("GET", "/gmail/v1/users/me/history"),
    ("GET", "/gmail/v1/users/me/messages/"),
    ("GET", "/gmail/v1/users/me/messages"),   # messages.list (M2b)
    ("GET", "/calendar/v3/calendars/primary/events"),
    ("GET", "/calendar/v3/users/me/calendarList"),  # calendarList.list (M2b)
    ("GET", "/calendar/v3/calendars/"),             # other calendars (M2b)
})


def _assert_allowed(method: str, full_url: str) -> None:
    """Raise AssertionError if the method+path is not in §4.4."""
    parsed = urllib.parse.urlparse(full_url)
    path = parsed.path
    for allowed_method, allowed_prefix in _ALLOWED_PATHS:
        if method == allowed_method and path.startswith(allowed_prefix):
            return
    raise AssertionError(
        f"HTTP call not in §4.4 allow-list: {method} {path}"
    )


# ── Redaction ─────────────────────────────────────────────────────────────────

def _redact(text: str) -> str:
    """Remove known secret patterns; fall back to agent.redact if available."""
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    # Belt-and-suspenders: mask anything resembling an OAuth token
    text = re.sub(r"\bya29\.[A-Za-z0-9_\-]{20,}", "ya29.[REDACTED]", text)
    text = re.sub(r'"access_token"\s*:\s*"[^"]{10,}"', '"access_token":"[REDACTED]"', text)
    text = re.sub(r'"refresh_token"\s*:\s*"[^"]{10,}"', '"refresh_token":"[REDACTED]"', text)
    return text


# ── GoogleClient ──────────────────────────────────────────────────────────────

class GoogleClient:
    """Authorised Google API client for one account.

    Usage::

        client = GoogleClient(creds)
        data = client.get_json("gmail", "/users/me/profile", [])
    """

    def __init__(
        self,
        creds: GoogleAccountCredentials,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._creds = creds
        self._timeout = timeout
        self._access_token: Optional[str] = None
        self._alias = creds.alias

    # ── Token management ──────────────────────────────────────────────────────

    def _get_token(self) -> str:
        if self._access_token is None:
            self._access_token = self._do_refresh()
        return self._access_token

    def _do_refresh(self) -> str:
        from olo.google.oauth import refresh_access_token
        return refresh_access_token(self._alias)

    def _invalidate_token(self) -> None:
        self._access_token = None

    # ── Base URL resolution ───────────────────────────────────────────────────

    def _base_url(self, base: str) -> str:
        override = self._creds.api_base_override
        if override:
            return override.rstrip("/")
        return _BASES[base]

    def _build_url(self, base: str, path: str, params: Sequence[tuple[str, str]]) -> str:
        base_url = self._base_url(base)
        # For override, path already includes "/gmail/v1/..." etc.
        if self._creds.api_base_override:
            # Strip the standard prefix so path is clean
            prefix = {
                "gmail": "/gmail/v1",
                "calendar": "/calendar/v3",
            }[base]
            if not path.startswith(prefix):
                path = prefix + path
        url = base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return url

    # ── Core GET ──────────────────────────────────────────────────────────────

    def get_json(
        self,
        base: str,
        path: str,
        params: Sequence[tuple[str, str]],
    ) -> Mapping:
        """Make an authorised GET; return parsed JSON.

        On 401, refresh once and retry once.
        Raises SourceError subclasses per §4.2.
        """
        url = self._build_url(base, path, params)
        _assert_allowed("GET", url)
        return self._get_with_retry(url)

    def _get_with_retry(self, url: str) -> Mapping:
        token = self._get_token()
        result, status = self._raw_get(url, token)
        if status == 401:
            self._invalidate_token()
            token = self._do_refresh()
            self._access_token = token
            result, status = self._raw_get(url, token)
            if status == 401:
                from olo.google.credentials import GoogleAuthRevoked
                raise GoogleAuthRevoked(
                    f"google auth revoked for account {self._alias!r}: "
                    f"run hermes olo google connect --account {self._alias}"
                )
        return self._check_status(result, status, url)

    def _raw_get(self, url: str, token: str) -> tuple[Mapping | str, int]:
        """Return (parsed_response_or_str, status_code)."""
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": f"olo-ai/{__version__}",
            },
        )
        try:
            from hermes_cli.urllib_security import open_credentialed_url
            with open_credentialed_url(req, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(body), resp.status
                except json.JSONDecodeError:
                    return body, resp.status
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:
                pass
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
            except Exception:
                parsed = {}
            return parsed, exc.code
        except (TimeoutError, OSError) as exc:
            raise SourceError(
                f"google network: {_redact(type(exc).__name__)}"
            ) from None

    def _check_status(self, result: Mapping | str, status: int, url: str) -> Mapping:
        """Map HTTP errors to SourceError subclasses per §4.2."""
        if status == 200:
            if not isinstance(result, dict):
                raise SourceError("google malformed response")
            return result

        # Extract error details
        err_dict: dict = {}
        if isinstance(result, dict):
            err_dict = (
                result.get("error", {}) if isinstance(result.get("error"), dict)
                else {"message": str(result.get("error", ""))}
            )
        err_message = err_dict.get("message", "")
        err_reason = ""
        for e in err_dict.get("errors", [{}]):
            if isinstance(e, dict):
                err_reason = e.get("reason", "")
                break

        parsed = urllib.parse.urlparse(url)
        path = parsed.path

        # 403 rate-limited
        if status == 403 and err_reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            raise GoogleRateLimited("google rate limited")
        if status == 429:
            raise GoogleRateLimited("google rate limited")

        # 403 scope
        if status == 403 and err_reason in (
            "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"
        ):
            raise _GoogleScopeMissing("google scope missing")

        # 404 on history → expired
        if status == 404 and "/users/me/history" in path:
            raise GmailHistoryExpired("gmail history expired")

        # 400 on history with a numeric startHistoryId → also treat as expired
        # (B3 §2.3: Google returns 400 for some invalid history IDs, not just 404)
        if status == 400 and "/users/me/history" in path:
            raise GmailHistoryExpired(f"gmail history expired (400): {err_message[:100]}")

        # 404 on messages → caller handles as deleted (return None via get_message_or_none)
        if status == 404 and re.search(r"/users/me/messages/", path):
            return {}   # sentinel — caller checks for absent "id"

        # 410 on events
        if status == 410 and "/calendars/" in path:
            raise CalendarSyncTokenExpired("calendar sync token expired")

        # 410 on calendarList (B4: sync token expired)
        if status == 410 and "/users/me/calendarList" in path:
            raise CalendarSyncTokenExpired("calendarList sync token expired")

        # 400 on events with pageToken
        if status == 400 and "/calendars/" in path:
            raise CalendarPageTokenInvalid("calendar page token invalid")

        reason = _redact(err_message[:200] if err_message else str(status))
        raise SourceError(f"google http {status}: {reason}")

    # ── Convenience wrappers ───────────────────────────────────────────────────

    def get_message_or_none(
        self,
        message_id: str,
        metadata_headers: list[str],
    ) -> Optional[Mapping]:
        """Fetch message metadata; return None if 404 (deleted)."""
        params: list[tuple[str, str]] = [
            ("format", "metadata"),
            ("fields", "id,threadId,labelIds,internalDate,sizeEstimate,payload/headers"),
        ]
        for h in metadata_headers:
            params.append(("metadataHeaders", h))
        result = self.get_json(
            "gmail",
            f"/users/me/messages/{message_id}",
            params,
        )
        if not result.get("id"):
            return None
        return result

    def get_message_full(self, message_id: str) -> Optional[Mapping]:
        """Fetch a message in full format (M2b); returns None on 404.

        Requires gmail.readonly scope. Raises _GoogleScopeMissing (SourceError)
        on 403 if the credential only has gmail.metadata.
        """
        result = self.get_json(
            "gmail",
            f"/users/me/messages/{message_id}",
            [("format", "full")],
        )
        if not result.get("id"):
            return None
        return result

    def list_messages(self, params: list[tuple[str, str]]) -> Mapping:
        """Call messages.list with the given params (M2b backfill)."""
        return self.get_json("gmail", "/users/me/messages", params)
