"""Olo AI — Google account credential storage.

§3.5: atomic 0600 writes, dir 0700, permission enforcement on load,
per-account lock, no access tokens on disk.
I6: api_base_override validation.
I7: alias pattern enforcement.
I9: expiry_status() production function.

No side effects at import time.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from typing import Optional

from olo.adapters import SourceError
from olo.paths import olo_home

# ── Constants ─────────────────────────────────────────────────────────────────

GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
# M2b v2 scopes (replace gmail.metadata for full content)
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
CALENDAR_LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"

CALENDAR_EVENTS_OWNED_RO = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
CALENDAR_EVENTS_RO = CALENDAR_READONLY_SCOPE

# M2 legacy scopes (still supported by existing connected accounts)
REQUIRED_SCOPES = frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO})
# M2b v2 scopes (required for full content)
REQUIRED_SCOPES_V2 = frozenset({
    GMAIL_READONLY_SCOPE,
    CALENDAR_READONLY_SCOPE,
    CALENDAR_LIST_SCOPE,
})
# FORBIDDEN_SCOPES: gmail.metadata must not co-exist with gmail.readonly
# (Google returns 403 for format=full with metadata scope),
# and no write scopes may appear.
FORBIDDEN_SCOPES = frozenset({
    GMAIL_METADATA_SCOPE,  # incompatible with format=full
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/drive",
})

CALENDAR_SCOPES = frozenset({
    CALENDAR_EVENTS_OWNED_RO,
    CALENDAR_EVENTS_RO,
    CALENDAR_READONLY_SCOPE,
    CALENDAR_LIST_SCOPE,
})

# scope_profile values
SCOPE_PROFILE_LEGACY = "legacy_v1"  # M2 gmail.metadata scopes
SCOPE_PROFILE_FULL_V1 = "full_v1"   # M2b full-content scopes

ACCOUNT_RE = "^[a-z0-9]{1,16}$"
_ACCOUNT_PATTERN = re.compile(ACCOUNT_RE)

# I6: allowed api_base_override hosts
_API_BASE_HOST_RE = re.compile(
    r"^http://(127\.0\.0\.1|localhost):[0-9]{1,5}$"
)


# ── Exceptions ────────────────────────────────────────────────────────────────

class GoogleAuthRevoked(SourceError):
    """refresh_token is invalid or expired."""


class CredentialPermissionError(PermissionError):
    """Credential file or directory has unsafe permissions or foreign owner."""


# ── I6: api_base_override validation ─────────────────────────────────────────

def _validate_api_base_override(value: Optional[str]) -> None:
    """Raise AdapterConfigError if value is not None and not a loopback URL.

    Allowed: http://127.0.0.1:<port> or http://localhost:<port>  (§3.5).
    Port must be explicitly present and in range 1–65535.
    No path, query, userinfo, or fragment allowed.
    """
    if value is None:
        return
    from olo.adapters import AdapterConfigError
    import urllib.parse as _up

    parsed = _up.urlparse(value.rstrip("/"))

    # Scheme must be http
    if parsed.scheme != "http":
        raise AdapterConfigError(
            f"api_base_override must be http://127.0.0.1:<port> or "
            f"http://localhost:<port>, got {value!r}"
        )

    # No userinfo (user@host)
    if parsed.username:
        raise AdapterConfigError(
            f"api_base_override must be http://127.0.0.1:<port> or "
            f"http://localhost:<port>, got {value!r}"
        )

    # Host must be exactly 127.0.0.1 or localhost
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise AdapterConfigError(
            f"api_base_override must be http://127.0.0.1:<port> or "
            f"http://localhost:<port>, got {value!r}"
        )

    # Port must be explicit and valid integer (urllib.parse raises ValueError
    # for non-numeric ports like ":abc")
    try:
        port = parsed.port
    except ValueError:
        raise AdapterConfigError(
            f"api_base_override port is not a valid integer, got {value!r}"
        )

    if port is None:
        raise AdapterConfigError(
            f"api_base_override must include an explicit port, got {value!r}"
        )

    # I6 minor: port must be in range 1–65535
    if not (1 <= port <= 65535):
        raise AdapterConfigError(
            f"api_base_override port must be 1–65535, got {port} in {value!r}"
        )

    # No path (empty or "/" only), no query, no fragment
    if parsed.path not in ("", "/"):
        raise AdapterConfigError(
            f"api_base_override must not include a path, got {value!r}"
        )
    if parsed.query or parsed.fragment:
        raise AdapterConfigError(
            f"api_base_override must not include query or fragment, got {value!r}"
        )


# ── I7: alias validation ──────────────────────────────────────────────────────

def _validate_alias(alias: str) -> None:
    """Raise AdapterConfigError for aliases that don't match ^[a-z0-9]{1,16}$."""
    if not _ACCOUNT_PATTERN.match(alias):
        from olo.adapters import AdapterConfigError
        raise AdapterConfigError(
            f"Google account alias {alias!r} is invalid; "
            f"must match ^[a-z0-9]{{1,16}}$"
        )


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GoogleAccountCredentials:
    """Immutable credential record for one Google account.

    Access tokens are NEVER stored here — they live only in memory.

    scope_profile: 'legacy_v1' (M2 gmail.metadata scopes) or
                   'full_v1'   (M2b full-content scopes). None means legacy.
    """

    alias: str
    client_id: str
    client_secret: str
    refresh_token: str
    granted_scopes: frozenset[str]
    email_address: str
    refresh_token_issued_at: datetime
    oauth_publishing_status: str
    api_base_override: Optional[str]
    scope_profile: Optional[str] = None  # M2b: 'legacy_v1' | 'full_v1' | None

    def __post_init__(self) -> None:
        # I7: alias validation at construction time
        _validate_alias(self.alias)
        # I6: api_base_override validation at construction time
        _validate_api_base_override(self.api_base_override)

    def has_gmail_scope(self) -> bool:
        """True if gmail.metadata OR gmail.readonly is granted."""
        return bool(self.granted_scopes & {GMAIL_METADATA_SCOPE, GMAIL_READONLY_SCOPE})

    def has_gmail_readonly_scope(self) -> bool:
        """True only for the full-content gmail.readonly scope (M2b)."""
        return GMAIL_READONLY_SCOPE in self.granted_scopes

    def has_calendar_scope(self) -> bool:
        return bool(self.granted_scopes & CALENDAR_SCOPES)

    def is_full_content_profile(self) -> bool:
        """True when this credential has the M2b full-content scope profile."""
        return self.scope_profile == SCOPE_PROFILE_FULL_V1

    def is_legacy_profile(self) -> bool:
        """True when this credential has the M2 legacy scope profile (or no profile)."""
        return self.scope_profile in (SCOPE_PROFILE_LEGACY, None)

    def __repr__(self) -> str:
        last4 = self.refresh_token[-4:] if len(self.refresh_token) >= 4 else "***"
        secret_last4 = self.client_secret[-4:] if len(self.client_secret) >= 4 else "***"
        return (
            f"GoogleAccountCredentials(alias={self.alias!r}, "
            f"email_address={self.email_address!r}, "
            f"client_id={self.client_id!r}, "
            f"client_secret=…{secret_last4}, "
            f"refresh_token=…{last4}, "
            f"granted_scopes={sorted(self.granted_scopes)!r}, "
            f"oauth_publishing_status={self.oauth_publishing_status!r})"
        )


# ── I9: production expiry_status ─────────────────────────────────────────────

def expiry_status(
    creds: GoogleAccountCredentials,
    now: Optional[datetime] = None,
) -> str:
    """Return 'ok', 'warning' (< 48 h), or 'expired' for testing-mode tokens.

    *now* defaults to datetime.now(UTC).  Pass a fixed value in tests.
    """
    if creds.oauth_publishing_status != "testing":
        return "ok"
    if now is None:
        now = datetime.now(UTC)
    expiry = creds.refresh_token_issued_at + timedelta(days=7)
    remaining = (expiry - now).total_seconds()
    if remaining <= 0:
        return "expired"
    if remaining < 48 * 3600:
        return "warning"
    return "ok"


# ── Paths ─────────────────────────────────────────────────────────────────────

def _cred_dir() -> Path:
    d = olo_home() / "credentials" / "google"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(d, 0o700)
        os.chmod(d.parent, 0o700)
    return d


def credential_path(alias: str) -> Path:
    _validate_alias(alias)  # I7
    return _cred_dir() / f"{alias}.json"


def lock_path(alias: str) -> Path:
    _validate_alias(alias)  # I7
    return _cred_dir() / f"{alias}.lock"


# ── Permission checks (POSIX) ─────────────────────────────────────────────────

def _check_posix_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    st = os.stat(str(path))
    if st.st_uid != os.getuid():
        raise CredentialPermissionError(
            f"credential file {path} is owned by uid {st.st_uid}, not {os.getuid()}"
        )
    bad_bits = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
    if st.st_mode & bad_bits:
        mode_str = oct(stat.S_IMODE(st.st_mode))
        raise CredentialPermissionError(
            f"credential file {path} has unsafe permissions {mode_str} "
            f"(expected 0600)"
        )


def _check_dir_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    st = os.stat(str(path))
    if st.st_uid != os.getuid():
        raise CredentialPermissionError(
            f"credential directory {path} is owned by uid {st.st_uid}"
        )
    bad_bits = stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
    if st.st_mode & bad_bits:
        raise CredentialPermissionError(
            f"credential directory {path} has unsafe permissions "
            f"{oct(stat.S_IMODE(st.st_mode))}"
        )


# ── Atomic write (H6 pattern) ──────────────────────────────────────────────────

def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        if os.name == "nt":
            with open(str(tmp), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        else:
            fd = os.open(
                str(tmp),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IRUSR | stat.S_IWUSR,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ── Lock context manager ──────────────────────────────────────────────────────

class _CredentialLock:
    """Exclusive per-account lock (POSIX flock / Windows locking)."""

    TIMEOUT_SECS = 30.0

    def __init__(self, alias: str) -> None:
        self._path = lock_path(alias)
        self._fh = None

    def __enter__(self) -> "_CredentialLock":
        import time
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fh = open(str(self._path), "a", encoding="utf-8")
        deadline = time.monotonic() + self.TIMEOUT_SECS
        if sys.platform == "win32":
            import msvcrt
            while True:
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self._fh.close()
                        raise SourceError("google credential lock timeout") from None
                    time.sleep(0.05)
        else:
            import fcntl
            import errno
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        self._fh.close()
                        raise SourceError("google credential lock timeout") from None
                    time.sleep(0.05)
        return self

    def __exit__(self, *_) -> None:
        if self._fh is not None:
            if sys.platform == "win32":
                try:
                    import msvcrt
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            self._fh.close()
            self._fh = None


def credential_lock(alias: str) -> _CredentialLock:
    return _CredentialLock(alias)


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _to_dict(creds: GoogleAccountCredentials) -> dict:
    return {
        "alias": creds.alias,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
        "granted_scopes": sorted(creds.granted_scopes),
        "email_address": creds.email_address,
        "refresh_token_issued_at": creds.refresh_token_issued_at.isoformat(),
        "oauth_publishing_status": creds.oauth_publishing_status,
        "api_base_override": creds.api_base_override,
        "scope_profile": creds.scope_profile,  # M2b: 'legacy_v1' | 'full_v1' | None
    }


def _from_dict(d: dict) -> GoogleAccountCredentials:
    issued_raw = d.get("refresh_token_issued_at", "")
    try:
        issued = datetime.fromisoformat(issued_raw)
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=UTC)
    except (ValueError, AttributeError):
        issued = datetime.now(UTC)
    return GoogleAccountCredentials(
        alias=d["alias"],
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        refresh_token=d["refresh_token"],
        granted_scopes=frozenset(d.get("granted_scopes", [])),
        email_address=d.get("email_address", ""),
        refresh_token_issued_at=issued,
        oauth_publishing_status=d.get("oauth_publishing_status", "testing"),
        api_base_override=d.get("api_base_override"),
        scope_profile=d.get("scope_profile"),  # M2b; None = legacy on old files
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load_credentials(alias: str) -> GoogleAccountCredentials:
    """Load credentials for *alias*; raises FileNotFoundError if not connected."""
    _validate_alias(alias)  # I7
    path = credential_path(alias)
    if not path.exists():
        raise FileNotFoundError(
            f"no credentials for account {alias!r}; run: hermes olo google connect --account {alias}"
        )
    _check_posix_permissions(path)
    _check_dir_permissions(path.parent)
    with open(str(path), encoding="utf-8") as f:
        data = json.load(f)
    return _from_dict(data)


def save_credentials(creds: GoogleAccountCredentials) -> None:
    """Save credentials atomically at 0600.  Must be called under the account lock."""
    _validate_alias(creds.alias)  # I7
    _validate_api_base_override(creds.api_base_override)  # I6
    _write_private_json(credential_path(creds.alias), _to_dict(creds))


def remove_credentials(alias: str) -> None:
    _validate_alias(alias)  # I7
    path = credential_path(alias)
    if path.exists():
        path.unlink()
    lp = lock_path(alias)
    if lp.exists():
        try:
            lp.unlink()
        except OSError:
            pass


def list_accounts() -> list[str]:
    try:
        d = _cred_dir()
    except Exception:
        return []
    return sorted(
        p.stem for p in d.glob("*.json")
        if not p.stem.startswith(".")
    )


def email_hash(address: str) -> str:
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()
