"""Olo M2b — Calendar full-content canonicalisation and event document blobs.

§6.3 (M2b extension, Calendar): canonical event document, content_refs on
calendar.event.changed and calendar.event.cancelled signals.

Design: the full event document blob (including description, conferenceData,
attachments, and all fields excluded from the metadata payload by §6.3) is
attached directly to the calendar.event.changed / calendar.event.cancelled
signal as:
  - signal.blobs = (ContentBlob(...),)
  - signal.payload["content_refs"] = [{sha256, media_type, size, role}]

This is the §2.2 contract for content_refs: the blob is attached to the
signal that caused it, not emitted as a separate signal.  The design differs
from email.content (which is a separate signal) because calendar events
already carry all structural metadata in calendar.event.changed; the blob
adds only the excluded descriptive fields.

Nothing here touches Olo's OAuth or any write path.
No side effects at import time.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Optional

from olo.signals import (
    ContentBlob,
    canonical_json,
    MAX_BLOB_BYTES,
)

# ── Constants ─────────────────────────────────────────────────────────────────
GCAL_EVENT_MEDIA_TYPE = "application/vnd.olo.gcal.event+json;v=1"
_MAX_EVENT_DOC_BYTES  = MAX_BLOB_BYTES   # 16 MiB cap (events are tiny in practice)


# ── Canonical event document ──────────────────────────────────────────────────

# Top-level fields excluded from the canonical doc because they are
# non-deterministic (etag changes on every fetch) or derived UI links.
_STRIP_KEYS = frozenset({"etag", "htmlLink"})


def canonical_event_doc(raw_event: Mapping, calendar_id: str) -> dict:
    """Build the canonical event document from a Calendar API event resource.

    §6.3 (M2b): removes etag and htmlLink; injects calendar_id as context.
    All other fields — including description, conferenceData, attachments —
    are preserved.

    Returns the dict (not yet serialised — caller calls make_event_blob).
    """
    doc: dict = {}
    for k, v in raw_event.items():
        if k not in _STRIP_KEYS:
            doc[k] = v

    # Inject calendar_id — not part of the Google API event resource body
    # but essential for retrieval context (which calendar does this event belong to).
    doc["calendar_id"] = calendar_id

    # Truncate if necessary (extremely unlikely for calendar events)
    raw = canonical_json(doc).encode()
    if len(raw) > _MAX_EVENT_DOC_BYTES:
        doc.pop("description", None)
        doc["truncated"] = True
        raw = canonical_json(doc).encode()
        if len(raw) > _MAX_EVENT_DOC_BYTES:
            # Last resort: keep only structural fields
            for key in list(doc.keys()):
                if key not in {
                    "id", "status", "summary", "start", "end",
                    "created", "updated", "organizer", "calendar_id",
                }:
                    doc.pop(key, None)
            doc["truncated"] = True
    return doc


def make_event_blob(doc: dict) -> ContentBlob:
    """Serialise and wrap *doc* as a ContentBlob."""
    raw_bytes = canonical_json(doc).encode()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    return ContentBlob(sha256=sha, media_type=GCAL_EVENT_MEDIA_TYPE, data=raw_bytes)


def build_event_blob(raw_event: Mapping, calendar_id: str) -> ContentBlob:
    """Build the content blob for a Calendar event.

    Called by gcal.py's _event_to_signal to attach the full event document
    to calendar.event.changed / calendar.event.cancelled via content_refs.

    Returns a ContentBlob containing the canonical event document JSON.
    description IS in the blob; it must NOT be in the metadata payload.
    """
    doc = canonical_event_doc(raw_event, calendar_id)
    return make_event_blob(doc)


def event_content_ref(blob: ContentBlob) -> dict:
    """Build the content_refs entry for an event blob."""
    return {
        "sha256":     blob.sha256,
        "media_type": blob.media_type,
        "size":       len(blob.data),
        "role":       "event_doc",
    }
