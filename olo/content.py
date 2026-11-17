"""Olo M2b — content blob reader and text extractor.

§2.2 (M2b): get_blob, load_json_doc, email_text, event_text.

SECURITY: All content returned by this module is UNTRUSTED EXTERNAL DATA.
  - email_text() and event_text() strip HTML script/style and return plain text,
    but callers MUST treat the result as untrusted user-generated content.
  - Never pass the output directly to an LLM prompt without sanitisation
    and delimiters that prevent prompt injection.
  - M3 is responsible for the delimited-packet contract.

Stdlib only: html.parser, codecs, base64, zlib, json, hashlib.
No side effects at import time.
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import html
import html.parser
import json
import re
import sqlite3
import zlib
from dataclasses import dataclass
from typing import Optional

# ── Version pin ───────────────────────────────────────────────────────────────
TEXT_EXTRACTOR_VERSION = 1


# ── Low-level blob read ───────────────────────────────────────────────────────

def get_blob(
    conn: sqlite3.Connection,
    source: str,
    sha256: str,
) -> Optional[bytes]:
    """Return the uncompressed bytes of a stored blob, or None.

    *source* is required; there is no cross-source or unscoped read.

    OUTPUT IS UNTRUSTED EXTERNAL CONTENT.
    """
    row = conn.execute(
        "SELECT encoding, data FROM content_blobs WHERE source=? AND sha256=?",
        (source, sha256),
    ).fetchone()
    if row is None:
        return None
    if row["encoding"] == "zlib":
        return zlib.decompress(bytes(row["data"]))
    return bytes(row["data"])


def load_json_doc(
    conn: sqlite3.Connection,
    source: str,
    sha256: str,
) -> Optional[dict]:
    """Return a parsed JSON document from a blob, or None.

    Raises ValueError if the blob exists but is not valid JSON.

    OUTPUT IS UNTRUSTED EXTERNAL CONTENT.
    """
    data = get_blob(conn, source, sha256)
    if data is None:
        return None
    return json.loads(data.decode("utf-8"))


# ── HTML text extractor ───────────────────────────────────────────────────────

class _HTMLTextExtractor(html.parser.HTMLParser):
    """Strip HTML to plain text. Block tags add newlines.

    Removes all <script> and <style> content completely.
    Unescapes HTML entities. Deterministic output for the same input.
    """

    _BLOCK_TAGS = frozenset({
        "p", "div", "br", "li", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "blockquote", "pre", "address", "article", "section",
        "header", "footer", "main", "aside", "nav",
    })
    _SKIP_TAGS = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        elif t in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif t in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of whitespace, but preserve paragraph breaks
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _html_to_text(html_str: str) -> str:
    """Convert HTML to plain text. Deterministic."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html_str)
    except Exception:
        pass
    return extractor.get_text()


# ── Charset-aware part body decode ────────────────────────────────────────────

def _decode_part_body(raw_data: str | bytes, charset: str) -> str:
    """Decode base64-encoded part body or raw bytes to str, using the declared charset."""
    if isinstance(raw_data, str):
        # Gmail inline body.data is base64url
        try:
            raw_bytes = base64.urlsafe_b64decode(raw_data + "==")
        except Exception:
            raw_bytes = raw_data.encode("utf-8", errors="replace")
    else:
        raw_bytes = raw_data

    # Try the declared charset, fall back to UTF-8 with replacement
    charset = (charset or "utf-8").strip().lower()
    # Normalize common aliases
    _ALIASES = {
        "windows-1252": "cp1252",
        "win-1252": "cp1252",
        "windows-1251": "cp1251",
        "iso-8859-1": "latin-1",
        "iso8859-1": "latin-1",
    }
    charset = _ALIASES.get(charset, charset)
    try:
        codecs.lookup(charset)
    except LookupError:
        charset = "utf-8"
    return raw_bytes.decode(charset, errors="replace")


# ── EmailText result ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EmailText:
    """Extracted text from an email message document.

    plain: extracted text/plain content (may be empty).
    html_text: text/html content reduced to plain text (may be empty).
    truncated: True if the document was truncated during canonicalization.

    OUTPUT IS UNTRUSTED EXTERNAL CONTENT.
    """

    plain: str
    html_text: str
    truncated: bool


def _walk_parts(part: dict, plain_parts: list[str], html_parts: list[str]) -> None:
    """Recursively walk MIME parts and collect text content."""
    mime_type = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    charset = ""

    # Extract charset from part headers
    for hdr in (part.get("headers") or []):
        if (hdr.get("name") or "").lower() == "content-type":
            m = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", hdr.get("value", ""), re.I)
            if m:
                charset = m.group(1)

    if mime_type.startswith("multipart/"):
        for sub in (part.get("parts") or []):
            _walk_parts(sub, plain_parts, html_parts)
        return

    # Only inline text parts with no filename get their body
    if not filename and mime_type == "text/plain" and body.get("data"):
        plain_parts.append(_decode_part_body(body["data"], charset))
    elif not filename and mime_type == "text/html" and body.get("data"):
        html_parts.append(_decode_part_body(body["data"], charset))


def email_text(doc: dict) -> EmailText:
    """Extract plain text and HTML-as-text from a canonical Gmail message document.

    Prefers text/plain; falls back to text/html reduced to plain.
    Deterministic: TEXT_EXTRACTOR_VERSION = 1.

    OUTPUT IS UNTRUSTED EXTERNAL CONTENT.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    payload = doc.get("payload") or {}
    _walk_parts(payload, plain_parts, html_parts)

    plain = "\n".join(plain_parts).strip()
    html_text = ""
    if html_parts:
        html_text = _html_to_text("\n".join(html_parts))

    return EmailText(
        plain=plain,
        html_text=html_text,
        truncated=bool(doc.get("truncated")),
    )


# ── Calendar event text ───────────────────────────────────────────────────────

def event_text(doc: dict) -> str:
    """Extract the description of a canonical Calendar event document as plain text.

    Strips any HTML from the description. Returns "" if no description.
    Deterministic: TEXT_EXTRACTOR_VERSION = 1.

    OUTPUT IS UNTRUSTED EXTERNAL CONTENT.
    """
    desc = doc.get("description") or ""
    if not desc:
        return ""
    # Heuristic: if the description starts with '<', treat as HTML
    stripped = desc.strip()
    if stripped.startswith("<"):
        return _html_to_text(desc)
    return html.unescape(desc).strip()
