"""The seeder's write API over the offline fake Google server (tests only).

``FakeWriteApi`` implements ``acceptance_fixtures.WriteApi`` against the
mailbox and calendar of ``tests.olo.fake_google.FakeGoogleServer``, so a
seeded fixture travels through Olo's real Gmail and Calendar adapters exactly
as it would after a real write: RFC 822 bytes become a Gmail API payload,
label changes become history records, and events become full Calendar
resources (organizer.self, attendees, recurring occurrences, deletion stubs).
"""
from __future__ import annotations

import base64
import copy
import email
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import Optional

from tests.olo.fake_google import FakeGoogleServer

from .acceptance_fixtures import RUN_HEADER, ApiError


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def mime_to_payload(part: Message, part_id: str = "") -> dict:
    """A Gmail API ``payload`` for *part* (bodies decoded from their transfer encoding)."""
    out: dict = {"partId": part_id, "mimeType": part.get_content_type(),
                 "filename": part.get_filename() or "",
                 "headers": [{"name": k, "value": str(v)} for k, v in part.items()]}
    if part.get_content_type() == "message/rfc822":
        inner = part.get_payload()[0]
        out["body"] = {"size": 0}
        out["parts"] = [mime_to_payload(inner, f"{part_id}.0" if part_id else "0")]
    elif part.is_multipart():
        out["body"] = {"size": 0}
        out["parts"] = [mime_to_payload(p, f"{part_id}.{n}" if part_id else str(n))
                        for n, p in enumerate(part.get_payload())]
    else:
        data = part.get_payload(decode=True) or b""
        out["body"] = {"size": len(data), "data": _b64(data)}
    return out


class FakeWriteApi:
    def __init__(self, google: FakeGoogleServer, account: str) -> None:
        self.g, self.account = google, account
        self._raw: dict[str, bytes] = {}
        self._drafts: dict[str, str] = {}          # draft id -> its DRAFT message id
        self._n = 0
        self._tick = datetime(2026, 11, 1, tzinfo=UTC)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def _updated(self) -> str:
        self._tick += timedelta(seconds=1)
        return self._tick.isoformat().replace("+00:00", "Z")

    # Gmail
    def profile_email(self) -> str:
        return self.account

    def find_message(self, msgid: str) -> Optional[dict]:
        for m in self.g.gmail.messages.values():
            if m.message_id_header == msgid:
                return {"id": m.id, "threadId": m.thread_id}
        return None

    def _add(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict:
        msg = email.message_from_bytes(raw)
        mid = self._id("m")
        if not thread_id and msg.get("In-Reply-To"):
            parent = self.find_message(msg["In-Reply-To"])
            thread_id = parent["threadId"] if parent else None
        fm = self.g.gmail.add_message(mid, thread_id or mid, labels=list(labels), subject=msg["Subject"] or "",
                                      from_=msg["From"] or "", to=msg["To"] or "",
                                      payload=mime_to_payload(msg))
        fm.message_id_header = msg["Message-ID"] or ""
        self._raw[mid] = raw
        return {"id": mid, "threadId": fm.thread_id}

    def import_message(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict:
        return self._add(raw, labels, thread_id)

    def insert_message(self, raw: bytes, labels: list[str], thread_id: Optional[str]) -> dict:
        return self._add(raw, sorted(set(labels) | {"SENT"}), thread_id)

    def modify(self, msg_id: str, add: list[str], remove: list[str]) -> None:
        if msg_id not in self.g.gmail.messages:
            raise ApiError(404, "not found")
        self.g.gmail.move_labels(msg_id, add=list(add), remove=list(remove))

    def trash(self, msg_id: str) -> None:
        self.modify(msg_id, ["TRASH"], ["INBOX"])

    def untrash(self, msg_id: str) -> None:
        self.modify(msg_id, ["INBOX"], ["TRASH"])

    def delete_message(self, msg_id: str) -> None:
        msg = self.g.gmail.messages.get(msg_id)
        if msg is not None:
            self.g.gmail.delete_message(msg_id, stub_label_ids=list(msg.label_ids))

    def message_marker(self, msg_id: str) -> Optional[str]:
        if msg_id not in self.g.gmail.messages:
            return None
        return email.message_from_bytes(self._raw[msg_id]).get(RUN_HEADER)

    def create_draft(self, raw: bytes) -> str:
        draft = self._id("d")
        self._drafts[draft] = self._add(raw, ["DRAFT"], None)["id"]
        return draft

    def update_draft(self, draft_id: str, raw: bytes) -> None:
        self.delete_message(self._drafts[draft_id])
        self._drafts[draft_id] = self._add(raw, ["DRAFT"], None)["id"]

    def delete_draft(self, draft_id: str) -> None:
        self.delete_message(self._drafts.pop(draft_id))

    def send_draft(self, draft_id: str) -> dict:
        draft_msg = self._drafts.pop(draft_id)
        raw = self._raw[draft_msg]
        self.delete_message(draft_msg)
        return self._add(raw, ["SENT", "INBOX", "UNREAD"], None)

    # Calendar
    def _resource(self, event_id: str) -> Optional[dict]:
        ev = self.g.calendar.events.get(event_id)
        return copy.deepcopy(ev.resource) if ev and ev.resource else None

    def insert_event(self, resource: dict) -> dict:
        existing = self._resource(resource["id"])
        if existing:
            return existing
        res = copy.deepcopy(resource)
        res.update({"status": "confirmed", "iCalUID": f"{res['id']}@google.com", "sequence": 0,
                    "organizer": {"email": self.account, "self": True},
                    "creator": {"email": self.account, "self": True}, "updated": self._updated()})
        res["attendees"] = [{"email": a["email"], "responseStatus": "needsAction"}
                            for a in res.get("attendees", [])]
        self.g.calendar.put_event(res)
        return copy.deepcopy(res)

    def get_event(self, event_id: str) -> Optional[dict]:
        return self._resource(event_id)

    def _occurrences(self, master: dict) -> list[dict]:
        start = datetime.fromisoformat(master["start"]["dateTime"])
        end = datetime.fromisoformat(master["end"]["dateTime"])
        out = []
        for n in range(4):
            s, e = start + timedelta(weeks=n), end + timedelta(weeks=n)
            occ = {k: copy.deepcopy(v) for k, v in master.items() if k not in ("recurrence", "id")}
            occ.update({"id": f"{master['id']}_{s.strftime('%Y%m%dT%H%M%SZ')}",
                        "recurringEventId": master["id"],
                        "originalStartTime": {"dateTime": s.isoformat(), "timeZone": "UTC"},
                        "start": {"dateTime": s.isoformat(), "timeZone": "UTC"},
                        "end": {"dateTime": e.isoformat(), "timeZone": "UTC"}})
            out.append(occ)
        return out

    def instances(self, event_id: str) -> list[dict]:
        master = self._resource(event_id)
        return self._occurrences(master) if master else []

    def patch_event(self, event_id: str, patch: dict) -> dict:
        res = self._resource(event_id)
        if res is None and "_" in event_id:
            master = self._resource(event_id.split("_", 1)[0])
            res = next((o for o in self._occurrences(master) if o["id"] == event_id), None) if master else None
        if res is None:
            raise ApiError(404, "not found")
        if patch.get("status") == "cancelled" and res.get("recurringEventId"):
            res = {"id": res["id"], "status": "cancelled", "recurringEventId": res["recurringEventId"],
                   "originalStartTime": res["originalStartTime"]}
        else:
            res.update(copy.deepcopy(patch))
            res["sequence"] = res.get("sequence", 0) + (1 if "start" in patch else 0)
        res["updated"] = self._updated()
        self.g.calendar.put_event(res)
        return copy.deepcopy(res)

    def delete_event(self, event_id: str) -> None:
        if self._resource(event_id):
            self.g.calendar.put_event({"id": event_id, "status": "cancelled", "updated": self._updated()})
