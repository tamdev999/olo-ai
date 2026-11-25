"""olo/extractor.py — Commitment-update extractor (S4).

Extracts structured commitment events (delivery orders, meeting slots) from
business signals using a budget-limited, cached LLM call.

Design pillars
--------------
Budget-limited  — a Budget object caps total calls and tokens; extraction
                  returns None when exhausted.
Cached          — an optional SQLite cache connection stores
                  prompt_hash → response so identical prompts are free.
Verifiable      — supporting_passage must appear verbatim in the signal text;
                  unverifiable extractions are rejected outright.
Reject-not-guess — confidence < 0.7 → None; ambiguous signals → None.
Actor from metadata — actor_role is derived from signal kind / payload metadata;
                  the model is never trusted for actor attribution.

Extraction is intentionally separate from ledger writes: callers receive an
ExtractionResult and pass it to apply_event themselves.  When actor_role is
"customer" and action is "cancel", apply_event will produce is_confirmed=0
(a proposal, not a confirmed cancellation — the S3 customer-cancel fix in
_write_version handles this automatically).

No side effects at import time.  No Hindsight dependency.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from olo.ledger import ACTIONS, SUPPORTED_EVENT_TYPES

__all__ = [
    "ExtractionResult",
    "Budget",
    "extract_commitment_update",
    "migrate_extraction_cache",
]

_CONFIDENCE_THRESHOLD: float = 0.7
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COST_PER_1M_TOKENS: float = 0.15   # gpt-4o-mini blended pricing (USD)
_MAX_SIGNAL_CHARS: int = 4_000       # prompt truncation guard


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExtractionResult:
    """Structured output of one successful extract_commitment_update call.

    Returned only when:
      - signal clearly describes a delivery or meeting commitment
      - confidence >= _CONFIDENCE_THRESHOLD (0.7)
      - supporting_passage is verified as a verbatim substring of signal text

    ``None`` is returned (not an ExtractionResult) for non-commitment signals,
    low-confidence extractions, unverifiable passages, or budget exhaustion.
    """
    event_type:           str            # "delivery" | "meeting"
    action:               str            # ledger action name
    actor_role:           str            # "business" | "customer" (from metadata)
    business_key:         Optional[str]  # order/booking ID, or None
    confirmed_date:       Optional[str]  # YYYY-MM-DD, or None
    proposed_date:        Optional[str]  # YYYY-MM-DD (request_change only)
    prior_confirmed_date: Optional[str]  # YYYY-MM-DD (date being superseded)
    thread_id:            Optional[str]  # from payload metadata
    supporting_passage:   str            # verbatim quote (verified)
    confidence:           float          # always >= _CONFIDENCE_THRESHOLD when set
    cache_hit:            bool = False   # True when served from cache


# ── Budget guard ─────────────────────────────────────────────────────────────

class Budget:
    """Mutable call and token budget for a session.

    Share one Budget instance across multiple extract_commitment_update calls
    to cap total spend.  Not thread-safe; use a per-request instance in
    concurrent settings.
    """

    def __init__(
        self,
        max_calls: int = 200,
        max_tokens_per_call: int = 3_000,
        max_total_tokens: int = 100_000,
    ) -> None:
        self.max_calls = max_calls
        self.max_tokens_per_call = max_tokens_per_call
        self.max_total_tokens = max_total_tokens
        # Mutable usage counters
        self.calls_used: int = 0
        self.tokens_used: int = 0
        self.cache_hits: int = 0
        self.cost_usd: float = 0.0

    def within_budget(self) -> bool:
        """True if another model call can be made without exceeding the budget."""
        return (
            self.calls_used < self.max_calls
            and self.tokens_used < self.max_total_tokens
        )

    def charge(self, tokens: int) -> None:
        """Record usage for one successful (non-cached) model call."""
        self.calls_used += 1
        self.tokens_used += tokens
        self.cost_usd += tokens * _COST_PER_1M_TOKENS / 1_000_000

    def record_cache_hit(self) -> None:
        """Record a cache hit; no token cost."""
        self.cache_hits += 1

    def report(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary of this session's usage."""
        return {
            "calls_used":  self.calls_used,
            "tokens_used": self.tokens_used,
            "cache_hits":  self.cache_hits,
            "cost_usd":    round(self.cost_usd, 6),
        }


# ── Cache schema ─────────────────────────────────────────────────────────────

_CACHE_DDL = """\
CREATE TABLE IF NOT EXISTS extraction_cache (
    prompt_hash  TEXT NOT NULL PRIMARY KEY,
    response     TEXT NOT NULL,
    tokens_used  INTEGER NOT NULL DEFAULT 0,
    cached_at    TEXT NOT NULL
);
"""


def migrate_extraction_cache(conn: sqlite3.Connection) -> None:
    """Create the extraction_cache table if it does not exist. Idempotent."""
    conn.executescript(_CACHE_DDL)
    conn.commit()


# ── Signal text extraction ────────────────────────────────────────────────────

def _signal_text(kind: str, payload: dict) -> str:
    """Extract the meaningful text from a signal payload."""
    if kind in ("email.sent", "email.received", "email.content"):
        subject = str(payload.get("subject", "")).strip()
        body    = str(payload.get("body",    "")).strip()
        parts: list[str] = []
        if subject:
            parts.append(f"Subject: {subject}")
        if body:
            parts.append(body)
        return "\n\n".join(parts)
    if kind == "call.transcript":
        return str(payload.get("transcript", "")).strip()
    # Best effort for other kinds
    return "\n".join(
        f"{k}: {v}" for k, v in payload.items() if isinstance(v, str) and v
    )


# ── Actor role from metadata ──────────────────────────────────────────────────

def _actor_role(kind: str, payload: dict) -> str:
    """Derive actor_role from signal metadata without relying on the model.

    Trusted-metadata rules:
      email.sent      → "business"  (we authored the outgoing message)
      email.received  → "customer"  (the customer authored the inbound message)
      call.transcript → "customer"  if direction='inbound', else "business"
      other           → "business"  (conservative default)

    For customer cancellations derived this way, apply_event will correctly
    produce is_confirmed=0 via the S3 customer-cancel gate in _write_version.
    """
    if kind == "email.sent":
        return "business"
    if kind == "email.received":
        return "customer"
    if kind == "call.transcript":
        if str(payload.get("direction", "")).lower() == "inbound":
            return "customer"
        return "business"
    return "business"


# ── Prompt template ───────────────────────────────────────────────────────────

_PROMPT_TEMPLATE = """\
You extract commitment updates from business signals.
A commitment is a confirmed or proposed delivery date or meeting appointment.
Return JSON only — no prose, no markdown.

Signal:
  kind:   {kind}
  source: {source}
  actor:  {actor_role} (the person who sent or said this)

Text:
{text}

Return exactly this JSON object (fill in values when found, otherwise null):
{{
  "event_type": null,
  "action": null,
  "business_key": null,
  "confirmed_date": null,
  "proposed_date": null,
  "prior_confirmed_date": null,
  "supporting_passage": null,
  "confidence": 0.0
}}

Field rules:
  event_type           — "delivery" | "meeting" | null
  action               — "commit" | "reaffirm" | "request_change" | "confirm_change" | "cancel" | null
  confirmed_date       — YYYY-MM-DD date the business is confirming/reaffirming
  proposed_date        — YYYY-MM-DD new date being requested (request_change only)
  prior_confirmed_date — YYYY-MM-DD previously-confirmed date now being changed
  business_key         — order number, booking ID, or similar (string or null)
  supporting_passage   — exact verbatim quote from the text above that supports this
  confidence           — 0.0–1.0; leave at 0.0 when uncertain

Reject: invoice notifications, payment receipts, shipping tracking, unrelated topics.
If event_type is null, all other fields must also be null and confidence must be 0.0."""


# ── Default model backend (production path) ───────────────────────────────────

def _default_model_fn(prompt: str) -> tuple[str, int]:
    """Call OpenAI gpt-4o-mini.  Requires OPENAI_API_KEY in the environment.

    Returns (response_text, tokens_used).
    Lazy-imported so the module loads cleanly without an API key.
    """
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract structured commitment events from business signals. "
                    "Return only valid JSON matching the requested schema."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=400,
        response_format={"type": "json_object"},
    )
    text   = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else max(len(text) // 4, 1)
    return text, tokens


# ── Parse and verify ─────────────────────────────────────────────────────────

def _clean_date(v: Any) -> Optional[str]:
    if isinstance(v, str) and _DATE_RE.match(v.strip()):
        return v.strip()
    return None


def _parse_and_verify(
    raw: str,
    actor_role: str,
    signal_text: str,
    thread_id: Optional[str],
    cache_hit: bool,
) -> Optional[ExtractionResult]:
    """Parse a raw model response and reject anything that cannot be verified.

    Rejects:
    - Invalid JSON
    - event_type not in SUPPORTED_EVENT_TYPES (including null)
    - action not in ACTIONS
    - confidence < _CONFIDENCE_THRESHOLD
    - supporting_passage absent or not a verbatim substring of signal_text
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    event_type = data.get("event_type")
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None  # null or unrecognised value

    action = data.get("action")
    if action not in ACTIONS:
        return None

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < _CONFIDENCE_THRESHOLD:
        return None

    passage = data.get("supporting_passage") or ""
    if not isinstance(passage, str) or not passage.strip():
        return None
    passage = passage.strip()
    # Hard verification: passage must exist verbatim in the original signal text.
    # This prevents hallucinated evidence from reaching the ledger.
    if passage not in signal_text:
        return None

    bkey = data.get("business_key")
    if not isinstance(bkey, str) or not bkey.strip():
        bkey = None

    return ExtractionResult(
        event_type=event_type,
        action=action,
        actor_role=actor_role,
        business_key=bkey,
        confirmed_date=_clean_date(data.get("confirmed_date")),
        proposed_date=_clean_date(data.get("proposed_date")),
        prior_confirmed_date=_clean_date(data.get("prior_confirmed_date")),
        thread_id=thread_id,
        supporting_passage=passage,
        confidence=confidence,
        cache_hit=cache_hit,
    )


# ── Main entry point ─────────────────────────────────────────────────────────

def extract_commitment_update(
    kind: str,
    payload: dict,
    source: str,
    *,
    budget: Optional[Budget] = None,
    cache_conn: Optional[sqlite3.Connection] = None,
    _model_fn: Optional[Callable[[str], tuple[str, int]]] = None,
) -> Optional[ExtractionResult]:
    """Extract a commitment update from a business signal.

    Args:
        kind:       Signal kind (e.g. 'email.received', 'call.transcript').
        payload:    Signal payload dict.
        source:     Signal source label (e.g. 'gmail_alice').
        budget:     Shared Budget for this session; None = unlimited.
        cache_conn: SQLite connection with an extraction_cache table (call
                    migrate_extraction_cache first); None = no caching.
        _model_fn:  Injected model backend for testing.
                    Signature: (prompt: str) -> (response: str, tokens: int).
                    Defaults to _default_model_fn (OpenAI gpt-4o-mini).

    Returns:
        ExtractionResult if a commitment update was found and verified.
        None when: not a commitment signal, confidence too low, supporting
        passage unverifiable, budget exhausted, or model error.
    """
    text = _signal_text(kind, payload)
    if not text.strip():
        return None

    role = _actor_role(kind, payload)
    thread_id: Optional[str] = None
    for key in ("thread_id", "message_id"):
        raw_tid = payload.get(key)
        if isinstance(raw_tid, str) and raw_tid.strip():
            thread_id = raw_tid.strip()
            break

    prompt = _PROMPT_TEMPLATE.format(
        kind=kind,
        source=source,
        actor_role=role,
        text=text[:_MAX_SIGNAL_CHARS],
    )
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    # ── Cache lookup ─────────────────────────────────────────────────────────
    raw_response: Optional[str] = None
    tokens_used: int = 0
    cache_hit = False

    if cache_conn is not None:
        row = cache_conn.execute(
            "SELECT response, tokens_used FROM extraction_cache WHERE prompt_hash=?",
            (prompt_hash,),
        ).fetchone()
        if row:
            raw_response = row[0]
            tokens_used  = int(row[1])
            cache_hit    = True
            if budget is not None:
                budget.record_cache_hit()

    # ── Model call ───────────────────────────────────────────────────────────
    if raw_response is None:
        if budget is not None and not budget.within_budget():
            return None  # budget exhausted; do not error — caller may still use cached results

        fn = _model_fn or _default_model_fn
        try:
            raw_response, tokens_used = fn(prompt)
        except Exception:
            return None  # model error — fail open (do not crash callers)

        if budget is not None:
            budget.charge(tokens_used)

        if cache_conn is not None and raw_response:
            now = datetime.now(timezone.utc).isoformat()
            try:
                cache_conn.execute(
                    "INSERT OR IGNORE INTO extraction_cache"
                    " (prompt_hash, response, tokens_used, cached_at)"
                    " VALUES (?,?,?,?)",
                    (prompt_hash, raw_response, tokens_used, now),
                )
                cache_conn.commit()
            except Exception:
                pass  # cache write failure is non-fatal

    if not raw_response:
        return None

    return _parse_and_verify(raw_response, role, text, thread_id, cache_hit)
