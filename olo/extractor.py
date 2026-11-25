"""olo/extractor.py — Commitment-update extractor (S4, corrected).

Extracts structured commitment events (delivery orders, meeting slots) from
business signals using a budget-limited, cached LLM call.

Design pillars
--------------
Budget-limited  — a Budget object caps total calls and tokens; extraction
                  returns None when exhausted.  Pricing uses DeepSeek V3 rates
                  ($0.14/1M input, $0.28/1M output) from olo/m3/hindsight/budget.py.
Cached          — an optional SQLite cache connection stores
                  prompt_hash → response so identical prompts are free.
Verifiable      — supporting_passage must appear verbatim in the FULL original
                  signal text (not the potentially-windowed prompt text); unverifiable
                  extractions are rejected outright.
Speaker from transcript — for call.transcript, actor_role is confirmed by finding
                  which Agent:/Customer: segment contains the supporting_passage.
                  If the transcript uses speaker markers but the passage cannot be
                  attributed, the event is left unresolved (returns None).
Long-transcript safe — a keyword-density window finds the most commitment-dense
                  section for the prompt while verification always uses the full text.
Reject-not-guess — confidence < 0.7 → None; unattributable speaker → None.

Extraction is intentionally separate from ledger writes: callers receive an
ExtractionResult and pass it to apply_event themselves.  When actor_role is
"customer" and action is "cancel", apply_event will produce is_confirmed=0
(a proposal, not a confirmed cancellation) via the S3 _write_version gate.

No side effects at import time.  No Hindsight dependency.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from olo.ledger import (
    ACTIONS,
    CANCEL_ACTION,
    SUPPORTED_EVENT_TYPES,
    cancellation_field_error,
)

__all__ = [
    "ExtractionResult",
    "Budget",
    "extract_commitment_update",
    "make_deepseek_model_fn",
    "migrate_extraction_cache",
]

_CONFIDENCE_THRESHOLD: float = 0.7
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# DeepSeek V3 pricing (from olo/m3/hindsight/budget.py)
_DEEPSEEK_INPUT_PER_M:  float = 0.14   # USD per 1M input  tokens
_DEEPSEEK_OUTPUT_PER_M: float = 0.28   # USD per 1M output tokens

# Keyword-density window for long signals
_MAX_SIGNAL_CHARS: int = 4_000
_WINDOW_STEP:      int = 200

# Speaker marker regex — matches "Agent:" or "Customer:" as whole words
# anywhere in a transcript (including mid-sentence after punctuation).
_SPEAKER_RE = re.compile(r"\b(Agent|Customer)\s*:", re.IGNORECASE)

# Keywords used to locate the most commitment-dense window in long transcripts
_COMMITMENT_KW = (
    "confirm", "schedul", "deliver", "appoint", "meet",
    "cancel", "reschedul", "change", "date", "booking",
)


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExtractionResult:
    """Structured output of one successful extract_commitment_update call.

    Returned only when:
      - signal clearly describes a delivery or meeting commitment
      - confidence >= _CONFIDENCE_THRESHOLD (0.7)
      - supporting_passage is verified as a verbatim substring of the original
        signal text (never the prompt window)
      - for call.transcript with Agent:/Customer: markers, the supporting_passage
        is attributable to a specific speaker

    ``None`` is returned (not an ExtractionResult) for non-commitment signals,
    low-confidence extractions, unverifiable passages, budget exhaustion, or
    when speaker attribution is uncertain on a marked transcript.
    """
    event_type:           str            # "delivery" | "meeting"
    action:               str            # ledger action name
    actor_role:           str            # "business" | "customer"
    business_key:         Optional[str]  # order/booking ID, or None
    confirmed_date:       Optional[str]  # YYYY-MM-DD, or None
    proposed_date:        Optional[str]  # YYYY-MM-DD (request_change only)
    prior_confirmed_date: Optional[str]  # YYYY-MM-DD (date being superseded)
    thread_id:            Optional[str]  # from payload metadata
    supporting_passage:   str            # verbatim quote (verified in full text)
    confidence:           float          # always >= _CONFIDENCE_THRESHOLD when set
    cache_hit:            bool = False   # True when served from cache


# ── Budget guard ─────────────────────────────────────────────────────────────

class Budget:
    """Mutable call and token budget for a session.

    Tracks input and output tokens separately for accurate DeepSeek V3 pricing.
    Share one instance across multiple extract_commitment_update calls.
    Not thread-safe; use a per-request instance in concurrent settings.
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
        # Usage counters
        self.calls_used:    int   = 0
        self.tokens_used:   int   = 0   # input + output combined
        self.cache_hits:    int   = 0
        self.cost_usd:      float = 0.0

    def within_budget(self) -> bool:
        """True if another model call is within budget."""
        return (
            self.calls_used < self.max_calls
            and self.tokens_used < self.max_total_tokens
        )

    def charge(self, tokens: int, *, output_tokens: int = 0) -> None:
        """Record usage for one successful (non-cached) model call.

        Args:
            tokens:        Total tokens used (input + output), or just input
                           when output_tokens is provided separately.
            output_tokens: Output tokens for separate DeepSeek pricing; when
                           provided, ``tokens`` is treated as input-only.
        """
        self.calls_used += 1
        input_t  = tokens
        output_t = output_tokens
        self.tokens_used += input_t + output_t
        self.cost_usd += (
            input_t  * _DEEPSEEK_INPUT_PER_M  / 1_000_000
            + output_t * _DEEPSEEK_OUTPUT_PER_M / 1_000_000
        )

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
    """Extract the full meaningful text from a signal payload.

    Returns the COMPLETE text, never truncated — callers that need a windowed
    version for a prompt should use _extract_relevant_window separately.
    """
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


def _extract_relevant_window(text: str, max_chars: int = _MAX_SIGNAL_CHARS) -> str:
    """Return at most max_chars characters centred on the most commitment-dense region.

    When the text fits within max_chars, returns it unchanged (no trimming,
    no markers added).  When truncation is necessary, keyword-density scoring
    finds the best window so relevant passages are unlikely to be lost.

    The returned slice is always a contiguous substring of the original text,
    so ``supporting_passage`` verification against the full original text still
    succeeds for any passage the model quotes from within the window.
    """
    if len(text) <= max_chars:
        return text

    best_start = 0
    best_score = -1
    half_win = max_chars // 2

    for start in range(0, len(text) - half_win + 1, _WINDOW_STEP):
        snippet = text[start : start + half_win].lower()
        score = sum(snippet.count(kw) for kw in _COMMITMENT_KW)
        if score > best_score:
            best_score = score
            best_start = start

    # Expand to full max_chars centred on best_start
    start = max(0, best_start - max_chars // 4)
    end   = min(len(text), start + max_chars)
    if end - start < max_chars and start > 0:
        start = max(0, end - max_chars)

    # Snap to word boundary to avoid splitting mid-token
    while start > 0 and text[start] not in (" ", "\n", ".", ",", ";"):
        start -= 1

    # Re-clamp end after the snap (start may have moved backward)
    end = min(len(text), start + max_chars)

    return text[start:end]


# ── Speaker attribution from transcript markers ───────────────────────────────

def _has_speaker_markers(text: str) -> bool:
    """True if the text contains ``Agent:`` or ``Customer:`` labelling."""
    return bool(_SPEAKER_RE.search(text))


def _find_speaker_for_passage(transcript: str, passage: str) -> Optional[str]:
    """Return the speaker role for the given passage in the transcript.

    Finds the last ``Agent:`` or ``Customer:`` marker that precedes the
    passage's position in the transcript.

    Returns:
        "business"  — passage is in an Agent: segment
        "customer"  — passage is in a Customer: segment
        None        — passage not in transcript, or no marker precedes it
                      (passage occurs before the first speaker label)
    """
    if not passage or passage not in transcript:
        return None

    pos = transcript.find(passage)
    last_speaker: Optional[str] = None

    for m in _SPEAKER_RE.finditer(transcript):
        if m.start() <= pos:
            last_speaker = m.group(1).lower()
        else:
            break  # markers are ordered; none after pos can apply

    if last_speaker is None:
        return None  # passage is before any speaker label

    return "business" if last_speaker == "agent" else "customer"


# ── Actor role from metadata ──────────────────────────────────────────────────

def _actor_role(kind: str, payload: dict) -> str:
    """Initial actor_role from signal metadata.

    For emails this is definitive (we authored sent mail, customers authored
    received mail).  For call.transcript this is a direction-based fallback;
    when the transcript uses Agent:/Customer: markers, _refine_call_actor_role
    will confirm or override this value after the model identifies the
    supporting_passage.

    Trusted-metadata rules:
      email.sent      → "business"
      email.received  → "customer"
      call.transcript → "customer" if direction='inbound', else "business"
      other           → "business" (conservative default)
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


# ── DeepSeek model backend ────────────────────────────────────────────────────

def make_deepseek_model_fn(
    model: str = "deepseek-chat",
    api_key: Optional[str] = None,
    base_url: str = "https://api.deepseek.com/v1",
    max_tokens: int = 400,
    temperature: float = 0,
) -> Callable[[str], tuple[str, int]]:
    """Return a model function that calls DeepSeek via the OpenAI-compatible API.

    Uses Olo's established DeepSeek configuration (DEEPSEEK_API_KEY env var,
    deepseek-chat model, api.deepseek.com endpoint).  The returned function has
    the standard ``(prompt: str) -> (response: str, tokens: int)`` signature.

    Args:
        model:       DeepSeek model name (default: deepseek-chat / DeepSeek V3).
        api_key:     Explicit API key; falls back to DEEPSEEK_API_KEY env var.
        base_url:    API base URL (default: https://api.deepseek.com/v1).
        max_tokens:  Maximum output tokens per call.
        temperature: Sampling temperature (0 = deterministic).

    Raises:
        ValueError: if no API key is available.
    """
    resolved_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not resolved_key:
        raise ValueError(
            "DEEPSEEK_API_KEY is not set. "
            "Set the environment variable or pass api_key explicitly."
        )

    from openai import OpenAI  # noqa: PLC0415 — lazy import, no side effects at load

    client = OpenAI(api_key=resolved_key, base_url=base_url)
    _system = (
        "You extract structured commitment events from business signals. "
        "Return only valid JSON matching the requested schema."
    )

    def fn(prompt: str) -> tuple[str, int]:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _system},
                {"role": "user",   "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        text = resp.choices[0].message.content or ""
        # Use separate input/output counts when available for accurate pricing
        if resp.usage:
            in_t  = resp.usage.prompt_tokens or 0
            out_t = resp.usage.completion_tokens or 0
            total = in_t + out_t
        else:
            total = max(len(text) // 4, 1)
        return text, total

    return fn


def _default_model_fn(prompt: str) -> tuple[str, int]:
    """Default production backend: DeepSeek via DEEPSEEK_API_KEY.

    Raises ValueError if DEEPSEEK_API_KEY is not set.
    For testing always inject _model_fn explicitly.
    """
    return make_deepseek_model_fn()(prompt)


# ── Parse and verify ─────────────────────────────────────────────────────────

def _clean_date(v: Any) -> Optional[str]:
    if isinstance(v, str) and _DATE_RE.match(v.strip()):
        return v.strip()
    return None


def _parse_and_verify(
    raw: str,
    actor_role: str,
    full_text: str,
    thread_id: Optional[str],
    cache_hit: bool,
) -> Optional[ExtractionResult]:
    """Parse a raw model response and reject anything that cannot be verified.

    ``full_text`` is the complete original signal text (never the windowed
    prompt text) — verification is always against the full source.

    Rejects:
    - Invalid JSON
    - event_type not in SUPPORTED_EVENT_TYPES (including null)
    - action not in ACTIONS
    - confidence < _CONFIDENCE_THRESHOLD
    - supporting_passage absent or not a verbatim substring of full_text
    - action='cancel' violating the cancellation contract
      (olo.ledger.cancellation_field_error)
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    event_type = data.get("event_type")
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None

    action = data.get("action")
    if action not in ACTIONS:
        return None

    # Cancellation contract (shared with the ledger): checked on the RAW
    # values so a misplaced or malformed date is rejected, never remapped.
    if action == CANCEL_ACTION and cancellation_field_error(
        confirmed_date=data.get("confirmed_date"),
        proposed_date=data.get("proposed_date"),
        prior_confirmed_date=data.get("prior_confirmed_date"),
    ):
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
    # Hard verification against the FULL original text (not the prompt window).
    if passage not in full_text:
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

    Actor role for call.transcript:
      1. Initial role comes from direction (inbound → customer, else business).
      2. After the model identifies the supporting_passage, the transcript is
         re-checked for Agent:/Customer: markers:
           - If markers present and passage attributed to a speaker: use that
             role (overrides direction when they disagree).
           - If markers present but passage unattributable: return None.
           - If no markers: keep direction-based role.

    Long transcript handling:
      _signal_text extracts the full original text; _extract_relevant_window
      provides a keyword-dense window for the prompt.  Verification is always
      performed against the full original text.

    Args:
        kind:       Signal kind (e.g. 'email.received', 'call.transcript').
        payload:    Signal payload dict.
        source:     Signal source label (e.g. 'gmail_alice').
        budget:     Shared Budget for this session; None = unlimited.
        cache_conn: SQLite connection with an extraction_cache table (call
                    migrate_extraction_cache first); None = no caching.
        _model_fn:  Injected model backend for testing.
                    Signature: (prompt: str) -> (response: str, tokens: int).
                    Defaults to _default_model_fn (DeepSeek via DEEPSEEK_API_KEY).

    Returns:
        ExtractionResult if a commitment update was found and verified.
        None when: not a commitment signal, confidence too low, supporting
        passage unverifiable, budget exhausted, model error, or speaker
        attribution uncertain on a marked transcript.
    """
    full_text = _signal_text(kind, payload)
    if not full_text.strip():
        return None

    role = _actor_role(kind, payload)
    thread_id: Optional[str] = None
    for key in ("thread_id", "message_id"):
        raw_tid = payload.get(key)
        if isinstance(raw_tid, str) and raw_tid.strip():
            thread_id = raw_tid.strip()
            break

    # Window the text for the prompt; verification still uses full_text.
    prompt_text = _extract_relevant_window(full_text)
    prompt = _PROMPT_TEMPLATE.format(
        kind=kind,
        source=source,
        actor_role=role,
        text=prompt_text,
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
            return None

        fn = _model_fn or _default_model_fn
        try:
            raw_response, tokens_used = fn(prompt)
        except Exception:
            return None

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
                pass

    if not raw_response:
        return None

    # ── Parse, verify, then refine call attribution ───────────────────────────
    result = _parse_and_verify(raw_response, role, full_text, thread_id, cache_hit)
    if result is None:
        return None

    # For call transcripts: use speaker markers to confirm or override actor_role.
    if kind == "call.transcript":
        transcript = str(payload.get("transcript", ""))
        if _has_speaker_markers(transcript):
            attributed = _find_speaker_for_passage(transcript, result.supporting_passage)
            if attributed is None:
                # Markers present but passage not attributable → unresolved.
                # We cannot safely attribute this commitment to either party.
                return None
            if attributed != result.actor_role:
                # Transcript attribution overrides direction-based initial role.
                result = replace(result, actor_role=attributed)

    return result
