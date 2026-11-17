"""Olo M2b B6 — Live test data seeder.

Creates synthetic Gmail messages and Calendar events in the throwaway test
account using Google API write paths.  Production Olo code is never imported
here; write credentials are entirely separate.

SAFETY GUARDS
─────────────
1. OLO_SEEDER_ALLOWED_EMAIL must match the authenticated account's email.
2. OLO_SEEDER_CLIENT_SECRET_FILE must point at a client-secret JSON from a
   *separate* Google Cloud project with write scopes.
3. All generated messages contain OLO-BODY-MARKER in the body so the live
   tests can verify Olo never stores that string in signals.payload.

REQUIRED WRITE SCOPES (seeder project only)
───────────────────────────────────────────
  https://www.googleapis.com/auth/gmail.insert
  https://www.googleapis.com/auth/gmail.labels
  https://mail.google.com/            (for trash/permanent-delete)
  https://www.googleapis.com/auth/calendar

ENVIRONMENT
───────────
  OLO_SEEDER_ALLOWED_EMAIL       Required.  Guards against seeding a real account.
  OLO_SEEDER_CLIENT_SECRET_FILE  Required.  Client-secret JSON (write-scoped project).
  OLO_SEEDER_TOKEN_FILE          Optional.  Path to cached token JSON (default:
                                             ~/.olo_seeder_token.json).
  OLO_SEEDER_CREDENTIALS_FILE    Optional.  Alias for OLO_SEEDER_TOKEN_FILE.

USAGE
─────
  python -m tests.olo.live_seed.seeder seed-gmail    # create P1-P8
  python -m tests.olo.live_seed.seeder seed-calendar # create E1-E4
  python -m tests.olo.live_seed.seeder status        # show account + existing fixtures
  python -m tests.olo.live_seed.seeder auth          # run OAuth flow, save token

No side effects at import time.
"""
from __future__ import annotations

import base64
import email.mime.multipart
import email.mime.text
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional


# ── Constants ─────────────────────────────────────────────────────────────────

BODY_MARKER = "OLO-BODY-MARKER"

# Write scopes required by the seeder project
_SEEDER_SCOPES = " ".join(sorted([
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "openid",
    "email",
]))

_GMAIL_INSERT_URL   = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/import"
_GMAIL_SEND_URL     = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
_GMAIL_MOD_URL      = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/modify"
_GMAIL_TRASH_URL    = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}/trash"
_GMAIL_DELETE_URL   = "https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}"
_CALENDAR_EVENTS    = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_CALENDAR_LIST_URL  = "https://www.googleapis.com/calendar/v3/calendars"
_PROFILE_URL        = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
_TOKEN_URL          = "https://oauth2.googleapis.com/token"
_AUTH_URL           = "https://accounts.google.com/o/oauth2/v2/auth"


# ── Account guard ─────────────────────────────────────────────────────────────

class SeederAccountError(RuntimeError):
    """Raised when the seeder account guard fires."""


def _assert_allowed_account(email_address: str) -> None:
    """Raise SeederAccountError unless the account matches OLO_SEEDER_ALLOWED_EMAIL."""
    allowed = os.environ.get("OLO_SEEDER_ALLOWED_EMAIL", "").strip().lower()
    if not allowed:
        raise SeederAccountError(
            "OLO_SEEDER_ALLOWED_EMAIL is not set. "
            "Set it to the throwaway test account email to authorise seeding."
        )
    actual = email_address.strip().lower()
    if actual != allowed:
        raise SeederAccountError(
            f"Account guard: authenticated as {actual!r} but "
            f"OLO_SEEDER_ALLOWED_EMAIL={allowed!r}. "
            "Seeding a non-throwaway account is not permitted."
        )


# ── Token store ───────────────────────────────────────────────────────────────

def _token_path() -> Path:
    p = (
        os.environ.get("OLO_SEEDER_TOKEN_FILE")
        or os.environ.get("OLO_SEEDER_CREDENTIALS_FILE")
    )
    if p:
        return Path(p)
    return Path.home() / ".olo_seeder_token.json"


def _load_token() -> Optional[dict]:
    p = _token_path()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_token(tok: dict) -> None:
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tok, indent=2), encoding="utf-8")
    if os.name != "nt":
        import stat as _stat
        os.chmod(str(p), _stat.S_IRUSR | _stat.S_IWUSR)


# ── OAuth helpers ─────────────────────────────────────────────────────────────

def _load_client_secret() -> dict:
    path = os.environ.get("OLO_SEEDER_CLIENT_SECRET_FILE", "")
    if not path:
        raise SeederAccountError(
            "OLO_SEEDER_CLIENT_SECRET_FILE is not set. "
            "Point it at the client-secret JSON from the seeder Google Cloud project."
        )
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    # Support both "installed" and "web" keys
    return raw.get("installed") or raw.get("web") or raw


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange refresh token for a new access token."""
    params = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }
    req = urllib.request.Request(
        _TOKEN_URL,
        data=urllib.parse.urlencode(params).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["access_token"]


def _get_access_token() -> str:
    """Return a valid access token, refreshing if needed."""
    tok = _load_token()
    if not tok:
        raise SeederAccountError(
            "No seeder token found. Run: python -m tests.olo.live_seed.seeder auth"
        )
    client = _load_client_secret()
    return _refresh_access_token(
        tok.get("client_id") or client["client_id"],
        tok.get("client_secret") or client["client_secret"],
        tok["refresh_token"],
    )


def _run_oauth_flow() -> None:
    """Interactive OAuth flow for the seeder. Saves token to OLO_SEEDER_TOKEN_FILE."""
    import hashlib
    import http.server
    import secrets
    import threading

    client = _load_client_secret()
    client_id = client["client_id"]
    client_secret = client["client_secret"]
    redirect_uris = client.get("redirect_uris", ["http://127.0.0.1:0/"])

    # Prefer loopback redirect
    redirect_uri = "http://127.0.0.1:0/"
    for uri in redirect_uris:
        if "127.0.0.1" in uri or "localhost" in uri:
            redirect_uri = uri
            break

    # Pick a free port
    import socket as _socket
    with _socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    redirect_uri = f"http://127.0.0.1:{port}/"

    # PKCE
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(96)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": _SEEDER_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = _AUTH_URL + "?" + urllib.parse.urlencode(params)

    # Loopback listener
    result: dict = {}
    done = threading.Event()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            result.update(dict(urllib.parse.parse_qsl(parsed.query)))
            done.set()
            body = b"<html><body><p>Authorised. Close this tab.</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    server.timeout = 1.0

    print(f"\nOpen this URL in a browser and sign in as the THROWAWAY test account:\n\n{auth_url}\n")

    deadline = time.monotonic() + 300
    while not done.is_set() and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()

    if not done.is_set():
        raise SeederAccountError("OAuth flow timed out waiting for redirect.")

    if result.get("state") != state:
        raise SeederAccountError("OAuth state mismatch — possible CSRF.")

    code = result.get("code", "")
    if not code:
        raise SeederAccountError(f"No code in redirect params: {result}")

    # Exchange code for tokens
    tok_params = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
        "code_verifier": verifier,
    }
    req = urllib.request.Request(
        _TOKEN_URL,
        data=urllib.parse.urlencode(tok_params).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.loads(resp.read())

    if "error" in tok:
        raise SeederAccountError(f"Token exchange error: {tok['error']}")

    # Get profile to verify account
    profile_req = urllib.request.Request(
        _PROFILE_URL,
        headers={"Authorization": f"Bearer {tok['access_token']}"},
    )
    with urllib.request.urlopen(profile_req, timeout=15) as resp:
        profile = json.loads(resp.read())
    email_address = profile.get("emailAddress", "")

    _assert_allowed_account(email_address)

    saved = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tok["refresh_token"],
        "email": email_address,
        "granted_at": datetime.now(UTC).isoformat(),
    }
    _save_token(saved)
    print(f"\nSeeder token saved for {email_address!r} → {_token_path()}")


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _api_get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(f"GET {url} → {exc.code}: {raw[:200]}") from exc


def _api_post(url: str, body: dict | bytes, token: str, *, content_type: str = "application/json") -> dict:
    if isinstance(body, dict):
        data = json.dumps(body).encode()
    else:
        data = body
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(f"POST {url} → {exc.code}: {raw[:200]}") from exc


def _api_delete(url: str, token: str) -> None:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        raise RuntimeError(f"DELETE {url} → {exc.code}: {raw[:200]}") from exc


# ── MIME builder ──────────────────────────────────────────────────────────────

def _rfc2822_date(dt: Optional[datetime] = None) -> str:
    """Return an RFC 2822-formatted date string."""
    import email.utils as _eu
    if dt is None:
        dt = datetime.now(UTC)
    return _eu.formatdate(dt.timestamp(), usegmt=True)


def build_mime_message(
    *,
    subject: str,
    from_addr: str,
    to_addr: str,
    body_text: str,
    cc_addrs: list[str] | None = None,
    message_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: list[str] | None = None,
    date: Optional[datetime] = None,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Build an RFC 5322 message string.

    Always includes OLO-BODY-MARKER in the body so live tests can verify
    Olo never stores that string in signals.payload.
    """
    msg = email.mime.text.MIMEText(
        f"{body_text}\n\n{BODY_MARKER}\n", "plain", "utf-8"
    )
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    if message_id:
        msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg["Date"] = _rfc2822_date(date)
    if extra_headers:
        for k, v in extra_headers.items():
            msg[k] = v
    return msg.as_string()


def _encode_message(raw: str) -> str:
    """Base64url-encode a raw RFC 5322 message for Gmail API."""
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _make_message_id(label: str, domain: str = "olo-seed.example.com") -> str:
    """Generate a deterministic RFC 5322 Message-ID for a fixture label."""
    ts = int(time.time())
    return f"<{label}-{ts}@{domain}>"


# ── Gmail seeder ──────────────────────────────────────────────────────────────

class GmailSeeder:
    """Creates P1–P8 fixture messages via Gmail API.

    P1–P5, P7: received messages (import with INBOX label)
    P6: sent reply to P5 (import with SENT label, same thread as P5)
    P8: self-email (sent to self; import with INBOX+SENT)
    """

    def __init__(self, token: str, account_email: str) -> None:
        self._token = token
        self._email = account_email
        self.created: dict[str, dict] = {}  # label -> {id, thread_id, message_id_header}

    def _import_message(self, raw: str, label_ids: list[str]) -> dict:
        """Import a raw RFC 5322 message via messages.import."""
        encoded = _encode_message(raw)
        url = _GMAIL_INSERT_URL + "?internalDateSource=dateHeader"
        body = json.dumps({"labelIds": label_ids, "raw": encoded}).encode()
        # messages.import uses multipart upload — use simple upload for small messages
        result = _api_post(url, body, self._token, content_type="application/json")
        return result

    def seed_p1(self, sender: str, run_tag: str) -> dict:
        """P1: simple received message."""
        subject = f"[OLO-P1-{run_tag}] Integration test fixture 1"
        mid = _make_message_id(f"p1-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=sender, to_addr=self._email,
            body_text=f"P1 fixture message.\nRun tag: {run_tag}\n",
            message_id=mid, date=datetime.now(UTC) - timedelta(minutes=10),
        )
        result = self._import_message(raw, ["INBOX", "UNREAD"])
        self.created["P1"] = {
            "id": result["id"], "thread_id": result.get("threadId", result["id"]),
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p2(self, sender: str, run_tag: str, cc: str = "") -> dict:
        """P2: received message with Cc."""
        subject = f"[OLO-P2-{run_tag}] Integration test fixture 2"
        mid = _make_message_id(f"p2-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=sender, to_addr=self._email,
            body_text=f"P2 fixture message with Cc.\nRun tag: {run_tag}\n",
            message_id=mid, cc_addrs=[cc] if cc else None,
            date=datetime.now(UTC) - timedelta(minutes=9),
        )
        result = self._import_message(raw, ["INBOX", "UNREAD"])
        self.created["P2"] = {
            "id": result["id"], "thread_id": result.get("threadId", result["id"]),
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p3(self, sender: str, run_tag: str) -> dict:
        """P3: received message (will be starred in stage 2)."""
        subject = f"[OLO-P3-{run_tag}] Integration test fixture 3"
        mid = _make_message_id(f"p3-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=sender, to_addr=self._email,
            body_text=f"P3 fixture message (star target).\nRun tag: {run_tag}\n",
            message_id=mid, date=datetime.now(UTC) - timedelta(minutes=8),
        )
        result = self._import_message(raw, ["INBOX", "UNREAD"])
        self.created["P3"] = {
            "id": result["id"], "thread_id": result.get("threadId", result["id"]),
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p4(self, sender: str, run_tag: str) -> dict:
        """P4: received message (will be trashed then permanently deleted in stage 2)."""
        subject = f"[OLO-P4-{run_tag}] Integration test fixture 4"
        mid = _make_message_id(f"p4-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=sender, to_addr=self._email,
            body_text=f"P4 fixture message (trash/delete target).\nRun tag: {run_tag}\n",
            message_id=mid, date=datetime.now(UTC) - timedelta(minutes=7),
        )
        result = self._import_message(raw, ["INBOX", "UNREAD"])
        self.created["P4"] = {
            "id": result["id"], "thread_id": result.get("threadId", result["id"]),
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p5(self, sender: str, run_tag: str) -> dict:
        """P5: received message (thread anchor for P6/P7)."""
        subject = f"[OLO-P5-{run_tag}] Integration test fixture 5"
        mid = _make_message_id(f"p5-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=sender, to_addr=self._email,
            body_text=f"P5 fixture message (thread anchor).\nRun tag: {run_tag}\n",
            message_id=mid, date=datetime.now(UTC) - timedelta(minutes=6),
        )
        result = self._import_message(raw, ["INBOX", "UNREAD"])
        self.created["P5"] = {
            "id": result["id"], "thread_id": result.get("threadId", result["id"]),
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p6(self, sender: str, run_tag: str) -> dict:
        """P6: sent reply from test account to P5 (SENT label, same thread)."""
        p5 = self.created.get("P5")
        if not p5:
            raise ValueError("P5 must be seeded before P6")
        subject = f"Re: [OLO-P5-{run_tag}] Integration test fixture 5"
        mid = _make_message_id(f"p6-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=self._email, to_addr=sender,
            body_text=f"P6 fixture reply (sent).\nRun tag: {run_tag}\n",
            message_id=mid,
            in_reply_to=p5["message_id_header"],
            references=[p5["message_id_header"]],
            date=datetime.now(UTC) - timedelta(minutes=5),
        )
        result = self._import_message(raw, ["SENT"])
        self.created["P6"] = {
            "id": result["id"],
            "thread_id": p5["thread_id"],  # same thread as P5
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p7(self, sender: str, run_tag: str) -> dict:
        """P7: received reply from sender to P6 (same thread)."""
        p5 = self.created.get("P5")
        p6 = self.created.get("P6")
        if not p5 or not p6:
            raise ValueError("P5 and P6 must be seeded before P7")
        subject = f"Re: [OLO-P5-{run_tag}] Integration test fixture 5"
        mid = _make_message_id(f"p7-{run_tag}")
        refs = [p5["message_id_header"], p6["message_id_header"]]
        raw = build_mime_message(
            subject=subject, from_addr=sender, to_addr=self._email,
            body_text=f"P7 fixture reply (received).\nRun tag: {run_tag}\n",
            message_id=mid,
            in_reply_to=p6["message_id_header"],
            references=refs,
            date=datetime.now(UTC) - timedelta(minutes=4),
        )
        result = self._import_message(raw, ["INBOX", "UNREAD"])
        self.created["P7"] = {
            "id": result["id"],
            "thread_id": p5["thread_id"],  # same thread
            "message_id_header": mid, "subject": subject,
        }
        return result

    def seed_p8(self, run_tag: str) -> dict:
        """P8: self-email (test account emails itself — both INBOX and SENT)."""
        subject = f"[OLO-P8-{run_tag}] Self-email fixture"
        mid = _make_message_id(f"p8-{run_tag}")
        raw = build_mime_message(
            subject=subject, from_addr=self._email, to_addr=self._email,
            body_text=f"P8 self-email fixture.\nRun tag: {run_tag}\n",
            message_id=mid, date=datetime.now(UTC) - timedelta(minutes=3),
        )
        result = self._import_message(raw, ["INBOX", "SENT", "UNREAD"])
        self.created["P8"] = {
            "id": result["id"], "thread_id": result.get("threadId", result["id"]),
            "message_id_header": mid, "subject": subject,
        }
        return result

    def archive_p1(self) -> None:
        """Stage 2: remove INBOX label from P1 (archive)."""
        p1 = self.created.get("P1") or self._load_fixture("P1")
        if not p1:
            raise ValueError("P1 not found — seed stage 1 first")
        url = _GMAIL_MOD_URL.format(id=p1["id"])
        _api_post(url, {"removeLabelIds": ["INBOX"]}, self._token)

    def mark_read_p2(self) -> None:
        """Stage 2: remove UNREAD from P2."""
        p2 = self.created.get("P2") or self._load_fixture("P2")
        if not p2:
            raise ValueError("P2 not found")
        url = _GMAIL_MOD_URL.format(id=p2["id"])
        _api_post(url, {"removeLabelIds": ["UNREAD"]}, self._token)

    def star_p3(self) -> None:
        """Stage 2: add STARRED to P3."""
        p3 = self.created.get("P3") or self._load_fixture("P3")
        if not p3:
            raise ValueError("P3 not found")
        url = _GMAIL_MOD_URL.format(id=p3["id"])
        _api_post(url, {"addLabelIds": ["STARRED"]}, self._token)

    def trash_then_delete_p4(self) -> None:
        """Stage 2: trash P4 then permanently delete it."""
        p4 = self.created.get("P4") or self._load_fixture("P4")
        if not p4:
            raise ValueError("P4 not found")
        # Trash
        _api_post(_GMAIL_TRASH_URL.format(id=p4["id"]), {}, self._token)
        # Permanent delete
        _api_delete(_GMAIL_DELETE_URL.format(id=p4["id"]), self._token)

    def _load_fixture(self, label: str) -> Optional[dict]:
        """Load fixture state from the saved JSON file."""
        p = _fixture_state_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("gmail", {}).get(label)

    def seed_all(self, sender: str, run_tag: str) -> None:
        """Seed P1–P8 in order."""
        print(f"  Seeding P1 (from={sender!r})…", end=" ")
        self.seed_p1(sender, run_tag)
        print(f"id={self.created['P1']['id']}")

        print(f"  Seeding P2…", end=" ")
        self.seed_p2(sender, run_tag)
        print(f"id={self.created['P2']['id']}")

        print(f"  Seeding P3…", end=" ")
        self.seed_p3(sender, run_tag)
        print(f"id={self.created['P3']['id']}")

        print(f"  Seeding P4…", end=" ")
        self.seed_p4(sender, run_tag)
        print(f"id={self.created['P4']['id']}")

        print(f"  Seeding P5 (thread anchor)…", end=" ")
        self.seed_p5(sender, run_tag)
        print(f"id={self.created['P5']['id']}")

        print(f"  Seeding P6 (sent reply to P5)…", end=" ")
        self.seed_p6(sender, run_tag)
        print(f"id={self.created['P6']['id']}")

        print(f"  Seeding P7 (received reply to P6)…", end=" ")
        self.seed_p7(sender, run_tag)
        print(f"id={self.created['P7']['id']}")

        print(f"  Seeding P8 (self-email)…", end=" ")
        self.seed_p8(run_tag)
        print(f"id={self.created['P8']['id']}")

        print("  All 8 Gmail fixtures created.")

    def apply_stage2_mutations(self) -> None:
        """Apply all stage 2 mutations: archive P1, mark-read P2, star P3, trash+delete P4."""
        print("  Archiving P1…", end=" ")
        self.archive_p1()
        print("done")
        print("  Marking P2 as read…", end=" ")
        self.mark_read_p2()
        print("done")
        print("  Starring P3…", end=" ")
        self.star_p3()
        print("done")
        print("  Trashing then permanently deleting P4…", end=" ")
        self.trash_then_delete_p4()
        print("done")


# ── Calendar seeder ───────────────────────────────────────────────────────────

class CalendarSeeder:
    """Creates E1–E4 fixture events, secondary calendar, and stage-2 mutations."""

    def __init__(self, token: str) -> None:
        self._token = token
        self.created: dict[str, dict] = {}   # label -> event resource
        self._secondary_cal_id: Optional[str] = None

    def _create_event(self, body: dict) -> dict:
        return _api_post(_CALENDAR_EVENTS, body, self._token)

    def _now_plus(self, hours: float = 0, days: float = 0) -> datetime:
        return datetime.now(UTC) + timedelta(hours=hours, days=days)

    def seed_e1(self, run_tag: str) -> dict:
        """E1: single event (will be renamed in stage 2)."""
        start = self._now_plus(hours=1)
        end = start + timedelta(hours=1)
        body = {
            "summary": f"[OLO-E1-{run_tag}] Integration test event 1",
            "description": f"E1 fixture. {BODY_MARKER}",
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        }
        result = self._create_event(body)
        self.created["E1"] = result
        return result

    def seed_e2(self, run_tag: str) -> dict:
        """E2: single event."""
        start = self._now_plus(hours=2)
        end = start + timedelta(hours=1)
        body = {
            "summary": f"[OLO-E2-{run_tag}] Integration test event 2",
            "description": f"E2 fixture. {BODY_MARKER}",
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        }
        result = self._create_event(body)
        self.created["E2"] = result
        return result

    def seed_e3(self, run_tag: str) -> dict:
        """E3: single event (will be cancelled/deleted in stage 2)."""
        start = self._now_plus(hours=3)
        end = start + timedelta(hours=1)
        body = {
            "summary": f"[OLO-E3-{run_tag}] Integration test event 3",
            "description": f"E3 fixture. {BODY_MARKER}",
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        }
        result = self._create_event(body)
        self.created["E3"] = result
        return result

    def seed_e4(self, run_tag: str) -> dict:
        """E4: weekly recurring event."""
        start = self._now_plus(days=1)
        end = start + timedelta(hours=1)
        body = {
            "summary": f"[OLO-E4-{run_tag}] Weekly recurring test event",
            "description": f"E4 fixture (recurring). {BODY_MARKER}",
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
            "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=4"],
        }
        result = self._create_event(body)
        self.created["E4"] = result
        return result

    def create_secondary_calendar(self, run_tag: str) -> str:
        """Create a secondary calendar for multi-calendar discovery testing.

        Returns the new calendar ID.
        """
        body = {
            "summary": f"[OLO-SEC-{run_tag}] Secondary test calendar",
            "description": f"Secondary calendar for M2b multi-cal testing. {BODY_MARKER}",
            "timeZone": "UTC",
        }
        result = _api_post(_CALENDAR_LIST_URL, body, self._token)
        self._secondary_cal_id = result["id"]
        return self._secondary_cal_id

    def seed_secondary_event(self, run_tag: str) -> dict:
        """Create one event on the secondary calendar."""
        if not self._secondary_cal_id:
            raise ValueError("Create secondary calendar first")
        url = f"https://www.googleapis.com/calendar/v3/calendars/{urllib.parse.quote(self._secondary_cal_id, safe='')}/events"
        start = self._now_plus(hours=4)
        end = start + timedelta(hours=1)
        body = {
            "summary": f"[OLO-SEC-EV-{run_tag}] Secondary calendar event",
            "description": f"Secondary calendar event fixture. {BODY_MARKER}",
            "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
            "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
        }
        result = _api_post(url, body, self._token)
        self.created["SEC_EV"] = result
        return result

    def rename_e1(self, new_summary: str) -> dict:
        """Stage 2: rename E1."""
        e1 = self.created.get("E1") or self._load_fixture("E1")
        if not e1:
            raise ValueError("E1 not found")
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{e1['id']}"
        updated = dict(e1)
        updated["summary"] = new_summary
        req = urllib.request.Request(
            url,
            data=json.dumps(updated).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        self.created["E1"] = result
        return result

    def delete_e3(self) -> None:
        """Stage 2: permanently delete E3."""
        e3 = self.created.get("E3") or self._load_fixture("E3")
        if not e3:
            raise ValueError("E3 not found")
        url = f"https://www.googleapis.com/calendar/v3/calendars/primary/events/{e3['id']}"
        _api_delete(url, self._token)

    def _load_fixture(self, label: str) -> Optional[dict]:
        p = _fixture_state_path()
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("calendar", {}).get(label)

    def seed_all(self, run_tag: str) -> None:
        """Create E1–E4 and secondary calendar."""
        print(f"  Seeding E1…", end=" ")
        self.seed_e1(run_tag)
        print(f"id={self.created['E1']['id']}")

        print(f"  Seeding E2…", end=" ")
        self.seed_e2(run_tag)
        print(f"id={self.created['E2']['id']}")

        print(f"  Seeding E3…", end=" ")
        self.seed_e3(run_tag)
        print(f"id={self.created['E3']['id']}")

        print(f"  Seeding E4 (weekly recurring)…", end=" ")
        self.seed_e4(run_tag)
        print(f"id={self.created['E4']['id']}")

        print(f"  Creating secondary calendar…", end=" ")
        sec_id = self.create_secondary_calendar(run_tag)
        print(f"id={sec_id}")

        print(f"  Seeding secondary calendar event…", end=" ")
        self.seed_secondary_event(run_tag)
        print(f"id={self.created['SEC_EV']['id']}")

        print("  All 4 Calendar events + secondary calendar created.")

    def apply_stage2_mutations(self, run_tag: str) -> None:
        """Rename E1 and delete E3."""
        print("  Renaming E1…", end=" ")
        self.rename_e1(f"[OLO-E1-RENAMED-{run_tag}] Renamed event 1")
        print("done")
        print("  Deleting E3…", end=" ")
        self.delete_e3()
        print("done")


# ── Fixture state persistence ─────────────────────────────────────────────────

def _fixture_state_path() -> Path:
    p = os.environ.get("OLO_SEEDER_STATE_FILE")
    if p:
        return Path(p)
    return Path.home() / ".olo_seeder_state.json"


def save_fixture_state(gmail: GmailSeeder, calendar: CalendarSeeder, run_tag: str) -> None:
    """Save created fixture IDs to a file for stage-2 access."""
    state = {
        "run_tag": run_tag,
        "saved_at": datetime.now(UTC).isoformat(),
        "gmail": {k: v for k, v in gmail.created.items()},
        "calendar": {k: v for k, v in calendar.created.items()},
        "secondary_cal_id": calendar._secondary_cal_id,
    }
    p = _fixture_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    if os.name != "nt":
        import stat as _stat
        os.chmod(str(p), _stat.S_IRUSR | _stat.S_IWUSR)
    print(f"Fixture state saved → {p}")


def load_fixture_state() -> dict:
    p = _fixture_state_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


# ── Status helper ─────────────────────────────────────────────────────────────

def _show_status() -> None:
    tok = _load_token()
    if not tok:
        print("No seeder token found. Run: python -m tests.olo.live_seed.seeder auth")
        return
    print(f"Seeder token: email={tok.get('email')!r}, granted_at={tok.get('granted_at')!r}")

    state = load_fixture_state()
    if state:
        print(f"Fixture state: run_tag={state.get('run_tag')!r}, saved_at={state.get('saved_at')!r}")
        print(f"  Gmail: {list(state.get('gmail', {}).keys())}")
        print(f"  Calendar: {list(state.get('calendar', {}).keys())}")
        print(f"  Secondary cal ID: {state.get('secondary_cal_id')!r}")
    else:
        print("No fixture state found.")


# ── CLI entrypoint ────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print(__doc__)
        return 0

    cmd = args[0]

    if cmd == "auth":
        _run_oauth_flow()
        return 0

    if cmd == "status":
        _show_status()
        return 0

    if cmd == "seed-gmail":
        sender = os.environ.get("OLO_SEEDER_SENDER_EMAIL", "")
        if not sender:
            print("ERROR: OLO_SEEDER_SENDER_EMAIL must be set to a sender email address.")
            return 1
        run_tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        token = _get_access_token()
        tok = _load_token()
        _assert_allowed_account(tok.get("email", ""))
        seeder = GmailSeeder(token, tok["email"])
        print(f"\nSeeding Gmail P1–P8 (run_tag={run_tag!r}, sender={sender!r})…")
        seeder.seed_all(sender, run_tag)
        state = load_fixture_state()
        state.setdefault("gmail", {}).update(seeder.created)
        state["run_tag"] = run_tag
        state["saved_at"] = datetime.now(UTC).isoformat()
        p = _fixture_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"Done. Fixture state → {p}")
        return 0

    if cmd == "seed-calendar":
        run_tag = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        # Try to reuse existing run_tag from state
        state = load_fixture_state()
        if state.get("run_tag"):
            run_tag = state["run_tag"]
        token = _get_access_token()
        tok = _load_token()
        _assert_allowed_account(tok.get("email", ""))
        seeder = CalendarSeeder(token)
        print(f"\nSeeding Calendar E1–E4 + secondary calendar (run_tag={run_tag!r})…")
        seeder.seed_all(run_tag)
        state.setdefault("calendar", {}).update(seeder.created)
        if seeder._secondary_cal_id:
            state["secondary_cal_id"] = seeder._secondary_cal_id
        state["saved_at"] = datetime.now(UTC).isoformat()
        p = _fixture_state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"Done. Fixture state → {p}")
        return 0

    if cmd == "mutate-stage2":
        token = _get_access_token()
        tok = _load_token()
        _assert_allowed_account(tok.get("email", ""))
        state = load_fixture_state()
        run_tag = state.get("run_tag", "unknown")

        g = GmailSeeder(token, tok["email"])
        c = CalendarSeeder(token)
        # Restore IDs from saved state
        g.created = state.get("gmail", {})
        c.created = state.get("calendar", {})
        c._secondary_cal_id = state.get("secondary_cal_id")

        print(f"\nApplying stage 2 mutations (run_tag={run_tag!r})…")
        g.apply_stage2_mutations()
        c.apply_stage2_mutations(run_tag)
        print("Stage 2 mutations complete.")
        return 0

    print(f"Unknown command: {cmd!r}. Valid: auth, status, seed-gmail, seed-calendar, mutate-stage2")
    return 1


if __name__ == "__main__":
    sys.exit(main())
