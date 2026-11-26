"""Gmail API-shaped message payloads for tests.

Shapes follow ``users.messages.get(format=full)``: a payload tree with
``partId``, ``mimeType``, ``filename``, per-part ``headers`` (including
``Content-Type`` with its charset) and ``body`` holding ``size`` and the
part's bytes as base64url ``data`` (the API has already undone the
transfer encoding), or an ``attachmentId`` for attachments.
"""
from __future__ import annotations

import base64
from typing import Optional


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def text_part(text: str, *, mime: str = "text/plain", charset: str = "UTF-8",
              part_id: str = "0", transfer: str = "quoted-printable") -> dict:
    raw = text.encode(charset)
    return {"partId": part_id, "mimeType": mime, "filename": "",
            "headers": [{"name": "Content-Type", "value": f'{mime}; charset="{charset}"'},
                        {"name": "Content-Transfer-Encoding", "value": transfer}],
            "body": {"size": len(raw), "data": _b64(raw)}}


def attachment_part(filename: str, *, mime: str = "text/plain", part_id: str = "1",
                    inline_text: Optional[str] = None) -> dict:
    """An attachment; small text attachments can carry inline data in the API."""
    body: dict = {"attachmentId": f"att-{part_id}", "size": 120}
    if inline_text is not None:
        raw = inline_text.encode()
        body = {"size": len(raw), "data": _b64(raw)}
    return {"partId": part_id, "mimeType": mime, "filename": filename,
            "headers": [{"name": "Content-Type", "value": f'{mime}; name="{filename}"'},
                        {"name": "Content-Disposition", "value": f'attachment; filename="{filename}"'}],
            "body": body}


def multipart(mime: str, parts: list[dict], *, part_id: str = "") -> dict:
    return {"partId": part_id, "mimeType": mime, "filename": "",
            "headers": [{"name": "Content-Type", "value": f'{mime}; boundary="b{part_id or 0}"'}],
            "body": {"size": 0}, "parts": parts}


def message_headers(*, frm: str, to: str, subject: str, msg_id: str, cc: str = "") -> list[dict]:
    hdrs = [{"name": "From", "value": frm}, {"name": "To", "value": to},
            {"name": "Subject", "value": subject},
            {"name": "Date", "value": "Tue, 2 Jun 2026 09:00:00 +0000"},
            {"name": "Message-ID", "value": f"<{msg_id}@mail.example>"},
            {"name": "MIME-Version", "value": "1.0"}]
    if cc:
        hdrs.append({"name": "Cc", "value": cc})
    return hdrs


def with_headers(payload: dict, headers: list[dict]) -> dict:
    """The top-level payload carries the message headers (plus its own)."""
    return {**payload, "headers": headers + list(payload.get("headers") or [])}


def alternative(plain: str, html: str, *, charset: str = "UTF-8") -> dict:
    """multipart/alternative with text/plain and text/html (Gmail's default)."""
    return multipart("multipart/alternative", [
        text_part(plain, charset=charset, part_id="0"),
        text_part(html, mime="text/html", charset=charset, part_id="1")])


def raw_message(msg_id: str, payload: dict, *, labels: list[str], thread: str = "t1",
                internal_ms: str = "1780390800000") -> dict:
    """A whole users.messages.get(format=full) response."""
    return {"id": msg_id, "threadId": thread, "labelIds": labels, "snippet": "",
            "historyId": "1", "internalDate": internal_ms, "sizeEstimate": 2048,
            "payload": payload}
