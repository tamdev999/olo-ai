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
                 the start of quoted history (attribution lines, forward
                 markers, Outlook header blocks; never a lone "From:" line);
                 only it may support an authoritative statement.  The
                 commitment extractor and the Hindsight projection both use
                 ``PreparedSignal.authored_text`` (``authored_sha256``).
MIME           — the readable body of a Gmail document: text/plain from
                 multipart/alternative (HTML otherwise), each part decoded with
                 its declared charset; attachments and attached messages are
                 not body text; HTML loses head/style/script/comments and its
                 quote containers become quoted lines.

No side effects at import time; no network; blob reads are injected.
"""
from __future__ import annotations

import base64
import codecs
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


def _part_header(part: dict, name: str) -> str:
    for h in part.get("headers") or []:
        if isinstance(h, dict) and (h.get("name") or "").lower() == name:
            return h.get("value") or ""
    return ""


_CHARSET_RE = re.compile(r"""charset\s*=\s*["']?([A-Za-z0-9_.:+-]+)""", re.IGNORECASE)


def _part_charset(part: dict) -> str:
    m = _CHARSET_RE.search(_part_header(part, "content-type"))
    if not m:
        return "utf-8"
    try:
        return codecs.lookup(m.group(1)).name
    except LookupError:
        return "utf-8"


def _part_text(part: dict) -> str:
    """A text part's body decoded with its declared charset.

    The Gmail API has already undone the transfer encoding (base64,
    quoted-printable): ``body.data`` is the part's raw bytes, base64url.
    """
    data = (part.get("body") or {}).get("data") or ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode(_part_charset(part), errors="replace")


def _is_attachment(part: dict) -> bool:
    return bool(part.get("filename")) or _part_header(
        part, "content-disposition").lower().startswith("attachment")


def _mime_body(part: dict) -> str:
    """The message text a person reads, from a MIME part tree.

    * multipart/alternative: the text/plain alternative, else the HTML one;
    * other multipart types: their readable children in order;
    * attachments (a filename or ``Content-Disposition: attachment``) and
      attached messages (``message/rfc822``) are not the message text.
    """
    mime = (part.get("mimeType") or "").lower()
    if _is_attachment(part) or mime == "message/rfc822":
        return ""
    if mime.startswith("multipart/"):
        children = [c for c in part.get("parts") or [] if isinstance(c, dict)]
        if mime == "multipart/alternative":
            for wanted in ("text/plain", "text/html"):
                for c in children:
                    if (c.get("mimeType") or "").lower() == wanted and not _is_attachment(c):
                        text = _mime_body(c)
                        if text.strip():
                            return text
            for c in children:                       # nested multipart alternative
                text = _mime_body(c)
                if text.strip():
                    return text
            return ""
        return "\n".join(t for t in (_mime_body(c) for c in children) if t.strip())
    if not (part.get("body") or {}).get("data"):
        return ""
    if mime == "text/plain":
        return _part_text(part)
    if mime == "text/html":
        return _html_to_text(_part_text(part))
    return ""


_DROP_HTML_RE = re.compile(
    r"<!--.*?-->|<(head|style|script|title)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
#: Where mail clients start the quoted original in HTML: Gmail, Outlook,
#: Apple Mail / Thunderbird citations.
_HTML_QUOTE_START_RE = re.compile(
    r"<div\b[^>]*\bclass=[\"'][^\"']*\bgmail_quote\b"
    r"|<div\b[^>]*\bid=[\"'](?:divRplyFwdMsg|appendonsend|mail-editor-reference-message-container)[\"']"
    r"|<div\b[^>]*\bclass=[\"'][^\"']*\bmoz-cite-prefix\b"
    r"|<blockquote\b[^>]*\btype=[\"']cite[\"']",
    re.IGNORECASE)
_BLOCKQUOTE_RE = re.compile(r"<blockquote\b[^>]*>(.*?)</blockquote>", re.IGNORECASE | re.DOTALL)
_BREAK_TAG_RE = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h[1-6]|hr)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(markup: str) -> str:
    """Plain text from HTML.

    Document head, styles, scripts and comments are dropped.  Quoted
    originals become ``>``-quoted lines: everything from a mail client's
    quote container (Gmail ``gmail_quote``, Outlook ``divRplyFwdMsg``,
    citation blocks) onward, and any ``<blockquote>``.
    """
    markup = _DROP_HTML_RE.sub("", markup)
    m = _HTML_QUOTE_START_RE.search(markup)
    authored, quoted = (markup[:m.start()], markup[m.start():]) if m else (markup, "")

    def quote(m: re.Match[str]) -> str:
        inner = _html_to_text(m.group(1))
        return "\n" + "\n".join("> " + line for line in inner.splitlines()) + "\n"

    def flat(fragment: str) -> str:
        text = _BLOCKQUOTE_RE.sub(quote, fragment)
        text = _BREAK_TAG_RE.sub("\n", text)
        text = html.unescape(_TAG_RE.sub("", text)).replace("\xa0", " ")
        lines = [line.rstrip() for line in text.splitlines()]
        return "\n".join(lines).strip()

    out = flat(authored)
    if quoted:
        q = flat(_BLOCKQUOTE_RE.sub(lambda mm: mm.group(1), quoted))
        out += "\n" + "\n".join("> " + line if line else ">" for line in q.splitlines())
    return re.sub(r"\n{3,}", "\n\n", out).strip()


_ICS_TYPES = ("text/calendar", "application/ics")
_ICS_FOLD_RE = re.compile(r"\r?\n[ \t]")
_ICS_UID_RE = re.compile(r"^UID(?:;[^:\r\n]*)?:(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def calendar_uids(doc: dict) -> tuple[str, ...]:
    """iCalendar UIDs in a Gmail message's calendar parts.

    A calendar invitation (or its update or cancellation) carries the event
    as a ``text/calendar`` part (or an ``.ics`` attachment whose bytes are
    inline); its ``UID`` is the event's iCalendar identity — Google
    Calendar's ``iCalUID``.  This is the authoritative link between an email
    and a calendar event.  Parts whose bytes were not fetched carry none.
    """
    found: set[str] = set()

    def walk(part: dict) -> None:
        mime = (part.get("mimeType") or "").lower()
        if mime.startswith("multipart/"):
            for child in part.get("parts") or []:
                if isinstance(child, dict):
                    walk(child)
            return
        is_ics = mime in _ICS_TYPES or (part.get("filename") or "").lower().endswith(".ics")
        if is_ics and (part.get("body") or {}).get("data"):
            text = _ICS_FOLD_RE.sub("", _part_text(part))
            found.update(m.group(1).strip() for m in _ICS_UID_RE.finditer(text))

    walk(doc.get("payload") or {})
    return tuple(sorted(u for u in found if u))


def _render_gmail_doc(doc: dict) -> tuple[str, str, bool]:
    """(subject, body, truncated) from a canonical Gmail message document."""
    payload = doc.get("payload") or {}
    subject = _part_header(payload, "subject").strip()
    body = _mime_body(payload).replace("\r\n", "\n").replace("\r", "\n").strip()
    return subject, body, bool(doc.get("truncated"))


# ── Authored text (quoted replies excluded) ───────────────────────────────────

#: A line that by itself starts a quoted or forwarded original.
_QUOTE_START_RE = re.compile(
    r"^\s*(?:"
    r"(?:On|Le|Am|El|Il|Op)\s.+\b(?:wrote|a écrit|schrieb|escribió|ha scritto|schreef)\s*:\s*$"
    r"|-{2,}\s*(?:Original|Forwarded)\s+Message\s*-{2,}"
    r"|-{2,}\s*Forwarded message\s*-{2,}"
    r"|Begin forwarded message:\s*$"
    r")",
    re.IGNORECASE,
)
#: Gmail wraps a long attribution line: "On Tue, Jun 2, 2026 at 9:00 AM Alice <a@x>"
#: on one line and "wrote:" on the next.
_ATTRIBUTION_START_RE = re.compile(r"^\s*(?:On|Le|Am|El|Il|Op)\s.+\d", re.IGNORECASE)
_WROTE_END_RE = re.compile(r"\b(?:wrote|a écrit|schrieb|escribió|ha scritto|schreef)\s*:\s*$",
                           re.IGNORECASE)
#: Outlook / RFC 822 style header block of a quoted original.
_HDR_FROM_RE = re.compile(r"^\s*\**\s*(?:From|Von|De|Van)\s*:\**\s+\S", re.IGNORECASE)
_HDR_DATE_RE = re.compile(r"^\s*\**\s*(?:Sent|Date|Gesendet|Envoyé|Enviado|Datum)\s*:", re.IGNORECASE)
_HDR_TO_RE = re.compile(r"^\s*\**\s*(?:To|Subject|Cc|An|Betreff|À|Objet|Para|Asunto)\s*:", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^\s*(?:_{5,}|-{5,})\s*$")
#: A From: line whose whole value is an address ("Name <a@b>", "a@b",
#: "[mailto:a@b]") is a quoted header even without the rest of the block.
_HDR_ADDRESS_RE = re.compile(
    r"^\s*\**\s*(?:From|Von|De|Van)\s*:\**\s+(?:[^<>@\n]{0,80}<[^<>@\s]+@[^<>\s]+>"
    r"|\S+@\S+|[^\n]{0,80}\[mailto:[^\]]+\])\s*$", re.IGNORECASE)


def _quote_start(lines: list[str], i: int) -> bool:
    """True when line *i* starts quoted history (never a lone ``From:``)."""
    line = lines[i]
    if _QUOTE_START_RE.match(line):
        return True
    if (_ATTRIBUTION_START_RE.match(line) and i + 1 < len(lines)
            and _WROTE_END_RE.search(lines[i + 1]) and not _WROTE_END_RE.search(line)):
        return True
    if _HDR_FROM_RE.match(line):
        if _HDR_ADDRESS_RE.match(line):
            return True                      # "From: Bob <bob@x.com>": a header, not prose
        window = lines[i + 1:i + 6]
        return any(_HDR_DATE_RE.match(w) for w in window) and any(_HDR_TO_RE.match(w) for w in window)
    if _SEPARATOR_RE.match(line):
        nxt = next((w for w in lines[i + 1:i + 3] if w.strip()), "")
        return bool(_HDR_FROM_RE.match(nxt)) and _quote_start(lines, lines.index(nxt, i + 1))
    return False


def authored_text(kind: str, full_text: str) -> str:
    """The part of the signal written by its author (quoted history removed).

    For email kinds: drops ``>``-quoted lines and everything from the start
    of a quoted or forwarded original: an attribution line ("On … wrote:",
    also when Gmail wraps it over two lines, and common translations), an
    "Original/Forwarded message" marker, a ``From:`` header whose value is an
    address, or an Outlook header block (``From:`` followed by ``Sent:``/
    ``Date:`` and ``To:``/``Subject:`` lines).  A line that merely begins
    with "From:" in prose ("From: Monday the warehouse is closed") is the
    author's text.  Other kinds are returned unchanged.
    """
    if kind not in EMAIL_KINDS:
        return full_text
    lines = full_text.splitlines()
    kept: list[str] = []
    for i, line in enumerate(lines):
        if _quote_start(lines, i):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).rstrip()


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
    #: iCalendar UIDs of calendar invitations the message carries (email).
    ical_uids: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.status == PREP_READY

    @property
    def quoted_omitted(self) -> bool:
        """True when quoted or forwarded history was removed from full_text."""
        return self.authored_text.strip() != self.full_text.strip()

    @property
    def authored_sha256(self) -> str:
        """Checksum of the authored text both extraction and projection use."""
        return hashlib.sha256(self.authored_text.encode("utf-8")).hexdigest()


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
    uids: tuple[str, ...] = ()
    if content.status == "ok" and content.media_type == GMAIL_MSG_MEDIA_TYPE:
        uids = calendar_uids(json.loads(content.raw.decode("utf-8")))
    if role is None:
        return PreparedSignal(status=PREP_NO_DIRECTION, full_text=text,
                              authored_text=authored, content_sha256=sha,
                              reason="no trusted direction for actor", ical_uids=uids, **base)
    return PreparedSignal(status=PREP_READY, full_text=text, authored_text=authored,
                          actor_role=role, content_sha256=sha, ical_uids=uids, **base)


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
