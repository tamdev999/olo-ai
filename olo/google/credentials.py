"""Olo AI — Google account credential storage.

§3.5: atomic 0600 writes, dir 0700, permission enforcement on load,
per-account lock, no access tokens on disk.

No side effects at import time.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Optional

from olo.adapters import SourceError
from olo.paths import olo_home

# ── Constants ─────────────────────────────────────────────────────────────────

GMAIL_METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
CALENDAR_EVENTS_OWNED_RO = "https://www.googleapis.com/auth/calendar.events.owned.readonly"
# Fallback if V3 fails and BBC approves:
CALENDAR_EVENTS_RO = "https://www.googleapis.com/auth/calendar.events.readonly"

REQUIRED_SCOPES = frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO})
CALENDAR_SCOPES = frozenset({CALENDAR_EVENTS_OWNED_RO, CALENDAR_EVENTS_RO})

ACCOUNT_RE = "^[a-z0-9]{1,16}$"
_ACCOUNT_PATTERN = __import__("re").compile(ACCOUNT_RE)


# ── Exceptions ────────────────────────────────────────────────────────────────

class GoogleAuthRevoked(SourceError):
    """refresh_token is invalid or expired."""


class CredentialPermissionError(PermissionError):
    """Credential file or directory has unsafe permissions or foreign owner."""


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GoogleAccountCredentials:
    """Immutable credential record for one Google account.

    Access tokens are NEVER stored here — they live only in memory.
    """

    alias: str
    client_id: str
    client_secret: str          # non-confidential for desktop apps but stored 0600
    refresh_token: str
    granted_scopes: frozenset[str]
    email_address: str          # recorded after connect
    refresh_token_issued_at: datetime   # UTC; set at connect and on rotation
    oauth_publishing_status: str        # "testing" | "production"
    api_base_override: Optional[str]    # loopback override for tests only

    def has_gmail_scope(self) -> bool:
        return GMAIL_METADATA_SCOPE in self.granted_scopes

    def has_calendar_scope(self) -> bool:
        return bool(self.granted_scopes & CALENDAR_SCOPES)

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


# ── Paths ─────────────────────────────────────────────────────────────────────

def _cred_dir() -> Path:
    d = olo_home() / "credentials" / "google"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(d, 0o700)
        os.chmod(d.parent, 0o700)
    return d


def credential_path(alias: str) -> Path:
    return _cred_dir() / f"{alias}.json"


def lock_path(alias: str) -> Path:
    return _cred_dir() / f"{alias}.lock"


# ── Permission checks (POSIX) ─────────────────────────────────────────────────

def _check_posix_permissions(path: Path) -> None:
    """Raise CredentialPermissionError if file is group/world-accessible or foreign-owned."""
    if os.name == "nt":
        return
    st = os.stat(str(path))
    # Check owner
    if st.st_uid != os.getuid():
        raise CredentialPermissionError(
            f"credential file {path} is owned by uid {st.st_uid}, not {os.getuid()}"
        )
    # Check mode: no group/world read or write
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
    """Atomically write JSON at mode 0600 from creation (H6 pattern)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(f".tmp.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        if os.name == "nt":
            # Windows: best-effort 0600 (chmod is a no-op for real ACLs here)
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

    def __enter__(self) -> _CredentialLock:
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
    """Context manager: exclusive per-account credential lock."""
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
    )


# ── Public API ────────────────────────────────────────────────────────────────

def load_credentials(alias: str) -> GoogleAccountCredentials:
    """Load credentials for *alias*; raises FileNotFoundError if not connected."""
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
    _write_private_json(credential_path(creds.alias), _to_dict(creds))


def remove_credentials(alias: str) -> None:
    """Delete the credential file for *alias*."""
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
    """Return sorted list of connected account aliases."""
    try:
        d = _cred_dir()
    except Exception:
        return []
    return sorted(
        p.stem for p in d.glob("*.json")
        if not p.stem.startswith(".")
    )


def email_hash(address: str) -> str:
    """SHA-256 hex of the lowercased email address."""
    return hashlib.sha256(address.strip().lower().encode()).hexdigest()
