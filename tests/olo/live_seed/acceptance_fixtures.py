"""Google acceptance fixtures, written through the separate seeder project.

The Google live acceptance harness (``scripts/olo/m3/google_acceptance``)
reads the dedicated test account through Olo's read-only client.  This module
creates the scenarios' test data in that same account through the seeder's
write-scoped client (``seeder.py``: its own Cloud project, token file and
account guard), so the two authorisations never mix.

Every fixture belongs to one run:

* a run ID (``accYYYYMMDDtHHMMSSxxxx``) appears in each subject or event
  title (``[OLO-<ID>] … - acc <run>``), in an ``X-Olo-Acceptance-Run`` header
  and in each event's private extended properties;
* Message-IDs and Calendar event IDs derive from (run, key), and each create
  first looks for the existing item, so re-running a phase creates nothing
  twice; a manifest records the phases already applied;
* writes are budgeted (``--max-writes``); Calendar writes send no updates;
  a draft is only ever sent to the account itself;
* the "customer" is a synthetic address on a reserved test domain: seeded
  mail is imported, never delivered, and no real person is involved;
* ``cleanup`` removes only the manifest's items whose run marker matches.

Scenarios whose assumption needs a genuine second party (a real invitation,
a real Outlook client) have no fixture here; the harness reports them as
``needs_identity``, never passed.

  python -m tests.olo.live_seed.acceptance_fixtures new-run
  python -m tests.olo.live_seed.acceptance_fixtures plan [--scenario ID ...]
  python -m tests.olo.live_seed.acceptance_fixtures status --run-id R
  python -m tests.olo.live_seed.acceptance_fixtures seed --run-id R --scenario G5 --phase 1
  python -m tests.olo.live_seed.acceptance_fixtures cleanup --run-id R [--permanent] [--dry-run]

``plan``, ``status`` and ``new-run`` are local.  ``seed`` and ``cleanup`` use
the seeder token (``python -m tests.olo.live_seed.seeder auth``).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

RUN_HEADER = "X-Olo-Acceptance-Run"
SEED_DOMAIN = "olo-seed.example.com"
SYNTHETIC_SUFFIXES = (".example.com", ".example", ".invalid", ".test")
DEFAULT_MAX_WRITES = 80
DEFAULT_MAX_READS = 200
_RUN_RE = re.compile(r"^acc\d{8}t\d{6}[0-9a-f]{4}$")


class AcceptanceSeedError(RuntimeError):
    """A guard refused the operation."""


class BudgetExceeded(AcceptanceSeedError):
    """The per-invocation request budget is spent."""


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"HTTP {status}: {detail[:200]}")
        self.status = status


# ── Identity of a run ─────────────────────────────────────────────────────────

def new_run_id(now: Optional[datetime] = None) -> str:
    return f"acc{(now or datetime.now(UTC)).strftime('%Y%m%dt%H%M%S')}{secrets.token_hex(2)}"


def check_run_id(run_id: str) -> str:
    if not _RUN_RE.match(run_id or ""):
        raise AcceptanceSeedError(f"invalid run id {run_id!r}: use `new-run`")
    return run_id


def check_synthetic(address: str) -> str:
    """The seeded 'customer' must be on a reserved test domain."""
    addr = (address or "").strip().lower()
    if "@" not in addr or not addr.split("@", 1)[1].endswith(SYNTHETIC_SUFFIXES):
        raise AcceptanceSeedError(
            f"the seeded customer {address!r} must be a synthetic address on a reserved test domain"
            f" (e.g. customer@{SEED_DOMAIN}); seeded mail must not name a real person")
    return addr


def message_id(run_id: str, key: str) -> str:
    return f"<olo-acc.{run_id}.{key}@{SEED_DOMAIN}>"


def event_id(run_id: str, key: str) -> str:
    """A valid Calendar event ID (base32hex characters) derived from (run, key)."""
    return "oloacc" + hashlib.sha256(f"{run_id}/{key}".encode()).hexdigest()[:32]


def tagged(scenario: str, run_id: str, text: str) -> str:
    return f"[OLO-{scenario}] {text} - acc {run_id}"


# ── MIME ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Mail:
    frm: str
    to: str
    subject: str
    text: Optional[str] = None
    html: Optional[str] = None
    charset: str = "utf-8"
    in_reply_to: Optional[str] = None
    attachments: tuple[tuple[str, str], ...] = ()      # (filename, text)
    forwarded: Optional[bytes] = None                   # an RFC 822 message
    date: Optional[datetime] = None


def build_mime(run_id: str, key: str, mail: Mail) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = mail.frm, mail.to, mail.subject
    msg["Message-ID"] = message_id(run_id, key)
    msg["Date"] = format_datetime(mail.date or datetime.now(UTC))
    msg[RUN_HEADER] = run_id
    if mail.in_reply_to:
        msg["In-Reply-To"] = msg["References"] = mail.in_reply_to
    if mail.html is not None and mail.text is None:
        msg.set_content(mail.html, subtype="html", charset=mail.charset)
    else:
        msg.set_content(mail.text or "", charset=mail.charset,
                        cte="quoted-printable" if mail.charset != "utf-8" else None)
    for filename, text in mail.attachments:
        msg.add_attachment(text.encode(), maintype="text", subtype="plain", filename=filename)
    if mail.forwarded is not None:
        from email import message_from_bytes
        from email.policy import default
        msg.add_attachment(message_from_bytes(mail.forwarded, policy=default))
    return msg.as_bytes()


# ── The write API (real Google, or the offline fake) ──────────────────────────

class WriteApi(Protocol):
    def profile_email(self) -> str: ...
    def find_message(self, msgid: str) -> Optional[dict]: ...
    def import_message(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict: ...
    def insert_message(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict: ...
    def modify(self, msg_id: str, add: list[str], remove: list[str]) -> None: ...
    def trash(self, msg_id: str) -> None: ...
    def untrash(self, msg_id: str) -> None: ...
    def delete_message(self, msg_id: str) -> None: ...
    def message_meta(self, msg_id: str) -> Optional[dict]: ...
    def create_draft(self, raw: bytes) -> str: ...
    def update_draft(self, draft_id: str, raw: bytes) -> None: ...
    def send_draft(self, draft_id: str) -> dict: ...
    def delete_draft(self, draft_id: str) -> None: ...
    def insert_event(self, resource: dict) -> dict: ...
    def get_event(self, event_id: str) -> Optional[dict]: ...
    def patch_event(self, event_id: str, patch: dict) -> dict: ...
    def delete_event(self, event_id: str) -> None: ...
    def instances(self, event_id: str) -> list[dict]: ...


Request = Callable[[str, str, Optional[bytes], str], dict]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class GoogleWriteApi:
    """The seeder's write path: Gmail import/insert/modify/drafts and Calendar
    events on the primary calendar, with ``sendUpdates=none`` on every event
    write.  *request* performs one HTTP call (injected in tests)."""

    gmail = "https://gmail.googleapis.com/gmail/v1/users/me"
    upload = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages"
    events = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

    def __init__(self, token: str, request: Optional[Request] = None) -> None:
        self._token = token
        self._request = request or self._urllib

    def _urllib(self, method: str, url: str, body: Optional[bytes], content_type: str) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"}
        if body is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, exc.read().decode("utf-8", "replace")) from exc
        return json.loads(raw) if raw else {}

    def _json(self, method: str, url: str, body: Optional[dict] = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        return self._request(method, url, data, "application/json")

    def _upload(self, url: str, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict:
        meta: dict[str, Any] = {"labelIds": labels}
        if thread_id:
            meta["threadId"] = thread_id
        boundary = "olo_acceptance_" + secrets.token_hex(6)
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n"
                ).encode() + raw + f"\r\n--{boundary}--".encode()
        return self._request("POST", url, body, f"multipart/related; boundary={boundary}")

    def profile_email(self) -> str:
        return self._json("GET", f"{self.gmail}/profile").get("emailAddress", "")

    def find_message(self, msgid: str) -> Optional[dict]:
        q = urllib.parse.urlencode({"q": f"rfc822msgid:{msgid.strip('<>')}", "includeSpamTrash": "true",
                                    "maxResults": "2"})
        found = self._json("GET", f"{self.gmail}/messages?{q}").get("messages") or []
        return found[0] if found else None

    def import_message(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict:
        never_spam = "false" if "SPAM" in labels else "true"
        url = (f"{self.upload}/import?uploadType=multipart&internalDateSource=dateHeader"
               f"&neverMarkSpam={never_spam}")
        return self._upload(url, raw, labels, thread_id)

    def insert_message(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict:
        return self._upload(f"{self.upload}?uploadType=multipart&internalDateSource=dateHeader",
                            raw, labels, thread_id)

    def modify(self, msg_id: str, add: list[str], remove: list[str]) -> None:
        self._json("POST", f"{self.gmail}/messages/{msg_id}/modify",
                   {"addLabelIds": add, "removeLabelIds": remove})

    def trash(self, msg_id: str) -> None:
        self._json("POST", f"{self.gmail}/messages/{msg_id}/trash")

    def untrash(self, msg_id: str) -> None:
        self._json("POST", f"{self.gmail}/messages/{msg_id}/untrash")

    def delete_message(self, msg_id: str) -> None:
        _ignore_gone(lambda: self._json("DELETE", f"{self.gmail}/messages/{msg_id}"))

    def message_meta(self, msg_id: str) -> Optional[dict]:
        """``{"marker": <run header or None>, "labels": [...]}``, None when gone (one read)."""
        try:
            msg = self._json("GET", f"{self.gmail}/messages/{msg_id}?format=metadata"
                                    f"&metadataHeaders={RUN_HEADER}")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        heads = (msg.get("payload") or {}).get("headers") or []
        return {"marker": next((h.get("value") for h in heads
                                if h.get("name", "").lower() == RUN_HEADER.lower()), None),
                "labels": list(msg.get("labelIds") or [])}

    def create_draft(self, raw: bytes) -> str:
        return self._json("POST", f"{self.gmail}/drafts", {"message": {"raw": _b64(raw)}})["id"]

    def update_draft(self, draft_id: str, raw: bytes) -> None:
        self._json("PUT", f"{self.gmail}/drafts/{draft_id}", {"id": draft_id, "message": {"raw": _b64(raw)}})

    def send_draft(self, draft_id: str) -> dict:
        return self._json("POST", f"{self.gmail}/drafts/send", {"id": draft_id})

    def delete_draft(self, draft_id: str) -> None:
        _ignore_gone(lambda: self._json("DELETE", f"{self.gmail}/drafts/{draft_id}"))

    def insert_event(self, resource: dict) -> dict:
        try:
            return self._json("POST", f"{self.events}?sendUpdates=none", resource)
        except ApiError as exc:
            if exc.status != 409:
                raise
            return self._json("GET", f"{self.events}/{resource['id']}")

    def get_event(self, event_id: str) -> Optional[dict]:
        try:
            return self._json("GET", f"{self.events}/{event_id}")
        except ApiError as exc:
            if exc.status in (404, 410):
                return None
            raise

    def patch_event(self, event_id: str, patch: dict) -> dict:
        return self._json("PATCH", f"{self.events}/{event_id}?sendUpdates=none", patch)

    def delete_event(self, event_id: str) -> None:
        _ignore_gone(lambda: self._json("DELETE", f"{self.events}/{event_id}?sendUpdates=none"))

    def instances(self, event_id: str) -> list[dict]:
        return self._json("GET", f"{self.events}/{event_id}/instances?maxResults=10").get("items") or []


def _ignore_gone(call: Callable[[], Any]) -> None:
    try:
        call()
    except ApiError as exc:
        if exc.status not in (404, 410):
            raise


class Budgeted:
    """Counts every call to *api* and refuses past the budget."""

    WRITES = {"import_message", "insert_message", "modify", "trash", "untrash", "delete_message",
              "create_draft", "update_draft", "send_draft", "delete_draft", "insert_event", "patch_event",
              "delete_event"}

    def __init__(self, api: WriteApi, max_writes: int, max_reads: int = DEFAULT_MAX_READS) -> None:
        self._api, self.max_writes, self.max_reads = api, max_writes, max_reads
        self.writes = self.reads = 0

    def __getattr__(self, name: str):
        target = getattr(self._api, name)

        def counted(*args, **kwargs):
            if name in self.WRITES:
                if self.writes >= self.max_writes:
                    raise BudgetExceeded(f"write budget of {self.max_writes} spent")
                self.writes += 1
            else:
                if self.reads >= self.max_reads:
                    raise BudgetExceeded(f"read budget of {self.max_reads} spent")
                self.reads += 1
            return target(*args, **kwargs)
        return counted


# ── Manifest ──────────────────────────────────────────────────────────────────

def manifest_path() -> Path:
    return Path(os.environ.get("OLO_SEEDER_ACCEPTANCE_STATE") or Path.home() / ".olo_seeder_acceptance.json")


def load_manifest(path: Optional[Path] = None) -> dict:
    p = path or manifest_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"runs": {}}


def save_manifest(data: dict, path: Optional[Path] = None) -> None:
    p = path or manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    tmp.replace(p)


# ── The seeding session ───────────────────────────────────────────────────────

@dataclass
class Session:
    api: Any                       # a WriteApi (usually Budgeted)
    run_id: str
    account: str
    customer: str
    run: dict                      # this run's manifest entry
    evidence: Optional[Path] = None
    scenario: str = ""
    phase: int = 0
    log: list[dict] = field(default_factory=list)

    # items
    def _items(self) -> dict:
        return self.run["scenarios"].setdefault(self.scenario, {"phases_done": [], "items": {}})["items"]

    def item(self, key: str) -> dict:
        return self._items()[key]

    def _record(self, op: str, key: str, ref: str, result: str) -> None:
        entry = {"at": datetime.now(UTC).isoformat(), "run": self.run_id, "scenario": self.scenario,
                 "phase": self.phase, "op": op, "key": key, "ref": ref, "result": result}
        self.log.append(entry)
        if self.evidence is not None:
            self.evidence.mkdir(parents=True, exist_ok=True)
            with (self.evidence / "fixtures.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")

    @property
    def base(self) -> datetime:
        """Event times: 15:00 UTC, three days after the run was created."""
        created = datetime.fromisoformat(self.run["created_at"])
        return (created + timedelta(days=3)).replace(hour=15, minute=0, second=0, microsecond=0)

    # Gmail
    def received(self, key: str, mail: Mail, labels: Optional[list[str]] = None,
                 thread_of: Optional[str] = None) -> dict:
        return self._message(key, mail, labels or ["INBOX", "UNREAD"], thread_of, self.api.import_message)

    def sent(self, key: str, mail: Mail, thread_of: Optional[str] = None) -> dict:
        return self._message(key, mail, ["SENT"], thread_of, self.api.insert_message)

    def _message(self, key, mail, labels, thread_of, write) -> dict:
        mid = message_id(self.run_id, f"{self.scenario}.{key}")
        found = self.api.find_message(mid)
        if found:
            item = {"type": "message", "id": found["id"], "thread": found.get("threadId"), "msgid": mid}
            self._items()[key] = item
            self._record("message", key, item["id"], "existing")
            return item
        thread = self.item(thread_of)["thread"] if thread_of else None
        res = write(build_mime(self.run_id, f"{self.scenario}.{key}", mail), labels, thread)
        item = {"type": "message", "id": res["id"], "thread": res.get("threadId"), "msgid": mid}
        self._items()[key] = item
        self._record("message", key, item["id"], "created")
        return item

    def labels(self, key: str, add: list[str] = (), remove: list[str] = ()) -> None:
        self.api.modify(self.item(key)["id"], list(add), list(remove))
        self._record("modify", key, self.item(key)["id"], f"+{list(add)} -{list(remove)}")

    def trash(self, key: str) -> None:
        self.api.trash(self.item(key)["id"])
        self._record("trash", key, self.item(key)["id"], "done")

    def untrash(self, key: str) -> None:
        self.api.untrash(self.item(key)["id"])
        self._record("untrash", key, self.item(key)["id"], "done")

    def delete_forever(self, key: str) -> None:
        self.api.delete_message(self.item(key)["id"])
        self._record("delete", key, self.item(key)["id"], "done")

    # Calendar
    def event(self, key: str, summary: str, start: datetime, *, minutes: int = 60,
              attendees: tuple[str, ...] = (), recurrence: tuple[str, ...] = ()) -> dict:
        eid = event_id(self.run_id, f"{self.scenario}.{key}")
        res = {"id": eid, "summary": summary, "description": f"Olo acceptance fixture ({self.run_id})",
               "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
               "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(), "timeZone": "UTC"},
               "extendedProperties": {"private": {"olo_acceptance_run": self.run_id,
                                                  "olo_acceptance_key": f"{self.scenario}.{key}"}}}
        if attendees:
            res["attendees"] = [{"email": a} for a in attendees]
        if recurrence:
            res["recurrence"] = list(recurrence)
        existing = self.api.get_event(eid)
        got = existing or self.api.insert_event(res)
        self._items()[key] = {"type": "event", "id": got["id"], "start": start.isoformat()}
        self._record("event", key, got["id"], "existing" if existing else "created")
        return got

    def move(self, key: str, start: datetime, minutes: int = 60) -> None:
        eid = self.item(key)["id"]
        self.api.patch_event(eid, {"start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                                   "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat(),
                                           "timeZone": "UTC"}})
        self._record("move", key, eid, start.isoformat())

    def retitle(self, key: str, summary: str, description: str) -> None:
        eid = self.item(key)["id"]
        self.api.patch_event(eid, {"summary": summary, "description": description})
        self._record("retitle", key, eid, "done")

    def delete_event(self, key: str) -> None:
        eid = self.item(key)["id"]
        self.api.delete_event(eid)
        self._record("delete_event", key, eid, "done")


# ── Scenario phases ───────────────────────────────────────────────────────────
# Each phase is applied once per run; the harness syncs between phases.

def _plain(s: Session, scenario: str, text: str, body: str, *, frm: Optional[str] = None) -> Mail:
    return Mail(frm=frm or f"Customer <{s.customer}>", to=s.account,
                subject=tagged(scenario, s.run_id, text), text=body)


def g1(s: Session) -> None:
    question = "Could you confirm order 4471 ships on Friday?"
    s.received("q", _plain(s, "G1", "order question", f"Hi,\n\n{question}\n\nThanks"))
    s.sent("reply", Mail(frm=s.account, to=s.customer, subject="Re: " + tagged("G1", s.run_id, "order question"),
                         text=f"Yes, it ships on Friday.\n\nOn Mon, Customer <{s.customer}> wrote:\n> {question}\n",
                         in_reply_to=s.item("q")["msgid"]), thread_of="q")


def g3(s: Session) -> None:
    s.received("html", Mail(frm=f"Customer <{s.customer}>", to=s.account,
                            subject=tagged("G3", s.run_id, "newsletter"),
                            html="<html><head><style>p{font-family:Arial}</style></head><body>"
                                 "<p style='font-family:Arial'>Café déjà vu</p><script>x=1</script>"
                                 "</body></html>"))
    s.received("latin1", Mail(frm=f"Customer <{s.customer}>", to=s.account,
                              subject=tagged("G3", s.run_id, "latin-1 note"),
                              text="Café déjà vu\n", charset="iso-8859-1"))


def g4(s: Session) -> None:
    fwd = build_mime(s.run_id, "G4.inner", Mail(frm=f"Supplier <supplier@{SEED_DOMAIN}>", to=s.customer,
                                                subject="Original quote", text="The original quote."))
    s.received("att", Mail(frm=f"Customer <{s.customer}>", to=s.account,
                           subject=tagged("G4", s.run_id, "documents"), text="Please see the attached notes.",
                           attachments=(("notes.txt", "OLO-ATTACHMENT-TEXT"),), forwarded=fwd))


def g5_spam(s: Session) -> None:
    s.received("m", _plain(s, "G5", "please help", "Hi, I need help with my order."), labels=["SPAM"])


def g5_not_spam(s: Session) -> None:
    s.labels("m", add=["INBOX"], remove=["SPAM"])


def g6_add(s: Session) -> None:
    s.received("a", _plain(s, "G6a", "restore me", "Keep this message."))
    s.received("b", _plain(s, "G6b", "delete me", "Delete this message."))


def g6_trash(s: Session) -> None:
    s.trash("a")
    s.trash("b")


def g6_restore_delete(s: Session) -> None:
    s.untrash("a")
    s.delete_forever("b")


def g7_draft(s: Session) -> None:
    items = s._items()
    if "draft" in items:
        return
    mail = Mail(frm=s.account, to=s.account, subject=tagged("G7", s.run_id, "note to self"), text="Draft 1")
    draft = s.api.create_draft(build_mime(s.run_id, "G7.draft", mail))
    s.api.update_draft(draft, build_mime(s.run_id, "G7.draft", replace(mail, text="Draft 2")))
    items["draft"] = {"type": "draft", "id": draft}
    s._record("draft", "draft", draft, "created, edited")


def g7_send(s: Session) -> None:
    sent = s.api.send_draft(s.item("draft")["id"])
    s._items()["sent"] = {"type": "message", "id": sent["id"], "thread": sent.get("threadId"),
                          "msgid": message_id(s.run_id, "G7.draft")}
    s._record("send_draft", "draft", sent["id"], "sent to self")


def g8_add(s: Session) -> None:
    s.received("m", _plain(s, "G8", "status update", "Your parcel left the depot."))


def g8_labels(s: Session) -> None:
    s.labels("m", remove=["INBOX"])
    s.labels("m", add=["STARRED"])
    s.labels("m", remove=["UNREAD"])


def g9(s: Session) -> None:
    """12 messages, each followed by signal-less draft history (create, two
    saves, discard), so one history page holds fewer signals than the job's
    limit and Olo must follow ``nextPageToken`` (as the M2b H1 fixture did)."""
    items = s._items()
    for n in range(1, 13):
        s.received(f"m{n}", _plain(s, "G9", f"bulk {n}", f"Bulk message {n} of 12."))
        if f"churn{n}" in items:
            continue
        churn = Mail(frm=s.account, to=s.account, subject=f"draft churn {n} - acc {s.run_id}", text="draft")
        draft = s.api.create_draft(build_mime(s.run_id, f"G9.churn{n}", churn))
        for rev in (1, 2):
            s.api.update_draft(draft, build_mime(s.run_id, f"G9.churn{n}", replace(churn, text=f"draft {rev}")))
        s.api.delete_draft(draft)
        items[f"churn{n}"] = {"type": "draft", "id": draft}
        s._record("draft_churn", f"churn{n}", draft, "created, saved twice, discarded")


def c1(s: Session) -> None:
    s.event("e", tagged("C1", s.run_id, "site visit"), s.base, attendees=(s.customer,))


def c2_create(s: Session) -> None:
    s.event("e", tagged("C2", s.run_id, "inspection"), s.base + timedelta(days=1), attendees=(s.customer,))


def c2_other_day(s: Session) -> None:
    s.move("e", s.base + timedelta(days=2))


def c2_same_day(s: Session) -> None:
    s.move("e", s.base + timedelta(days=2, hours=2))


def c3_create(s: Session) -> None:
    s.event("e", tagged("C3", s.run_id, "planning call"), s.base + timedelta(days=3), attendees=(s.customer,))


def c3_retitle(s: Session) -> None:
    s.retitle("e", tagged("C3", s.run_id, "planning call (agenda updated)"), "Updated agenda.")


def c5_series(s: Session) -> None:
    s.event("e", tagged("C5", s.run_id, "weekly check-in"), s.base + timedelta(days=4),
            attendees=(s.customer,), recurrence=("RRULE:FREQ=WEEKLY;COUNT=4",))


def c5_exceptions(s: Session) -> None:
    occurrences = s.api.instances(s.item("e")["id"])
    if len(occurrences) < 3:
        raise AcceptanceSeedError(f"C5: expected 4 occurrences, Google returned {len(occurrences)}")
    moved, cancelled = occurrences[1], occurrences[2]
    start = datetime.fromisoformat(moved["start"]["dateTime"]) + timedelta(hours=1)
    s.api.patch_event(moved["id"], {"start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                                    "end": {"dateTime": (start + timedelta(hours=1)).isoformat(),
                                            "timeZone": "UTC"}})
    s.api.patch_event(cancelled["id"], {"status": "cancelled"})
    s._items()["moved"] = {"type": "instance", "id": moved["id"]}
    s._items()["cancelled"] = {"type": "instance", "id": cancelled["id"]}
    s._record("exceptions", "e", s.item("e")["id"], "moved one occurrence, cancelled another")


def c6_create(s: Session) -> None:
    s.event("e", tagged("C6", s.run_id, "delivery slot"), s.base + timedelta(days=5), attendees=(s.customer,))


def c6_delete(s: Session) -> None:
    s.delete_event("e")


def c8(s: Session) -> None:
    for n in range(1, 13):
        s.event(f"e{n}", tagged("C8", s.run_id, f"slot {n}"), s.base + timedelta(days=6, hours=n % 8))


def l2(s: Session) -> None:
    start = s.base + timedelta(days=7)
    s.event("e", tagged("L2", s.run_id, "warehouse walkthrough"), start, attendees=(s.customer,))
    s.sent("confirm", Mail(frm=s.account, to=s.customer, subject=tagged("L2", s.run_id, "our walkthrough"),
                           text=f"Confirming our walkthrough on {start:%A %d %B} at {start:%H:%M} UTC."))


#: Scenario → ordered phases.  Scenarios absent here need no Google data
#: (local, operator or installed-path actions) or a genuine second identity.
PHASES: dict[str, tuple[Callable[[Session], None], ...]] = {
    "G1": (g1,), "G3": (g3,), "G4": (g4,), "G5": (g5_spam, g5_not_spam),
    "G6": (g6_add, g6_trash, g6_restore_delete), "G7": (g7_draft, g7_send), "G8": (g8_add, g8_labels),
    "G9": (g9,), "C1": (c1,), "C2": (c2_create, c2_other_day, c2_same_day), "C3": (c3_create, c3_retitle),
    "C5": (c5_series, c5_exceptions), "C6": (c6_create, c6_delete), "C8": (c8,), "L2": (l2,),
}


def new_run(manifest: dict, run_id: str, account: str, customer: str, now: Optional[datetime] = None) -> dict:
    runs = manifest.setdefault("runs", {})
    if run_id not in runs:
        runs[run_id] = {"created_at": (now or datetime.now(UTC)).isoformat(),
                        "account_fp": hashlib.sha256(account.lower().encode()).hexdigest()[:10],
                        "customer": customer, "scenarios": {}}
    run = runs[run_id]
    if run["customer"] != customer:
        raise AcceptanceSeedError("this run was seeded with a different customer address")
    if run["account_fp"] != hashlib.sha256(account.lower().encode()).hexdigest()[:10]:
        raise AcceptanceSeedError("this run belongs to another account")
    return run


def seed_phase(session: Session, scenario: str, phase: int, *, force: bool = False) -> str:
    """Apply one phase once; ``"done"`` or ``"already done"``."""
    phases = PHASES.get(scenario)
    if not phases:
        raise AcceptanceSeedError(f"{scenario} has no seeder fixture (see `plan`)")
    if not 1 <= phase <= len(phases):
        raise AcceptanceSeedError(f"{scenario} has {len(phases)} phase(s)")
    state = session.run["scenarios"].setdefault(scenario, {"phases_done": [], "items": {}})
    missing = [p for p in range(1, phase) if p not in state["phases_done"]]
    if missing:
        raise AcceptanceSeedError(f"{scenario}: apply phase(s) {missing} first (with a sync between)")
    if phase in state["phases_done"] and not force:
        return "already done"
    session.scenario, session.phase = scenario, phase
    phases[phase - 1](session)
    state["phases_done"] = sorted(set(state["phases_done"]) | {phase})
    return "done"


def cleanup(api: Any, run_id: str, run: dict, *, permanent: bool = False, dry_run: bool = False) -> list[dict]:
    """Remove this run's items whose marker matches the run (nothing else)."""
    report = []
    for scenario, state in run["scenarios"].items():
        for key, item in state["items"].items():
            report.append(_cleanup_item(api, run_id, scenario, key, item, permanent, dry_run))
    return report


def _cleanup_item(api, run_id, scenario, key, item, permanent, dry_run) -> dict:
    out = {"scenario": scenario, "key": key, "type": item["type"], "ref": item["id"]}
    if item["type"] == "instance":
        return {**out, "result": "removed with its series"}
    if item["type"] == "event":
        ev = api.get_event(item["id"])
        if not ev or ev.get("status") == "cancelled":
            return {**out, "result": "gone"}
        props = (ev.get("extendedProperties") or {}).get("private") or {}
        if props.get("olo_acceptance_run") != run_id:
            return {**out, "result": "skipped: run marker differs"}
        if not dry_run:
            api.delete_event(item["id"])
        return {**out, "result": "would delete" if dry_run else "deleted"}
    if item["type"] == "draft":
        return {**out, "result": "skipped: drafts are sent or discarded by G7"}
    meta = api.message_meta(item["id"])
    if meta is None:
        return {**out, "result": "gone"}
    if meta["marker"] != run_id:
        return {**out, "result": "skipped: run marker differs"}
    if not permanent and "TRASH" in meta["labels"]:
        # Already trashed (an earlier cleanup, or the scenario itself): nothing
        # to do, and a dry run must not claim it would trash it again.
        return {**out, "result": "already in trash"}
    if not dry_run:
        (api.delete_message if permanent else api.trash)(item["id"])
    action = "deleted" if permanent else "trashed"
    return {**out, "result": f"would be {action}" if dry_run else action}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _open_real(max_writes: int) -> tuple[Budgeted, str]:
    """The seeder token for the guarded account (contacts Google: token refresh)."""
    from tests.olo.live_seed import seeder
    tok = seeder._load_token() or {}
    seeder._assert_allowed_account(tok.get("email", ""))
    expected = os.environ.get("OLO_ACCEPTANCE_ACCOUNT", "").strip().lower()
    if expected and expected != tok.get("email", "").strip().lower():
        raise AcceptanceSeedError("the seeder account is not OLO_ACCEPTANCE_ACCOUNT")
    api = Budgeted(GoogleWriteApi(seeder._get_access_token()), max_writes)
    if api.profile_email().strip().lower() != tok.get("email", "").strip().lower():
        raise AcceptanceSeedError("the seeder token belongs to another mailbox")
    return api, tok["email"]


def _plan(scenarios: list[str]) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "olo" / "m3"))
    from google_acceptance.scenarios import SCENARIOS
    for sc in SCENARIOS:
        if scenarios and sc.id not in scenarios:
            continue
        phases = PHASES.get(sc.id, ())
        steps = " -> ".join(f"seed {sc.id} {n} -> sync" for n in range(1, len(phases) + 1)) or sc.action
        by = "seeder" if phases else ("real-identity" if sc.evidence == "second_identity" else "operator")
        print(f"{sc.id:4s} {sc.evidence:15s} {steps}  ->  run.py action {sc.id} --by {by}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="acceptance_fixtures")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new-run")
    pl = sub.add_parser("plan")
    pl.add_argument("--scenario", action="append", default=[])
    st = sub.add_parser("status")
    st.add_argument("--run-id", required=True)
    sd = sub.add_parser("seed")
    sd.add_argument("--run-id", required=True)
    sd.add_argument("--scenario", required=True)
    sd.add_argument("--phase", type=int, default=1)
    sd.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES)
    sd.add_argument("--force", action="store_true", help="re-apply a phase already applied")
    cl = sub.add_parser("cleanup")
    cl.add_argument("--run-id", required=True)
    cl.add_argument("--permanent", action="store_true", help="delete messages instead of trashing them")
    cl.add_argument("--dry-run", action="store_true")
    cl.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES)
    args = ap.parse_args(argv)
    if args.cmd == "new-run":
        print(new_run_id())
        return 0
    if args.cmd == "plan":
        _plan(args.scenario)
        return 0
    run_id = check_run_id(args.run_id)
    manifest = load_manifest()
    if args.cmd == "status":
        print(json.dumps(manifest.get("runs", {}).get(run_id, {}), indent=2))
        return 0
    customer = check_synthetic(os.environ.get("OLO_ACCEPTANCE_CUSTOMER") or f"customer@{SEED_DOMAIN}")
    api, account = _open_real(args.max_writes)
    run = new_run(manifest, run_id, account, customer)
    if args.cmd == "cleanup":
        report = cleanup(api, run_id, run, permanent=args.permanent, dry_run=args.dry_run)
        print(json.dumps(report, indent=2))
        return 0
    evidence = Path(os.environ["OLO_ACCEPTANCE_EVIDENCE"]) if os.environ.get("OLO_ACCEPTANCE_EVIDENCE") else None
    session = Session(api, run_id, account, customer, run, evidence)
    try:
        result = seed_phase(session, args.scenario, args.phase, force=args.force)
    finally:
        save_manifest(manifest)
    print(f"{args.scenario} phase {args.phase}: {result} ({api.writes} writes, {api.reads} reads)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
