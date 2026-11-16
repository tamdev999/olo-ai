"""Olo AI — NormalizedSignal dataclass and canonical serialisation helpers.

§5.1–5.2: immutable data contract, hash scope, canonical JSON.
No side effects at import time.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from typing import Any, Union

# ── Type alias ───────────────────────────────────────────────────────────────
# JSON-serialisable scalar and recursive container.
JSONValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JSONValue"]
    | dict[str, "JSONValue"]
)

# ── Validation patterns ───────────────────────────────────────────────────────
_SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_KIND_RE = re.compile(r"^[a-z0-9_.]{1,64}$")
_MAX_EXTERNAL_ID = 256
_MAX_SUBJECT = 256
_MAX_PAYLOAD_BYTES = 65_536  # 64 KiB canonical JSON


# ── Helpers ───────────────────────────────────────────────────────────────────

def canonical_json(obj: Any) -> str:
    """Return deterministic JSON: sorted keys, no extra whitespace, no NaN/Inf.

    Stable across Python versions; reused by approval hashing in future milestones.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def format_utc(dt: datetime) -> str:
    """Format *dt* as ``YYYY-MM-DDTHH:MM:SS.ffffffZ`` (always 6 fractional digits, always Z).

    Raises ValueError for naive datetimes.
    """
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"datetime must be timezone-aware, got: {dt!r}")
    utc = dt.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond:06d}Z"


def _check_no_nan(obj: Any, path: str = "") -> None:
    """Walk a JSON-able structure and raise ValueError for NaN/Infinity."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):  # NaN test: x != x
            raise ValueError(f"payload contains NaN or Infinity at {path!r}")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ValueError(f"payload key at {path!r} is not a string: {k!r}")
            _check_no_nan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_no_nan(v, f"{path}[{i}]")


# ── NormalizedSignal ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NormalizedSignal:
    """Immutable, validated business signal.

    All fields validated in ``__post_init__``; raises ``ValueError`` with a
    precise message on any constraint violation.
    """

    source: str
    external_id: str
    kind: str
    occurred_at: datetime
    subject: str | None
    payload: Mapping[str, JSONValue]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not _SOURCE_RE.match(self.source):
            raise ValueError(
                f"source must match ^[a-z0-9_]{{1,32}}$, got: {self.source!r}"
            )
        if not self.external_id or len(self.external_id) > _MAX_EXTERNAL_ID:
            raise ValueError(
                f"external_id must be 1–{_MAX_EXTERNAL_ID} chars, got length "
                f"{len(self.external_id)}"
            )
        if not _KIND_RE.match(self.kind):
            raise ValueError(
                f"kind must match ^[a-z0-9_.]{{1,64}}$, got: {self.kind!r}"
            )
        if self.occurred_at.tzinfo is None or \
                self.occurred_at.tzinfo.utcoffset(self.occurred_at) is None:
            raise ValueError(
                f"occurred_at must be timezone-aware, got: {self.occurred_at!r}"
            )
        if self.subject is not None and len(self.subject) > _MAX_SUBJECT:
            raise ValueError(
                f"subject must be ≤{_MAX_SUBJECT} chars, got length {len(self.subject)}"
            )
        # Validate payload: no NaN/Infinity, no non-string keys, ≤ 64 KiB
        _check_no_nan(dict(self.payload))
        raw = canonical_json(dict(self.payload))
        if len(raw.encode()) > _MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload canonical JSON exceeds {_MAX_PAYLOAD_BYTES} bytes "
                f"(got {len(raw.encode())})"
            )

    def content_sha256(self) -> str:
        """SHA-256 hex of the UTF-8 canonical JSON of exactly the specified fields.

        §5.2 scope: schema_version, source, external_id, kind, occurred_at,
        subject, payload.  Excludes: id, ingested_at, run_id, and all runner
        metadata.
        """
        blob = canonical_json({
            "schema_version": self.schema_version,
            "source": self.source,
            "external_id": self.external_id,
            "kind": self.kind,
            "occurred_at": format_utc(self.occurred_at),
            "subject": self.subject,
            "payload": dict(self.payload),
        })
        return hashlib.sha256(blob.encode()).hexdigest()
