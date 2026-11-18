"""Olo M2b — Gmail full-content canonicalization and email.content signals.

§2.3 (M2b): canonical message document, email.content signal, batch byte budget,
backfill phases (bootstrap, resume, gap).

Nothing here touches Olo's OAuth or any write path.
No side effects at import time.
"""
from __future__ import annotations

import base64
import email.header as _email_header
import email.utils as _email_utils
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, UTC
from typing import Optional

from olo.adapters import SourceError
from olo.signals import (
    ContentBlob,
    NormalizedSignal,
    MAX_BLOB_BYTES,
    canonical_json,
    MAX_BATCH_BLOB_BYTES,
)

# ── Constants ─────────────────────────────────────────────────────────────────
GMAIL_MSG_MEDIA_TYPE = "application/vnd.olo.gmail.message+json;v=1"
_MAX_MSG_DOC_BYTES = MAX_BLOB_BYTES           # 16 MiB per message doc
_BATCH_BLOB_BUDGET_BYTES = 48 * 1024 * 1024   # 48 MiB soft stop per fetch
_MAX_REFS = 50     # In-Reply-To / References thread headers
_MAX_ADDRS = 100   # per address list in payload
_MAX_ATTS = 100    # attachments in payload

# Fields removed from canonical doc (mutable or derived)
_STRIP_KEYS = frozenset({"labelIds", "historyId", "snippet"})

_ZLIB_SAVE_THRESHOLD = 0.10  # compress if saves ≥ 10 %


# ── Canonical message document ────────────────────────────────────────────────

def _strip_body_data(part: dict) -> dict:
    """Return a copy of *part* with inline body.data stripped unless text/no-filename.

    Per §2.3: inline body.data is kept only for text/* parts with no filename.
    For all other parts:
      - body.data is removed
      - body.data_sha256 is added if inline data existed
      - body.attachmentId and body.size are kept
    """
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = dict(part.get("body") or {})
    keep_inline = mime.startswith("text/") and not filename

    if not keep_inline and "data" in body:
        # Store sha256 of original inline data as a reference
        raw = body.pop("data")
        if raw:
            try:
                raw_bytes = base64.urlsafe_b64decode(raw + "==")
                body["data_sha256"] = hashlib.sha256(raw_bytes).hexdigest()
            except Exception:
                pass

    result = dict(part)
    result["body"] = body
    # Recurse into sub-parts
    if "parts" in result:
        result["parts"] = [_strip_body_data(p) for p in (result["parts"] or [])]
    return result


def _doc_size(doc: dict) -> int:
    return len(canonical_json(doc).encode())


def _truncate_doc(doc: dict) -> dict:
    """Truncate over-large doc to fit in _MAX_MSG_DOC_BYTES.

    Strategy (§2.3):
    1. Drop text/html inline data, mark parts as omitted="size".
    2. Then drop text/plain inline data in reverse part order.
    3. Set truncated=True.
    """
    if _doc_size(doc) <= _MAX_MSG_DOC_BYTES:
        return doc

    doc = dict(doc)
    # Pass 1: drop HTML inline data
    doc["payload"] = _drop_inline(doc.get("payload") or {}, "text/html")
    if _doc_size(doc) <= _MAX_MSG_DOC_BYTES:
        doc["truncated"] = True
        return doc

    # Pass 2: drop plain text inline data in reverse part order
    doc["payload"] = _drop_inline(doc.get("payload") or {}, "text/plain", reverse=True)
    doc["truncated"] = True
    return doc


def _drop_inline(part: dict, target_mime: str, reverse: bool = False) -> dict:
    """Drop inline body.data from parts matching *target_mime*."""
    part = dict(part)
    if (part.get("mimeType") or "").lower() == target_mime:
        body = dict(part.get("body") or {})
        if "data" in body:
            body.pop("data")
            body["omitted"] = "size"
            part["body"] = body
        return part
    if "parts" in part:
        subparts = [dict(p) for p in (part["parts"] or [])]
        if reverse:
            subparts = list(reversed(subparts))
        processed = [_drop_inline(p, target_mime, reverse) for p in subparts]
        if reverse:
            processed = list(reversed(processed))
        part["parts"] = processed
    return part


def canonical_message_doc(raw_msg: Mapping) -> dict:
    """Build the canonical message document from a format=full Gmail API response.

    §2.3: removes labelIds/historyId/snippet; strips non-text inline data;
    truncates if > 16 MiB; serialises with canonical_json.

    Returns the dict (not yet serialised/compressed — caller handles that).
    """
    # Start with a shallow copy, removing top-level mutable/derived fields
    doc: dict = {}
    for k, v in raw_msg.items():
        if k not in _STRIP_KEYS:
            doc[k] = v

    # Process payload tree: strip inline body.data for non-text parts
    if "payload" in doc:
        doc["payload"] = _strip_body_data(doc["payload"])

    # Truncate if necessary
    doc = _truncate_doc(doc)
    return doc


def make_message_blob(doc: dict) -> ContentBlob:
    """Serialise, optionally compress, and wrap *doc* as a ContentBlob."""
    import zlib
    raw_bytes = canonical_json(doc).encode()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    return ContentBlob(sha256=sha, media_type=GMAIL_MSG_MEDIA_TYPE, data=raw_bytes)


# ── Header extraction helpers ──────────────────────────────────────────────────

def _header_val(headers: list[dict], name: str) -> str:
    name_lc = name.lower()
    for h in headers:
        if (h.get("name") or "").lower() == name_lc:
            return h.get("value") or ""
    return ""


def _decode_hdr(raw: str) -> str:
    if not raw:
        return ""
    try:
        parts = _email_header.decode_header(raw)
        return str(_email_header.make_header(parts))
    except Exception:
        return raw


def _parse_addrs(raw: str, cap: int = _MAX_ADDRS) -> list[dict]:
    if not raw:
        return []
    pairs = _email_utils.getaddresses([raw[:998]])
    result = []
    for name, addr in pairs[:cap]:
        result.append({"name": name.strip(), "email": addr.strip().lower()})
    return result


def _split_refs(raw: str, cap: int = _MAX_REFS) -> list[str]:
    """Split References / In-Reply-To header into individual message IDs."""
    ids = re.findall(r"<[^>]+>", raw)
    return ids[:cap]


# ── email.content signal ──────────────────────────────────────────────────────

def build_email_content_signal(
    source: str,
    raw_msg: Mapping,
    record_id: str,
) -> NormalizedSignal:
    """Build an email.content signal for *raw_msg* (format=full).

    Returns a NormalizedSignal with one ContentBlob (the canonical doc).
    The blob sha256 appears in payload["content_refs"].

    Does NOT emit email.received / email.sent — those remain in _record_to_signals.
    """
    from datetime import UTC

    msg_id = raw_msg.get("id", "")
    thread_id = raw_msg.get("threadId", "")
    label_ids: list[str] = raw_msg.get("labelIds") or []
    direction = "sent" if "SENT" in label_ids else "received"

    try:
        ts_ms = int(raw_msg.get("internalDate", "0"))
        occurred_at = datetime.fromtimestamp(ts_ms / 1000.0, tz=UTC)
    except (ValueError, TypeError):
        occurred_at = datetime.now(UTC)

    headers = (raw_msg.get("payload") or {}).get("headers") or []

    # Subject for signal.subject
    raw_subject = _header_val(headers, "Subject")
    subject_decoded = _decode_hdr(raw_subject)
    subject_collapsed = re.sub(r"\s+", " ", subject_decoded).strip()
    subject: Optional[str] = subject_collapsed[:256] if subject_collapsed else None

    # Threading headers
    rfc822_msg_id = _header_val(headers, "Message-ID")
    in_reply_to   = _header_val(headers, "In-Reply-To")
    references_raw = _header_val(headers, "References")
    references = _split_refs(references_raw)

    # Addresses
    from_addrs = _parse_addrs(_header_val(headers, "From"))
    to_addrs   = _parse_addrs(_header_val(headers, "To"))
    cc_addrs   = _parse_addrs(_header_val(headers, "Cc"))
    bcc_raw    = _header_val(headers, "Bcc") if direction == "sent" else ""
    bcc_addrs  = _parse_addrs(bcc_raw) if bcc_raw else []

    # Attachments
    attachments = _extract_attachment_refs(msg_id, (raw_msg.get("payload") or {}))

    # Build canonical doc and blob
    doc = canonical_message_doc(raw_msg)
    blob = make_message_blob(doc)

    payload: dict = {
        "message_id":   msg_id,
        "thread_id":    thread_id,
        "direction":    direction,
        "rfc822_message_id": rfc822_msg_id,
        "in_reply_to":  in_reply_to,
        "references":   references,
        "from":         from_addrs,
        "to":           to_addrs,
        "cc":           cc_addrs,
    }
    if bcc_addrs:
        payload["bcc"] = bcc_addrs
    payload["attachments"] = attachments[:_MAX_ATTS]
    payload["truncated"]   = bool(doc.get("truncated"))
    payload["content_refs"] = [
        {
            "sha256":     blob.sha256,
            "media_type": blob.media_type,
            "size":       len(blob.data),
            "role":       "message_doc",
        }
    ]

    return NormalizedSignal(
        source=source,
        external_id=f"c:{msg_id}",
        kind="email.content",
        occurred_at=occurred_at,
        subject=subject,
        payload=payload,
        blobs=(blob,),
    )


def _extract_attachment_refs(msg_id: str, part: dict, refs: list | None = None) -> list[dict]:
    """Recursively collect attachment references from a MIME part tree."""
    if refs is None:
        refs = []
    mime = (part.get("mimeType") or "").lower()
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    part_id = part.get("partId", "")

    # Attachment: has a filename and is not a container type
    if filename and not mime.startswith("multipart/"):
        ref: dict = {
            "part_id":  part_id,
            "filename": filename[:255],
            "mime_type": mime,
            "size":      body.get("size", 0),
        }
        refs.append(ref)

    for subpart in (part.get("parts") or []):
        _extract_attachment_refs(msg_id, subpart, refs)
    return refs


# ── Backfill logic (§2.3) ─────────────────────────────────────────────────────

def _backfill_mode_from_audit(source: str, conn) -> str:
    """Determine backfill mode: 'resume', 'gap', or 'bootstrap'.

    - 'resume': a valid numeric checkpoint exists.
    - 'gap': no checkpoint but signals or a checkpoint.reset audit entry exist.
    - 'bootstrap': fresh account, no prior history.
    """
    from olo.store import get_checkpoint, count_signals
    cursor = get_checkpoint(source, conn)
    if cursor is not None and cursor.lstrip("-").isdigit():
        return "resume"
    # Check for signals or a checkpoint.reset audit
    n_sigs = count_signals(source, conn)
    if n_sigs > 0:
        return "gap"
    reset_row = conn.execute(
        "SELECT id FROM audit_events WHERE actor LIKE ? AND action=? LIMIT 1",
        (f"%{source}%", "checkpoint.reset"),
    ).fetchone()
    if reset_row:
        return "gap"
    return "bootstrap"


def _get_expired_cursor_from_audit(source: str, conn) -> Optional[str]:
    """Read the old cursor from the last checkpoint.reset audit event."""
    row = conn.execute(
        "SELECT detail FROM audit_events WHERE action='checkpoint.reset' "
        "AND detail LIKE ? ORDER BY at DESC LIMIT 1",
        (f'%"{source}"%',),
    ).fetchone()
    if not row:
        return None
    try:
        d = json.loads(row["detail"])
        return d.get("old_cursor_hash")  # may be a hash, not the raw cursor
    except Exception:
        return None


def run_backfill_batch(
    source: str,
    client,
    conn,
    run_id: str,
    *,
    after_epoch: int,
    page_token: Optional[str],
    resume_history_id: str,
    mode: str,
    limit: int,
) -> tuple[list[NormalizedSignal], Optional[str], bool]:
    """Fetch one page of backfill messages and return (signals, next_page_token, done).

    §2.3 rules:
    - Only emits email.content (not email.received/sent — avoids conflicts).
    - Skips 404 messages (deleted since listing).
    - done=True when no next page token.
    """
    from olo.store import _audit

    params: list[tuple[str, str]] = [
        ("includeSpamTrash", "false"),
        ("maxResults", str(min(limit, 500))),
    ]
    # Gmail's after: filter uses unix epoch in seconds; after:0 returns nothing.
    # Only add the filter when we have a meaningful positive epoch value.
    if after_epoch and after_epoch > 0:
        params.insert(0, ("q", f"after:{after_epoch}"))
    if page_token:
        params.append(("pageToken", page_token))

    resp = client.list_messages(params)
    messages = resp.get("messages") or []
    next_pt = resp.get("nextPageToken")

    signals: list[NormalizedSignal] = []
    batch_blob_bytes = 0
    # P1-F: on a budget-triggered soft-stop, we return the CURRENT page_token
    # so the caller re-enters the same messages.list page on the next batch.
    # To avoid replaying already-processed messages, we store a skip_count in
    # the page_token wrapper when soft-stopping mid-page. The caller passes
    # skip_count back via page_token; we decode it here.
    # Format: when page_token is "skip:N:REAL_TOKEN" we skip the first N stubs.
    skip_count = 0
    real_page_token = page_token
    if page_token and page_token.startswith("skip:"):
        parts = page_token.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            skip_count = int(parts[1])
            real_page_token = parts[2] if parts[2] else None

    # Re-fetch with the real (un-prefixed) page_token
    if real_page_token != page_token:
        params_real: list[tuple[str, str]] = [
            ("includeSpamTrash", "false"),
            ("maxResults", str(min(limit, 500))),
        ]
        if after_epoch and after_epoch > 0:
            params_real.insert(0, ("q", f"after:{after_epoch}"))
        if real_page_token:
            params_real.append(("pageToken", real_page_token))
        resp = client.list_messages(params_real)
        messages = resp.get("messages") or []
        next_pt = resp.get("nextPageToken")

    processed_count = 0
    for msg_stub in messages:
        msg_id = msg_stub.get("id", "")
        if not msg_id:
            continue
        # P1-F: skip already-committed stubs from a previous soft-stop
        if processed_count < skip_count:
            processed_count += 1
            continue
        processed_count += 1

        # P0-C: Only 404 (get_message_full returns None) is skippable.
        raw_msg = client.get_message_full(msg_id)
        if not raw_msg:
            continue  # 404 — deleted since listing; skip gracefully

        sig = build_email_content_signal(source, raw_msg, run_id)

        blob_bytes = sum(len(b.data) for b in (sig.blobs or ()))
        if batch_blob_bytes + blob_bytes > _BATCH_BLOB_BUDGET_BYTES and signals:
            # P1-F: Soft stop — return what we have. Encode the number of stubs
            # already processed on this page so the next call can skip them.
            # Format: "skip:{committed_count}:{real_page_token_or_empty}"
            encoded_pt = f"skip:{len(signals)+skip_count}:{real_page_token or ''}"
            return signals, encoded_pt, False

        signals.append(sig)
        batch_blob_bytes += blob_bytes

        if len(signals) >= limit:
            break

    done = next_pt is None
    return signals, next_pt, done
