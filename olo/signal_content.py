"""olo/signal_content.py — the one preparation path from a stored signal to text.

Every consumer of signal text goes through this module:

* the commitment extractor (``olo.extractor``) — synthetic corpus payloads and
  production signals alike;
* the Hindsight item builder (``olo.m3.feeder.build_hindsight_items``) — for
  reading and verifying content blobs;
* the signal worker — via ``load_prepared_signal`` for a stored signal.

What it guarantees
------------------
Real content   — Gmail metadata signals (``email.sent`` / ``email.received``)
                 carry the subject in ``headers.subject``; full-content signals
                 (``email.content``) carry the message in a content blob, the
                 canonical Gmail JSON document.  Bodies are rendered from its
                 ``text/plain`` parts (``text/html`` only as a fallback).
Trusted actor  — business vs customer comes only from trusted direction
                 metadata: the Gmail SENT label (``email.sent`` or
                 ``direction='sent'``) or a call's ``direction``.  Unknown
                 direction is never defaulted.
Exact evidence — ``full_text`` is a deterministic rendering of the stored
                 content and ``content_sha256`` names the verified blob it came
                 from; the blob itself is never modified.
Authored text  — ``authored_text`` drops ``>``-quoted lines and everything from
                 the first reply/forward header; only it may support an
                 authoritative statement.

No side effects at import time; no network; blob reads are injected.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from olo.adapters.gmail_full import GMAIL_MSG_MEDIA_TYPE
from olo.signals import format_utc

__all__ = [
    "PREP_CONTENT_ERROR",
    "PREP_MISSING_CONTENT",
    "PREP_NO_DIRECTION",
    "PREP_NO_TEXT",
    "PREP_READY",
    "PREP_UNSUPPORTED_KIND",
    "ContentLoad",
    "PreparedSignal",
    "authored_text",
    "load_content",
    "load_prepared_signal",
    "prepare_signal",
    "trusted_actor_role",
]

EMAIL_KINDS: frozenset[str] = frozenset({"email.sent", "email.received", "email.content"})
TRANSCRIPT_KIND = "call.transcript"
PROSE_KINDS: frozenset[str] = EMAIL_KINDS | {TRANSCRIPT_KIND}

PREP_READY            = "ready"             # text and trusted actor available
PREP_NO_TEXT          = "no_text"           # signal genuinely carries no text
PREP_MISSING_CONTENT  = "missing_content"   # referenced blob absent (integrity failure)
PREP_CONTENT_ERROR    = "content_error"     # blob corrupt or unreadable
PREP_NO_DIRECTION     = "no_direction"      # actor cannot be trusted
PREP_UNSUPPORTED_KIND = "unsupported_kind"  # not a prose signal

BlobReader = Callable[[str], Optional[bytes]]


# ── Content blobs ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContentLoad:
    """Result of reading a signal's content blob.

    status: 'ok' | 'no_ref' | 'missing' | 'corrupt' | 'unreadable'.
    ``text`` is the rendered body (and ``subject`` for Gmail documents).
    """
    status: str
    sha256: str = ""
    media_type: str = ""
    raw: Optional[bytes] = None
    text: str = ""
    subject: str = ""
    truncated: bool = False

    @property
    def rendered(self) -> str:
        """Item text: plain blobs verbatim; Gmail documents as subject + body."""
        if self.media_type == GMAIL_MSG_MEDIA_TYPE:
            return _compose_email(self.subject, self.text)
        return self.text


def _content_ref(payload: dict) -> tuple[str, str]:
    """(sha256, media_type) of the signal's primary content blob, or ('', '')."""
    sha = payload.get("content_sha256") or ""
    media = ""
    refs = payload.get("content_refs") or []
    if refs and isinstance(refs[0], dict):
        sha = sha or refs[0].get("sha256", "")
        media = refs[0].get("media_type", "") or ""
    return (sha if isinstance(sha, str) else ""), media


def load_content(payload: dict, blob_reader: Optional[BlobReader]) -> ContentLoad:
    """Read, verify (sha256) and render a signal's content blob.

    Plain-text blobs (synthetic and transcript content) render to their exact
    UTF-8 text.  Gmail documents render to subject + body.
    """
    sha, media = _content_ref(payload)
    if not sha:
        return ContentLoad("no_ref")
    raw = blob_reader(sha) if blob_reader is not None else None
    if raw is None:
        return ContentLoad("missing", sha, media)
    if hashlib.sha256(raw).hexdigest() != sha:
        return ContentLoad("corrupt", sha, media, raw)
    try:
        if media == GMAIL_MSG_MEDIA_TYPE:
            subject, body, truncated = _render_gmail_doc(json.loads(raw.decode("utf-8")))
            return ContentLoad("ok", sha, media, raw, body, subject, truncated)
        return ContentLoad("ok", sha, media, raw, raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError, AttributeError):
        return ContentLoad("unreadable", sha, media, raw)


def _b64url_text(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _walk_parts(part: dict) -> list[dict]:
    out = [part]
    for sub in part.get("parts") or []:
        if isinstance(sub, dict):
            out.extend(_walk_parts(sub))
    return out


_BLOCKQUOTE_RE = re.compile(r"<blockquote\b[^>]*>(.*?)</blockquote>", re.IGNORECASE | re.DOTALL)
_BREAK_TAG_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(markup: str) -> str:
    """Plain text from HTML; blockquoted content becomes ``>``-quoted lines."""
    def quote(m: re.Match[str]) -> str:
        inner = _html_to_text(m.group(1))
        return "\n" + "\n".join("> " + line for line in inner.splitlines()) + "\n"

    text = _BLOCKQUOTE_RE.sub(quote, markup)
    text = _BREAK_TAG_RE.sub("\n", text)
    text = html.unescape(_TAG_RE.sub("", text))
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _render_gmail_doc(doc: dict) -> tuple[str, str, bool]:
    """(subject, body, truncated) from a canonical Gmail message document."""
    payload = doc.get("payload") or {}
    subject = ""
    for h in payload.get("headers") or []:
        if isinstance(h, dict) and (h.get("name") or "").lower() == "subject":
            subject = (h.get("value") or "").strip()
            break
    parts = _walk_parts(payload)
    plain = [p for p in parts if (p.get("mimeType") or "").lower() == "text/plain"
             and (p.get("body") or {}).get("data")]
    htmls = [p for p in parts if (p.get("mimeType") or "").lower() == "text/html"
             and (p.get("body") or {}).get("data")]
    if plain:
        body = "\n".join(_b64url_text(p["body"]["data"]) for p in plain)
    elif htmls:
        body = "\n".join(_html_to_text(_b64url_text(p["body"]["data"])) for p in htmls)
    else:
        body = ""
    body = body.replace("\r\n", "\n").replace("\r", "\n").strip()
    return subject, body, bool(doc.get("truncated"))


# ── Authored text (quoted replies excluded) ───────────────────────────────────

#: Lines that start a quoted/forwarded block in an email body.  Everything
#: from the first such line onward is not authored by the sender.
_REPLY_HEADER_RE = re.compile(
    r"^\s*(?:"
    r"On\s.+\bwrote:\s*$"                         # On Mon, Bob <b@x> wrote:
    r"|-{2,}\s*(?:Original|Forwarded)\s+Message\s*-{2,}"
    r"|-{2,}\s*Forwarded message\s*-{2,}"
    r"|_{5,}\s*$"                                  # Outlook separator
    r"|From:\s.+"                                  # From: header of a quoted mail
    r")",
    re.IGNORECASE,
)


def authored_text(kind: str, full_text: str) -> str:
    """The part of the signal written by its author (quoted replies removed).

    For email kinds: drops ``>``-quoted lines and everything from the first
    reply/forward header line onward.  Other kinds are returned unchanged.
    """
    if kind not in EMAIL_KINDS:
        return full_text
    kept: list[str] = []
    for line in full_text.splitlines():
        if _REPLY_HEADER_RE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


# ── Trusted actor ─────────────────────────────────────────────────────────────

def trusted_actor_role(kind: str, payload: dict) -> Optional[str]:
    """'business' | 'customer' from trusted direction metadata, else None.

    email.sent → business (Gmail SENT label); email.received → customer;
    email.content → its ``direction``; call.transcript → its ``direction``
    (outbound → business, inbound → customer).  Never defaulted.
    """
    if kind == "email.sent":
        return "business"
    if kind == "email.received":
        return "customer"
    direction = str(payload.get("direction", "")).strip().lower()
    if kind == "email.content":
        return {"sent": "business", "received": "customer"}.get(direction)
    if kind == TRANSCRIPT_KIND:
        return {"outbound": "business", "inbound": "customer"}.get(direction)
    return None


# ── Prepared signal ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PreparedSignal:
    """A signal ready (or explicitly not ready) for commitment extraction.

    ``full_text`` is the verification text; ``authored_text`` the subset that
    may support an authoritative statement.  ``occurred_at`` is canonical UTC
    (``format_utc``) or None when the signal carries no trusted timestamp.
    """
    kind: str
    status: str
    full_text: str = ""
    authored_text: str = ""
    actor_role: Optional[str] = None
    thread_id: Optional[str] = None
    occurred_at: Optional[str] = None
    content_sha256: Optional[str] = None
    reason: str = ""

    @property
    def ready(self) -> bool:
        return self.status == PREP_READY


def _compose_email(subject: str, body: str) -> str:
    parts = [f"Subject: {subject}"] if subject else []
    if body:
        parts.append(body)
    return "\n\n".join(parts)


def _utc(value: object) -> Optional[str]:
    """Canonical UTC string for an aware datetime/ISO string, else None."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None
    return format_utc(dt)


def _thread_id(payload: dict) -> Optional[str]:
    for key in ("thread_id", "message_id"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def prepare_signal(
    kind: str,
    payload: dict,
    *,
    subject: Optional[str] = None,
    occurred_at: object = None,
    blob_reader: Optional[BlobReader] = None,
) -> PreparedSignal:
    """Prepare one signal for extraction.

    Text sources, in order: inline ``subject``/``body`` (synthetic corpus
    shape) or ``transcript``; otherwise the content blob via *blob_reader*;
    the subject falls back to ``headers.subject`` and then *subject* (the
    stored signal subject).  A referenced blob that cannot be read is an
    explicit failure, never an empty text.
    """
    base = {"kind": kind, "thread_id": _thread_id(payload), "occurred_at": _utc(occurred_at)}
    if kind not in PROSE_KINDS:
        return PreparedSignal(status=PREP_UNSUPPORTED_KIND, reason=f"{kind} is not prose", **base)

    content = load_content(payload, blob_reader)
    if content.status in ("missing", "corrupt", "unreadable"):
        status = PREP_MISSING_CONTENT if content.status == "missing" else PREP_CONTENT_ERROR
        return PreparedSignal(status=status, content_sha256=content.sha256 or None,
                              reason=f"content blob {content.status}", **base)
    sha = content.sha256 or None

    if kind == TRANSCRIPT_KIND:
        inline = payload.get("transcript")
        text = (str(inline) if inline else content.text).strip()
    else:
        hdr = payload.get("headers") or {}
        subj = (payload.get("subject") or content.subject or hdr.get("subject")
                or subject or "")
        body = payload.get("body") or content.text or ""
        text = _compose_email(str(subj).strip(), str(body).strip())

    if not text.strip():
        return PreparedSignal(status=PREP_NO_TEXT, content_sha256=sha,
                              reason="signal has no text", **base)

    role = trusted_actor_role(kind, payload)
    authored = authored_text(kind, text)
    if role is None:
        return PreparedSignal(status=PREP_NO_DIRECTION, full_text=text,
                              authored_text=authored, content_sha256=sha,
                              reason="no trusted direction for actor", **base)
    return PreparedSignal(status=PREP_READY, full_text=text, authored_text=authored,
                          actor_role=role, content_sha256=sha, **base)


def load_prepared_signal(
    conn: sqlite3.Connection, source: str, signal_id: str,
) -> PreparedSignal:
    """Prepare a stored signal (the worker's entry point).

    Reads the signal row and its blobs from this source only (no cross-source
    reads), so business isolation follows the store's.  *conn* is a store
    connection (``olo.store.connect``: ``sqlite3.Row`` rows).
    """
    from olo.store import get_blob_raw

    row = conn.execute(
        "SELECT kind, occurred_at, subject, payload FROM signals"
        " WHERE source=? AND external_id=?",
        (source, signal_id),
    ).fetchone()
    if row is None:
        return PreparedSignal(kind="", status=PREP_MISSING_CONTENT,
                              reason=f"signal {signal_id!r} not found for {source!r}")
    kind, occurred_at, subject, payload_raw = row[0], row[1], row[2], row[3]
    try:
        payload = json.loads(payload_raw) if isinstance(payload_raw, str) else dict(payload_raw)
    except (TypeError, ValueError):
        return PreparedSignal(kind=kind, status=PREP_CONTENT_ERROR, reason="payload unreadable")
    return prepare_signal(
        kind, payload, subject=subject, occurred_at=occurred_at,
        blob_reader=lambda sha: get_blob_raw(source, sha, conn),
    )
