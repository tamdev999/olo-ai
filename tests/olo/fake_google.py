"""Olo M2 — Fake Google API server for deterministic tests.

Provides:
  FakeGoogleServer: in-process loopback httpd implementing token, profile,
                    history, messages, events endpoints.
  FakeGmailState: mutable mailbox state for the server.
  FakeCalendarState: mutable calendar state.

Usage in tests::

    with FakeGoogleServer() as fake:
        creds = fake.make_credentials("alias")
        # ... test code using api_base_override=fake.base_url
"""
from __future__ import annotations

import http.server
import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Optional


# ── Fake mailbox / calendar state ─────────────────────────────────────────────

@dataclass
class FakeMessage:
    id: str
    thread_id: str
    label_ids: list[str]
    internal_date: str          # ms-since-epoch as string
    size_estimate: int
    subject: str = ""
    from_: str = "sender@example.com"
    to: str = "recipient@example.com"
    message_id_header: str = ""


@dataclass
class FakeGmailState:
    messages: dict[str, FakeMessage] = field(default_factory=dict)
    history_records: list[dict] = field(default_factory=list)   # [{id, ...}]
    current_history_id: str = "1000"
    deleted_message_ids: set[str] = field(default_factory=set)
    # Minimum valid startHistoryId for history.list; requests below → 404
    min_valid_history_id: str = "1000"

    def add_message(
        self,
        msg_id: str,
        thread_id: str = "",
        labels: list[str] | None = None,
        subject: str = "Test Subject",
        from_: str = "sender@example.com",
        to: str = "recipient@example.com",
        internal_date_ms: Optional[str] = None,
    ) -> FakeMessage:
        """Add a new message and a messagesAdded history record."""
        labels = labels or ["INBOX", "UNREAD"]
        ts_ms = internal_date_ms or str(int(datetime.now(UTC).timestamp() * 1000))
        msg = FakeMessage(
            id=msg_id,
            thread_id=thread_id or msg_id,
            label_ids=labels,
            internal_date=ts_ms,
            size_estimate=1024,
            subject=subject,
            from_=from_,
            to=to,
            message_id_header=f"<{msg_id}@example.com>",
        )
        self.messages[msg_id] = msg
        self._bump_history({"messagesAdded": [{"message": {
            "id": msg_id,
            "threadId": thread_id or msg_id,
            "labelIds": labels,
        }}]})
        return msg

    def delete_message(self, msg_id: str) -> None:
        """Remove message and add a messagesDeleted history record."""
        self.deleted_message_ids.add(msg_id)
        self.messages.pop(msg_id, None)
        self._bump_history({"messagesDeleted": [{"message": {"id": msg_id, "threadId": msg_id}}]})

    def add_labels(self, msg_id: str, labels: list[str]) -> None:
        if msg_id in self.messages:
            for lbl in labels:
                if lbl not in self.messages[msg_id].label_ids:
                    self.messages[msg_id].label_ids.append(lbl)
        self._bump_history({"labelsAdded": [{"message": {
            "id": msg_id, "threadId": msg_id,
            "labelIds": self.messages[msg_id].label_ids if msg_id in self.messages else [],
        }, "labelIds": labels}]})

    def remove_labels(self, msg_id: str, labels: list[str]) -> None:
        if msg_id in self.messages:
            self.messages[msg_id].label_ids = [
                lbl for lbl in self.messages[msg_id].label_ids if lbl not in labels
            ]
        msg_labels = self.messages[msg_id].label_ids if msg_id in self.messages else []
        self._bump_history({"labelsRemoved": [{"message": {
            "id": msg_id, "threadId": msg_id,
            "labelIds": msg_labels,
        }, "labelIds": labels}]})

    def _bump_history(self, record_data: dict) -> None:
        h_id = str(int(self.current_history_id) + 1)
        self.current_history_id = h_id
        record = {"id": h_id}
        record.update(record_data)
        self.history_records.append(record)

    # ── F2: scripted page sequences ───────────────────────────────────────────
    # When scripted_history_pages is set, _handle_history dequeues from it
    # instead of computing pages from history_records.
    # Each entry is a dict with optional keys:
    #   "records": list[dict]  — history records (default [])
    #   "next_page_token": str | None  — token for the next page (default None)
    #   "history_id": str | None  — historyId to return (default current_history_id)
    # Also records which page_token was received in the request (for assertion).

    def script_history_pages(self, pages: list[dict]) -> None:
        """Replace normal history.list behaviour with a scripted sequence.

        pages is a list of response blueprints consumed one-at-a-time.
        Call recorded_history_requests() to inspect which tokens were sent.
        """
        self._scripted_pages: list[dict] = list(pages)
        self._recorded_requests: list[str | None] = []

    def pop_scripted_page(self, page_token: str | None) -> dict | None:
        """Return the next scripted page and record the token; None if exhausted."""
        if not hasattr(self, "_scripted_pages") or not self._scripted_pages:
            return None
        self._recorded_requests.append(page_token)
        return self._scripted_pages.pop(0)

    def recorded_history_requests(self) -> list[str | None]:
        """Return the list of pageToken values sent in each request."""
        return list(getattr(self, "_recorded_requests", []))


@dataclass
class FakeCalendarEvent:
    id: str
    summary: str
    status: str = "confirmed"
    updated: str = ""
    start: str = ""
    end: str = ""
    organizer: str = "organizer@example.com"


@dataclass
class FakeCalendarState:
    events: dict[str, FakeCalendarEvent] = field(default_factory=dict)
    sync_token: str = "synctoken_initial"
    _incr_events: list[FakeCalendarEvent] = field(default_factory=list)
    # I3: configurable page size; None = all events in one response
    page_size: Optional[int] = None
    _pages: dict[str, list[FakeCalendarEvent]] = field(default_factory=dict)
    _next_page_counter: int = 0

    def add_event(
        self,
        event_id: str,
        summary: str = "Test Event",
        status: str = "confirmed",
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
    ) -> FakeCalendarEvent:
        now = datetime.now(UTC)
        ev = FakeCalendarEvent(
            id=event_id,
            summary=summary,
            status=status,
            updated=now.isoformat(),
            start=(start_dt or now).isoformat(),
            end=(end_dt or (now + timedelta(hours=1))).isoformat(),
        )
        self.events[event_id] = ev
        self._incr_events.append(ev)
        return ev

    def cancel_event(self, event_id: str) -> None:
        ev = self.events.get(event_id)
        if ev:
            ev.status = "cancelled"
            ev.updated = datetime.now(UTC).isoformat()
            self._incr_events.append(ev)

    def consume_incr(self) -> list[FakeCalendarEvent]:
        evs = list(self._incr_events)
        self._incr_events.clear()
        self.sync_token = f"synctoken_{len(self.events)}"
        return evs

    def all_events_full(self, time_min: Optional[str] = None) -> list[FakeCalendarEvent]:
        evs = list(self.events.values())
        if time_min:
            try:
                tm = datetime.fromisoformat(time_min)
                evs = [
                    e for e in evs
                    if datetime.fromisoformat(e.start) >= tm
                ]
            except Exception:
                pass
        return evs

    # I3: paginate helper
    def paginate(
        self, events: list[FakeCalendarEvent], max_results: int
    ) -> tuple[list[FakeCalendarEvent], Optional[str]]:
        """Return (page, next_page_token_or_None).

        If page_size is set, use min(page_size, max_results) as the effective
        page size; otherwise use max_results directly.
        """
        effective = max_results
        if self.page_size is not None:
            effective = min(self.page_size, max_results)
        if not events or len(events) <= effective:
            return events, None
        page = events[:effective]
        rest = events[effective:]
        self._next_page_counter += 1
        tok = f"pagetoken_{self._next_page_counter}"
        self._pages[tok] = rest
        return page, tok

    def pop_page(self, page_token: str) -> Optional[list[FakeCalendarEvent]]:
        return self._pages.pop(page_token, None)


# ── Fake token store ───────────────────────────────────────────────────────────

@dataclass
class FakeTokenStore:
    """Tracks valid tokens; optionally rotates on every refresh."""
    access_token: str = "access_fake_initial"
    refresh_token: str = "refresh_fake_initial"
    rotate_on_refresh: bool = False
    _refresh_count: int = 0
    invalid_grant: bool = False
    _log: list[str] = field(default_factory=list)

    def do_refresh(self, presented_token: str) -> dict:
        self._log.append(("refresh", presented_token))
        if self.invalid_grant:
            return {"error": "invalid_grant"}
        if presented_token != self.refresh_token:
            return {"error": "invalid_grant"}
        self._refresh_count += 1
        new_access = f"access_fake_{self._refresh_count}"
        self.access_token = new_access
        resp: dict = {"access_token": new_access, "token_type": "Bearer", "expires_in": 3600}
        if self.rotate_on_refresh:
            new_refresh = f"refresh_fake_{self._refresh_count}"
            self.refresh_token = new_refresh
            resp["refresh_token"] = new_refresh
        return resp


# ── Module-level helper used by _handle_events ────────────────────────────────

def _ev_dict_local(ev: "FakeCalendarEvent") -> dict:
    """Serialise a FakeCalendarEvent to the shape Google Calendar returns."""
    return {
        "id": ev.id,
        "status": ev.status,
        "summary": ev.summary,
        "updated": ev.updated,
        "start": {"dateTime": ev.start},
        "end": {"dateTime": ev.end},
        "created": ev.updated,
        "organizer": {"email": ev.organizer, "self": False},
        "htmlLink": f"https://calendar.google.com/event?eid={ev.id}",
    }


# ── HTTP handler ───────────────────────────────────────────────────────────────

class _FakeGoogleHandler(http.server.BaseHTTPRequestHandler):

    # State is set on the class so all handler instances share it
    gmail: FakeGmailState = None
    calendar: FakeCalendarState = None
    tokens: FakeTokenStore = None
    inject_401_on_next: bool = False
    inject_500_on_next: bool = False
    inject_429_on_next: bool = False
    inject_timeout_on_next: bool = False
    inject_malformed_on_next: bool = False
    # Cross-origin redirect target (None = no redirect)
    inject_redirect_to: Optional[str] = None
    email_address: str = "test@example.com"
    granted_scopes: str = ""
    # F7: Event released by FakeGoogleServer.__exit__ to unblock timeout injection
    _teardown_event: threading.Event = None

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _inject_errors(self) -> bool:
        if self.__class__.inject_timeout_on_next:
            self.__class__.inject_timeout_on_next = False
            # F7: use Event instead of time.sleep(60) so teardown can unblock
            ev = self.__class__._teardown_event
            if ev is not None:
                ev.wait(timeout=60)
            else:
                threading.Event().wait(timeout=60)
            return True
        if self.__class__.inject_401_on_next:
            self.__class__.inject_401_on_next = False
            self._send_json({"error": {"code": 401, "message": "Unauthorized"}}, 401)
            return True
        if self.__class__.inject_500_on_next:
            self.__class__.inject_500_on_next = False
            self._send_json({"error": {"code": 500, "message": "Server error"}}, 500)
            return True
        if self.__class__.inject_429_on_next:
            self.__class__.inject_429_on_next = False
            self._send_json({"error": {"code": 429, "message": "Rate limit"}}, 429)
            return True
        if self.__class__.inject_malformed_on_next:
            self.__class__.inject_malformed_on_next = False
            body = b"not json"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if self.__class__.inject_redirect_to:
            target = self.__class__.inject_redirect_to
            self.__class__.inject_redirect_to = None
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return True
        return False

    def do_POST(self) -> None:
        body = self._read_body()
        params = dict(urllib.parse.parse_qsl(body.decode("utf-8", errors="replace")))
        path = self.path.split("?")[0]

        if path.endswith("/token"):
            self._handle_token(params)
        elif path.endswith("/revoke"):
            self._send_json({}, 200)
        elif path.endswith("/o/oauth2/v2/auth"):
            # Auth code endpoint in test: redirect with code
            qs = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            redirect = qs.get("redirect_uri", "http://127.0.0.1:1/")
            state = qs.get("state", "")
            code = "test_auth_code_123"
            redir_url = f"{redirect}?code={code}&state={state}"
            self.send_response(302)
            self.send_header("Location", redir_url)
            self.end_headers()
        else:
            self._send_json({"error": "unknown endpoint"}, 404)

    def do_GET(self) -> None:
        if self._inject_errors():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        if "/gmail/v1/users/me/profile" in path:
            self._handle_profile()
        elif "/gmail/v1/users/me/history" in path:
            self._handle_history(params)
        elif re.search(r"/gmail/v1/users/me/messages/([^/]+)$", path):
            m = re.search(r"/messages/([^/]+)$", path)
            self._handle_message(m.group(1))
        elif "/calendar/v3/calendars/primary/events" in path:
            self._handle_events(params)
        else:
            self._send_json({"error": "unknown"}, 404)

    def _handle_token(self, params: dict) -> None:
        grant = params.get("grant_type", "")
        if grant == "authorization_code":
            code = params.get("code", "")
            if code != "test_auth_code_123":
                self._send_json({"error": "invalid_grant"})
                return
            scopes = self.__class__.granted_scopes or " ".join([
                "https://www.googleapis.com/auth/gmail.metadata",
                "https://www.googleapis.com/auth/calendar.events.owned.readonly",
            ])
            self._send_json({
                "access_token": self.__class__.tokens.access_token,
                "refresh_token": self.__class__.tokens.refresh_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": scopes,
            })
        elif grant == "refresh_token":
            resp = self.__class__.tokens.do_refresh(params.get("refresh_token", ""))
            self._send_json(resp)
        else:
            self._send_json({"error": "unsupported_grant_type"})

    def _handle_profile(self) -> None:
        self._send_json({
            "emailAddress": self.__class__.email_address,
            "messagesTotal": len(self.__class__.gmail.messages),
            "historyId": self.__class__.gmail.current_history_id,
        })

    def _handle_history(self, params: dict) -> None:
        # Re-parse query string to handle repeated historyTypes keys
        parsed_qs = urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query)
        htypes = [v for k, v in parsed_qs if k == "historyTypes"]

        start_id_str = params.get("startHistoryId", "")
        page_token_in = params.get("pageToken")

        # ── F2: scripted page sequences (checked before any validation) ───────
        scripted = self.__class__.gmail.pop_scripted_page(page_token_in)
        if scripted is not None:
            history_id = scripted.get(
                "history_id", self.__class__.gmail.current_history_id
            )
            resp: dict = {"historyId": history_id}
            recs = scripted.get("records", [])
            if recs:
                resp["history"] = recs
            npt = scripted.get("next_page_token")
            if npt:
                resp["nextPageToken"] = npt
            self._send_json(resp)
            return

        # ── Normal path ───────────────────────────────────────────────────────
        try:
            start_id = int(start_id_str)
        except ValueError:
            self._send_json({"error": {"code": 400, "message": "invalid historyId"}}, 400)
            return

        min_id = int(self.__class__.gmail.min_valid_history_id)
        if start_id < min_id:
            self._send_json({"error": {"code": 404, "message": "history not available"}}, 404)
            return

        max_results = int(params.get("maxResults", "100"))

        all_records = [
            r for r in self.__class__.gmail.history_records
            if int(r["id"]) > start_id
        ]

        # Filter by historyTypes if specified
        # Map API historyType values → record keys (plural form used in records)
        _HTYPE_MAP = {
            "messageAdded": "messagesAdded",
            "messageDeleted": "messagesDeleted",
            "labelAdded": "labelsAdded",
            "labelRemoved": "labelsRemoved",
        }
        if htypes:
            record_keys = {_HTYPE_MAP.get(ht, ht) for ht in htypes}
            filtered = []
            for rec in all_records:
                keep = {}
                for key in record_keys:
                    if key in rec:
                        keep[key] = rec[key]
                if keep:
                    new_rec = {"id": rec["id"]}
                    new_rec.update(keep)
                    filtered.append(new_rec)
            all_records = filtered

        # pageToken: skip to the right offset
        if page_token_in:
            try:
                offset = int(page_token_in.split("_")[-1])
            except (ValueError, IndexError):
                offset = 0
            all_records = all_records[offset:]

        page_records = all_records[:max_results]
        next_page_token = None
        if len(all_records) > max_results:
            # Encode offset into the token
            base = 0
            if page_token_in:
                try:
                    base = int(page_token_in.split("_")[-1])
                except (ValueError, IndexError):
                    base = 0
            next_page_token = f"page_token_{base + max_results}"

        resp = {"historyId": self.__class__.gmail.current_history_id}
        if page_records:
            resp["history"] = page_records
        if next_page_token:
            resp["nextPageToken"] = next_page_token
        self._send_json(resp)

    def _handle_message(self, msg_id: str) -> None:
        msg = self.__class__.gmail.messages.get(msg_id)
        if msg is None or msg_id in self.__class__.gmail.deleted_message_ids:
            self._send_json({"error": {"code": 404, "message": "not found"}}, 404)
            return
        headers = [
            {"name": "Subject", "value": msg.subject},
            {"name": "From", "value": msg.from_},
            {"name": "To", "value": msg.to},
            {"name": "Date", "value": "Mon, 1 Jan 2026 00:00:00 +0000"},
            {"name": "Message-ID", "value": msg.message_id_header},
        ]
        self._send_json({
            "id": msg.id,
            "threadId": msg.thread_id,
            "labelIds": msg.label_ids,
            "internalDate": msg.internal_date,
            "sizeEstimate": msg.size_estimate,
            "payload": {"headers": headers},
        })

    def _handle_events(self, params: dict) -> None:
        """I3: honour maxResults, emit nextPageToken/nextSyncToken correctly.

        Rules (mirror real Google Calendar API):
        - Full sync (no syncToken, no pageToken): serve all events paginated by
          maxResults; non-final pages get nextPageToken only; final page gets
          nextSyncToken only.
        - Incremental (syncToken present): same pagination rules over the
          incremental event set.
        - pageToken: resume a pending page stored in cal._pages.
        - 410 for EXPIRED_SYNC_TOKEN; 400 for INVALID_PAGE_TOKEN.
        """
        sync_token = params.get("syncToken")
        page_token = params.get("pageToken")
        time_min = params.get("timeMin")
        try:
            max_results = int(params.get("maxResults", "2500"))
        except (ValueError, TypeError):
            max_results = 2500

        cal = self.__class__.calendar

        # ── 410 / 400 error paths ─────────────────────────────────────────────
        if sync_token == "EXPIRED_SYNC_TOKEN":
            self._send_json({"error": {"code": 410, "message": "sync token expired"}}, 410)
            return

        if page_token == "INVALID_PAGE_TOKEN":
            self._send_json({
                "error": {"code": 400, "message": "invalid page token",
                          "errors": [{"reason": "invalid", "domain": "calendar"}]}
            }, 400)
            return

        # ── Determine the full event list for this request ────────────────────
        if page_token:
            # Resume a pending page stored by a previous paginate() call
            evs = cal.pop_page(page_token)
            if evs is None:
                # Unknown page token → treat as invalid
                self._send_json({
                    "error": {"code": 400, "message": "invalid page token",
                              "errors": [{"reason": "invalid", "domain": "calendar"}]}
                }, 400)
                return
        elif sync_token:
            # Incremental sync — accept any synctoken_* token as valid
            evs = cal.consume_incr()
        else:
            # Full sync
            evs = cal.all_events_full(time_min)

        # ── Paginate ──────────────────────────────────────────────────────────
        page_evs, next_page_token = cal.paginate(evs, max_results)

        resp: dict = {
            "kind": "calendar#events",
            "summary": "primary",
            "items": [_ev_dict_local(e) for e in page_evs],
        }
        if next_page_token:
            resp["nextPageToken"] = next_page_token
        else:
            # Final page always gets nextSyncToken; bump it so each call is fresh
            cal.sync_token = f"synctoken_{len(cal.events)}_{cal._next_page_counter}"
            resp["nextSyncToken"] = cal.sync_token

        self._send_json(resp)

    def log_message(self, *_) -> None:
        pass


# ── FakeGoogleServer context manager ─────────────────────────────────────────

class FakeGoogleServer:
    """In-process loopback Google API fake.

    with FakeGoogleServer() as fake:
        creds = fake.make_credentials("alias")
        client = GoogleClient(creds)
        ...
    """

    def __init__(
        self,
        *,
        email: str = "test@example.com",
        rotate_tokens: bool = False,
        gmail_state: Optional[FakeGmailState] = None,
        calendar_state: Optional[FakeCalendarState] = None,
        granted_scopes: str = "",
    ) -> None:
        self.gmail = gmail_state or FakeGmailState()
        self.calendar = calendar_state or FakeCalendarState()
        self.tokens = FakeTokenStore(rotate_on_refresh=rotate_tokens)
        self.email = email
        self.granted_scopes = granted_scopes
        self._server: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeGoogleServer":
        self._server = http.server.HTTPServer(
            ("127.0.0.1", 0), _FakeGoogleHandler
        )
        self.port = self._server.server_address[1]
        # Set shared state on the handler class
        self._teardown_ev = threading.Event()
        _FakeGoogleHandler.gmail = self.gmail
        _FakeGoogleHandler.calendar = self.calendar
        _FakeGoogleHandler.tokens = self.tokens
        _FakeGoogleHandler.email_address = self.email
        _FakeGoogleHandler.granted_scopes = self.granted_scopes
        _FakeGoogleHandler.inject_401_on_next = False
        _FakeGoogleHandler.inject_500_on_next = False
        _FakeGoogleHandler.inject_429_on_next = False
        _FakeGoogleHandler.inject_timeout_on_next = False
        _FakeGoogleHandler.inject_malformed_on_next = False
        _FakeGoogleHandler.inject_redirect_to = None
        _FakeGoogleHandler._teardown_event = self._teardown_ev
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        self._server.serve_forever()

    def __exit__(self, *_) -> None:
        # Unblock any pending timeout injection before shutting down
        if hasattr(self, "_teardown_ev"):
            self._teardown_ev.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def make_credentials(self, alias: str) -> GoogleAccountCredentials:
        """Return in-memory credentials pointing at this fake server."""
        from olo.google.credentials import GoogleAccountCredentials
        from olo.google.oauth import GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO
        return GoogleAccountCredentials(
            alias=alias,
            client_id="fake_client_id",
            client_secret="fake_client_secret",
            refresh_token=self.tokens.refresh_token,
            granted_scopes=frozenset({GMAIL_METADATA_SCOPE, CALENDAR_EVENTS_OWNED_RO}),
            email_address=self.email,
            refresh_token_issued_at=datetime.now(UTC),
            oauth_publishing_status="testing",
            api_base_override=self.base_url,
        )

    def make_saved_credentials(
        self,
        alias: str,
        home: Path,
        monkeypatch,
    ) -> GoogleAccountCredentials:
        """Create credentials, save them in *home*, return the object."""
        monkeypatch.setenv("HERMES_HOME", str(home))
        from olo.google.credentials import save_credentials, credential_lock
        creds = self.make_credentials(alias)
        with credential_lock(alias):
            save_credentials(creds)
        return creds

    # ── Injection helpers ─────────────────────────────────────────────────────

    def inject_401(self) -> None:
        _FakeGoogleHandler.inject_401_on_next = True

    def inject_500(self) -> None:
        _FakeGoogleHandler.inject_500_on_next = True

    def inject_429(self) -> None:
        _FakeGoogleHandler.inject_429_on_next = True

    def inject_malformed(self) -> None:
        _FakeGoogleHandler.inject_malformed_on_next = True

    def inject_invalid_grant(self) -> None:
        self.tokens.invalid_grant = True

    def inject_cross_origin_redirect(self, target: str) -> None:
        _FakeGoogleHandler.inject_redirect_to = target

    def set_min_history_id(self, h_id: str) -> None:
        self.gmail.min_valid_history_id = h_id

    def set_page_size(self, n: int) -> None:
        """I3: set cal.page_size so _handle_events pages at n events."""
        self.calendar.page_size = n
