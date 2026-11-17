"""Olo M2b — Calendar full-content canonicalisation and calendar.event.content signals.

§6.3 (M2b extension, Calendar): canonical event document, calendar.event.content signal.
Extends §6 (Calendar sync) and §2.1–2.2 (ContentBlob) from B1.

Parallel to gmail_full.py (§2.3, Gmail).  The canonical event document stores the
full Google Calendar event resource including description, conferenceData, and
attachments metadata — fields excluded from the metadata-only
calendar.event.changed payload by §6.3 (M2 privacy invariant preserved:
description never appears in signals.payload, only in content_blobs).

Nothing here touches Olo's OAuth or any write path.
No side effects at import time.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, UTC
from typing import Optional

from olo.signals import (
    ContentBlob,
    NormalizedSignal,
    MAX_BLOB_BYTES,
    canonical_json,
)

# ── Constants ─────────────────────────────────────────────────────────────────
GCAL_EVENT_MEDIA_TYPE = "application/vnd.olo.gcal.event+json;v=1"
_MAX_EVENT_DOC_BYTES = MAX_BLOB_BYTES   # 16 MiB cap (events are tiny in practice)
_SIGNAL_SUBJECT_MAX = 256
_MAX_SUMMARY = 1024

# Fields removed from canonical doc (mutable or non-deterministic top-level)
# historyId is Gmail-specific; etag is a cache validator not part of the event.
# htmlLink is a derived UI URL excluded per §6.3.
_STRIP_KEYS = frozenset({"etag", "htmlLink"})


# ── Canonical event document ──────────────────────────────────────────────────

def canonical_event_doc(raw_event: Mapping) -> dict:
    """Build the canonical event document from a Calendar API event resource.

    §2.3 (Calendar): removes etag and htmlLink (non-deterministic or excluded);
    truncates if > MAX_BLOB_BYTES.  All other fields — including description,
    conferenceData, attachments — are preserved.

    Returns the dict (not yet serialised/compressed — caller handles that via
    make_event_blob).
    """
    doc: dict = {}
    for k, v in raw_event.items():
        if k not in _STRIP_KEYS:
            doc[k] = v

    # Truncate if necessary (extremely unlikely for calendar events)
    raw = canonical_json(doc).encode()
    if len(raw) > _MAX_EVENT_DOC_BYTES:
        # Last resort: drop description only
        doc.pop("description", None)
        doc["truncated"] = True
        raw = canonical_json(doc).encode()
        if len(raw) > _MAX_EVENT_DOC_BYTES:
            # Still too large: drop everything except structural fields
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


# ── calendar.event.content signal ────────────────────────────────────────────

def build_event_content_signal(
    source: str,
    raw_event: Mapping,
    calendar_id: str,
) -> NormalizedSignal:
    """Build a calendar.event.content signal for *raw_event*.

    Returns a NormalizedSignal with one ContentBlob (the canonical event doc).
    The blob sha256 appears in payload["content_refs"].

    Does NOT emit calendar.event.changed / calendar.event.cancelled — those
    remain in gcal.py's _event_to_signal().  This signal carries the full
    document, including description.

    The description field IS present in the blob but NOT in the metadata
    payload (§6.3: description excluded from calendar.event.changed payload).
    Both invariants are preserved here:
      - Full document → stored in content_blob (description preserved)
      - Metadata payload → no description field (privacy invariant from M2)
    """
    event_id = raw_event.get("id", "")
    status = raw_event.get("status", "confirmed")
    updated_raw = raw_event.get("updated", "")

    # Include calendar_id in the doc (not part of the Google API response body,
    # but essential context for a stored canonical document)
    doc = canonical_event_doc(raw_event)
    doc["calendar_id"] = calendar_id   # inject for retrieval context

    blob = make_event_blob(doc)

    # occurred_at from updated timestamp
    try:
        s = updated_raw.replace("Z", "+00:00")
        occurred_at = datetime.fromisoformat(s)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        occurred_at = occurred_at.astimezone(UTC)
    except Exception:
        occurred_at = datetime.now(UTC)

    # Subject = event summary (truncated)
    summary_raw = raw_event.get("summary")
    subject: Optional[str] = None
    if summary_raw:
        collapsed = re.sub(r"\s+", " ", summary_raw).strip()
        subject = collapsed[:_SIGNAL_SUBJECT_MAX] if collapsed else None

    # External id: keyed on event_id + updated + calendar_id hash so it is
    # stable across re-fetches of the same version.
    cal_hash = hashlib.sha256(calendar_id.encode()).hexdigest()[:8]
    ev_hash = hashlib.sha256(event_id.encode()).hexdigest()[:32]
    external_id = f"ec:{ev_hash}_{cal_hash}"[:256]

    payload: dict = {
        "event_id": event_id,
        "calendar_id": calendar_id,
        "status": status,
        "updated": updated_raw,
        # description deliberately NOT included here (§6.3 privacy invariant)
        "content_refs": [
            {
                "sha256":     blob.sha256,
                "media_type": blob.media_type,
                "size":       len(blob.data),
                "role":       "event_doc",
            }
        ],
    }

    return NormalizedSignal(
        source=source,
        external_id=external_id,
        kind="calendar.event.content",
        occurred_at=occurred_at,
        subject=subject,
        payload=payload,
        blobs=(blob,),
    )
