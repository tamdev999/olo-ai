"""olo/extractor.py — Commitment-update extractor (S4, corrected).

Extracts structured commitment events (delivery orders, meeting slots) from
business signals using a budget-limited, cached LLM call.

Design pillars
--------------
Budget-limited  — a Budget object caps total calls and tokens; extraction
                  returns None when exhausted.  Pricing uses the DeepSeek peak list
                  prices defined once in olo/m3/hindsight/budget.py.
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
Source-grounded  — (S1–S4 hardening) every extracted date must be a real
                  calendar date stated explicitly in the supporting passage;
                  alternatives ("X or Y"), ranges and competing dates are
                  rejected, never resolved by picking one; the passage must be
                  in the author's own text, not quoted/forwarded mail; a
                  business_key must appear in that text.
Explicit outcome — extract_commitment_outcome() returns an ExtractionOutcome
                  that separates EXTRACTED, NO_COMMITMENT, REJECTED,
                  MODEL_ERROR and BUDGET_EXHAUSTED, so failures are never
                  mistaken for "no commitment".

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
from datetime import date as _date
from datetime import datetime, timezone
from enum import Enum
from itertools import pairwise
from typing import Any, Callable, Optional

from olo.ledger import (
    ACTIONS,
    CANCEL_ACTION,
    DATED_ACTIONS,
    SUPPORTED_EVENT_TYPES,
    cancellation_field_error,
    is_valid_iso_date,
)
# DeepSeek pricing: one definition, shared with the Hindsight usage guard.
from olo.m3.hindsight.budget import COST_INPUT_PER_M as _DEEPSEEK_INPUT_PER_M
from olo.m3.hindsight.budget import COST_OUTPUT_PER_M as _DEEPSEEK_OUTPUT_PER_M
from olo.signal_content import (
    PREP_CONTENT_ERROR,
    PREP_MISSING_CONTENT,
    PREP_NO_DIRECTION,
    PREP_NO_TEXT,
    PREP_UNSUPPORTED_KIND,
    PreparedSignal,
    prepare_signal,
)

__all__ = [
    "EXTRACTION_VERSION",
    "ExtractionResult",
    "ExtractionOutcome",
    "ExtractionStatus",
    "Budget",
    "extract_commitment_outcome",
    "extract_prepared",
    "extract_commitment_update",
    "DEFAULT_DEEPSEEK_MODEL",
    "make_deepseek_model_fn",
    "migrate_extraction_cache",
]

_CONFIDENCE_THRESHOLD: float = 0.7

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
        #: Calls whose reported usage exceeded max_tokens_per_call despite the
        #: pre-call estimate (visible in report(); the estimate is conservative).
        self.per_call_overruns: int = 0

    def within_budget(self) -> bool:
        """True if another model call is within budget."""
        return (
            self.calls_used < self.max_calls
            and self.tokens_used < self.max_total_tokens
        )

    def call_refusal(self, estimated_tokens: int) -> Optional[str]:
        """Why a call estimated at *estimated_tokens* must not be made, or None.

        Enforces the per-call limit before any money is spent, as well as the
        call and total-token limits.
        """
        if self.calls_used >= self.max_calls:
            return f"call budget exhausted ({self.max_calls} calls)"
        if estimated_tokens > self.max_tokens_per_call:
            return (f"estimated {estimated_tokens} tokens exceeds the per-call limit "
                    f"of {self.max_tokens_per_call}")
        if self.tokens_used + estimated_tokens > self.max_total_tokens:
            return f"token budget exhausted ({self.max_total_tokens} tokens)"
        return None

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
            "per_call_overruns": self.per_call_overruns,
        }

    def charge_reply(self, usage: tuple[int, ...]) -> None:
        """Charge one model reply's usage.

        ``(input_tokens, output_tokens)`` is priced exactly.  A single combined
        count cannot be split, so it is priced entirely at the (higher) output
        rate — an upper bound, never an under-count.
        """
        if len(usage) >= 2:
            self.charge(int(usage[0]), output_tokens=int(usage[1]))
            total = int(usage[0]) + int(usage[1])
        else:
            total = int(usage[0]) if usage else 0
            self.charge(0, output_tokens=total)
        if total > self.max_tokens_per_call:
            self.per_call_overruns += 1


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


# ── Signal text (shared preparation) ──────────────────────────────────────────

def _signal_text(kind: str, payload: dict) -> str:
    """Full verification text of an inline signal payload.

    Thin view over olo.signal_content.prepare_signal — the one preparation
    path shared with the Hindsight item builder and the signal worker.
    """
    return prepare_signal(kind, payload).full_text


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

#: Version of the extraction contract (prompt construction + verification).
#: Part of every cache key: bump it whenever either changes so no cached
#: answer is reinterpreted under different rules.
#:   1 — S4 extractor;  2 — S1–S4 hardening + S5 part 1 (prepared signals);
#:   3 — post-audit batch 2 (MIME/charset/HTML rendering, quote detection).
EXTRACTION_VERSION: str = "3"

#: Output tokens assumed for a model function that does not declare its cap.
DEFAULT_MAX_OUTPUT_TOKENS: int = 400


#: DeepSeek chat model used for extraction.  ``deepseek-chat`` (DeepSeek V3,
#: used for the S4 results and the gate) was discontinued on 2026-07-24;
#: ``deepseek-flash`` is the supported model (GET /models, 2026-11-26).  It
#: thinks by default, so every request disables thinking explicitly.
DEFAULT_DEEPSEEK_MODEL: str = "deepseek-flash"
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
#: Request field selecting non-thinking mode (DeepSeek thinking-mode guide:
#: ``thinking.type`` takes precedence over ``reasoning_effort``).
DEEPSEEK_NON_THINKING: dict = {"thinking": {"type": "disabled"}}


def _deepseek_model_id(model: str, base_url: str, temperature: float, max_tokens: int) -> str:
    return (f"deepseek:{model}@{base_url}:thinking=disabled"
            f":temperature={temperature}:max_tokens={max_tokens}")


def make_deepseek_model_fn(
    model: str = DEFAULT_DEEPSEEK_MODEL,
    api_key: Optional[str] = None,
    base_url: str = DEEPSEEK_BASE_URL,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    temperature: float = 0,
) -> Callable[[str], tuple]:
    """Return a model function that calls DeepSeek via the OpenAI-compatible API.

    The returned function maps ``prompt -> (text, input_tokens, output_tokens)``
    when the API reports usage, else ``(text, estimated_total_tokens)``.  It
    carries ``model_id`` (part of the cache key) and ``max_output_tokens``
    (used by the per-call budget check).

    Args:
        model:       DeepSeek model name (default: deepseek-flash, non-thinking).
        api_key:     Explicit API key; falls back to DEEPSEEK_API_KEY env var.
        base_url:    API base URL (default: https://api.deepseek.com/v1).

    Reasoning tokens, should a reply report any, are charged as output on
    top of ``completion_tokens``: an upper bound whichever way the API counts
    them.
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

    def fn(prompt: str) -> tuple:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _system},
                {"role": "user",   "content": prompt},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            extra_body=DEEPSEEK_NON_THINKING,
        )
        text = resp.choices[0].message.content or ""
        usage = resp.usage
        if usage:
            details = getattr(usage, "completion_tokens_details", None)
            reasoning = int(getattr(details, "reasoning_tokens", 0) or 0)
            return (text, int(usage.prompt_tokens or 0),
                    int(usage.completion_tokens or 0) + reasoning)
        return text, max(len(text) // 4, 1)

    fn.model_id = _deepseek_model_id(model, base_url, temperature, max_tokens)  # type: ignore[attr-defined]
    fn.max_output_tokens = max_tokens  # type: ignore[attr-defined]
    return fn


def _default_model_fn(prompt: str) -> tuple:
    """Default production backend: DeepSeek via DEEPSEEK_API_KEY.

    Raises ValueError if DEEPSEEK_API_KEY is not set.
    For testing always inject _model_fn explicitly.
    """
    return make_deepseek_model_fn()(prompt)


_default_model_fn.model_id = _deepseek_model_id(  # type: ignore[attr-defined]
    DEFAULT_DEEPSEEK_MODEL, DEEPSEEK_BASE_URL, 0, DEFAULT_MAX_OUTPUT_TOKENS)
_default_model_fn.max_output_tokens = DEFAULT_MAX_OUTPUT_TOKENS  # type: ignore[attr-defined]


# ── Outcome types ────────────────────────────────────────────────────────────

class ExtractionStatus(str, Enum):
    """Why an extraction did or did not produce a result.

    Only EXTRACTED carries a result.  MODEL_ERROR and BUDGET_EXHAUSTED are
    retryable failures and must never be recorded as "no commitment".
    """
    EXTRACTED        = "extracted"         # verified commitment update
    NO_COMMITMENT    = "no_commitment"     # model found nothing relevant
    REJECTED         = "rejected"          # model claimed one; verification failed
    MODEL_ERROR      = "model_error"       # transport/model exception or unusable output
    BUDGET_EXHAUSTED = "budget_exhausted"  # call not made: budget or per-call limit
    CONTENT_ERROR    = "content_error"     # stored content missing/corrupt (integrity)
    NO_INPUT         = "no_input"          # signal carries no text


#: Statuses a worker should retry rather than finalise.
RETRYABLE_STATUSES: frozenset[ExtractionStatus] = frozenset({
    ExtractionStatus.MODEL_ERROR, ExtractionStatus.BUDGET_EXHAUSTED,
})


@dataclass(frozen=True)
class ExtractionOutcome:
    """Result of one extraction attempt.  ``result`` is set only when EXTRACTED."""
    status: ExtractionStatus
    result: Optional[ExtractionResult] = None
    reason: str = ""

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES


# ── Source evidence: date mentions and grounding ────────────────────────────

_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<year>\d{4})"

_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})\b"),                 # 2024-03-15
    re.compile(rf"\b{_MON}\s+{_DAY}(?:,?\s+{_YEAR})?\b", re.IGNORECASE),       # March 15[, 2024]
    re.compile(rf"\b{_DAY}\s+(?:of\s+)?{_MON}(?:,?\s+{_YEAR})?\b", re.IGNORECASE),  # 15 March [2024]
    re.compile(r"\b(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y4>\d{4})\b"),            # 3/15/2024
)


@dataclass(frozen=True)
class _DateMention:
    start: int
    end: int
    dates: frozenset[str]   # full ISO dates this mention can denote (may be empty)
    month_days: frozenset[tuple[int, int]]  # (month, day) it can denote


def _iso(y: int, m: int, d: int) -> Optional[str]:
    try:
        return datetime(y, m, d).date().isoformat()
    except ValueError:
        return None


def _date_mentions(text: str) -> list[_DateMention]:
    """Explicit date mentions in *text*, left to right, non-overlapping.

    A year-less mention ("April 10") denotes a month/day but no full date.
    An all-numeric d/m vs m/d mention denotes every valid reading.
    Relative expressions ("next Tuesday") are not dates here.
    """
    found: list[_DateMention] = []
    taken: list[tuple[int, int]] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            s, e = m.span()
            if any(s < te and ts < e for ts, te in taken):
                continue
            g = m.groupdict()
            dates: set[str] = set()
            mds: set[tuple[int, int]] = set()
            if g.get("y") is not None:
                y, mo, d = int(g["y"]), int(g["m"]), int(g["d"])
                iso = _iso(y, mo, d)
                if iso:
                    dates.add(iso)
                    mds.add((mo, d))
            elif g.get("mon") is not None:
                mo = _MONTHS[g["mon"].lower().rstrip(".")]
                d = int(g["day"])
                if _iso(2000, mo, d):            # leap year: any valid month/day
                    mds.add((mo, d))
                    if g.get("year"):
                        iso = _iso(int(g["year"]), mo, d)
                        if iso:
                            dates.add(iso)
            else:
                a, b, y = int(g["a"]), int(g["b"]), int(g["y4"])
                for mo, d in {(a, b), (b, a)}:
                    iso = _iso(y, mo, d)
                    if iso:
                        dates.add(iso)
                        mds.add((mo, d))
            if not mds:
                continue
            taken.append((s, e))
            found.append(_DateMention(s, e, frozenset(dates), frozenset(mds)))
    found.sort(key=lambda x: x.start)
    return found


#: A year-less date ("April 10") is resolved against the signal's trusted
#: timestamp only when exactly one candidate year lies within this many days
#: of it; otherwise it stays unresolved.
_YEARLESS_WINDOW_DAYS: int = 183


def _resolve_yearless(month: int, day: int, reference: _date) -> Optional[str]:
    """The single plausible full date for a year-less mention, else None."""
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        iso = _iso(year, month, day)
        if iso and abs((_date.fromisoformat(iso) - reference).days) <= _YEARLESS_WINDOW_DAYS:
            candidates.append(iso)
    return candidates[0] if len(candidates) == 1 else None


def _grounds(mention: _DateMention, iso: str, reference: Optional[_date] = None) -> bool:
    """True when the mention unambiguously denotes exactly *iso*.

    A year-less mention grounds a date only through a trusted reference
    timestamp and only when that resolution is unique.
    """
    if mention.dates == frozenset({iso}):
        return True
    if not mention.dates and reference is not None and len(mention.month_days) == 1:
        (month, day), = mention.month_days
        return _resolve_yearless(month, day, reference) == iso
    return False


def _explained_by(mention: _DateMention, extracted: set[str]) -> bool:
    """True when the mention refers to one of the extracted dates."""
    if mention.dates & extracted:
        return True
    return any((int(x[5:7]), int(x[8:10])) in mention.month_days for x in extracted)


_SENTENCE_BREAK = re.compile(r"[.!?\n]")


def _context(text: str, start: int, end: int) -> str:
    """The sentence(s) of *text* that contain the span [start, end)."""
    left = start
    while left > 0 and not _SENTENCE_BREAK.match(text[left - 1]):
        left -= 1
    right = end
    while right < len(text) and not _SENTENCE_BREAK.match(text[right]):
        right += 1
    return text[left:right]


_ALT_GAP = re.compile(r"\bor\b|/|\bversus\b|\bvs\.?\b", re.IGNORECASE)
_RANGE_GAP = re.compile(r"\band\b|\bto\b|-|–", re.IGNORECASE)

#: Actions whose event supersedes an earlier date (one unextracted earlier
#: date in the same sentence is tolerated as that superseded date).
_SUPERSEDING_ACTIONS: frozenset[str] = frozenset({"request_change", "confirm_change", "cancel"})


def _date_evidence_error(
    action: str,
    dates: dict[str, Optional[str]],
    passage: str,
    context: str,
    reference: Optional[_date] = None,
) -> Optional[str]:
    """Why the extracted dates are not supported by the source, or None.

    * every non-null extracted date is stated in the supporting passage —
      with its year, or year-less but uniquely resolvable from the signal's
      trusted timestamp (*reference*);
    * the passage's sentence offers no alternatives ("X or Y", "between X
      and Y") — an alternative is never resolved by picking one;
    * no unexplained date competes with the extraction: none for commit /
      reaffirm, at most one (the superseded date) for superseding actions.
    """
    extracted = {v for v in dates.values() if v is not None}
    passage_mentions = _date_mentions(passage)
    for field, value in dates.items():
        if value is not None and not any(_grounds(m, value, reference) for m in passage_mentions):
            return f"{field}={value} is not stated explicitly and unambiguously in the supporting passage"

    ctx_mentions = _date_mentions(context)
    for a, b in pairwise(ctx_mentions):
        gap = context[a.end:b.start]
        if len(gap) <= 30 and _ALT_GAP.search(gap):
            return "source offers alternative dates; none is definite"
        before = context[max(0, a.start - 20):a.start].lower()
        if len(gap) <= 12 and "between" in before and _RANGE_GAP.search(gap):
            return "source gives a date range; no single date is definite"

    unexplained = [m for m in ctx_mentions if not _explained_by(m, extracted)]
    allowed = 1 if (action in _SUPERSEDING_ACTIONS and dates.get("prior_confirmed_date") is None) else 0
    if len(unexplained) > allowed:
        return f"{len(unexplained)} other date(s) in the same sentence compete with the extraction"
    return None


# ── Parse and verify ─────────────────────────────────────────────────────────

_DATE_KEYS: tuple[str, ...] = ("confirmed_date", "proposed_date", "prior_confirmed_date")


def _reference_date(prepared: PreparedSignal) -> Optional[_date]:
    if not prepared.occurred_at:
        return None
    try:
        return _date.fromisoformat(prepared.occurred_at[:10])
    except ValueError:
        return None


def _parse_and_verify_outcome(
    raw: str,
    prepared: PreparedSignal,
    cache_hit: bool,
) -> ExtractionOutcome:
    """Parse a raw model response and verify it against the prepared signal.

    Verification is always against the complete original text; the evidence
    must sit in the author's own (unquoted) text.  Nothing is coerced: any
    malformed field rejects the whole extraction.
    """
    S = ExtractionStatus
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return ExtractionOutcome(S.MODEL_ERROR, reason="model output is not valid JSON")
    if not isinstance(data, dict):
        return ExtractionOutcome(S.MODEL_ERROR, reason="model output is not a JSON object")

    event_type = data.get("event_type")
    if event_type is None:
        return ExtractionOutcome(S.NO_COMMITMENT, reason="model found no commitment")
    if event_type not in SUPPORTED_EVENT_TYPES:
        return ExtractionOutcome(S.REJECTED, reason=f"unsupported event_type {event_type!r}")

    action = data.get("action")
    if action not in ACTIONS:
        return ExtractionOutcome(S.REJECTED, reason=f"unsupported action {action!r}")

    # Cancellation contract (shared with the ledger): raw values, never remapped.
    if action == CANCEL_ACTION:
        err = cancellation_field_error(
            confirmed_date=data.get("confirmed_date"),
            proposed_date=data.get("proposed_date"),
            prior_confirmed_date=data.get("prior_confirmed_date"),
        )
        if err:
            return ExtractionOutcome(S.REJECTED, reason=err)

    # Every date must be null or a real calendar date (shared validator).
    dates: dict[str, Optional[str]] = {}
    for key in _DATE_KEYS:
        value = data.get(key)
        if value is not None and not is_valid_iso_date(value):
            return ExtractionOutcome(S.REJECTED, reason=f"{key}={value!r} is not a valid date")
        dates[key] = value
    if action in DATED_ACTIONS and dates["confirmed_date"] is None:
        return ExtractionOutcome(S.REJECTED, reason=f"action={action!r} has no confirmed_date")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < _CONFIDENCE_THRESHOLD:
        return ExtractionOutcome(S.REJECTED, reason=f"low confidence {confidence:.2f}")

    passage = data.get("supporting_passage") or ""
    if not isinstance(passage, str) or not passage.strip():
        return ExtractionOutcome(S.REJECTED, reason="no supporting passage")
    passage = passage.strip()
    # Hard verification against the FULL original text (not the prompt window).
    if passage not in prepared.full_text:
        return ExtractionOutcome(S.REJECTED, reason="supporting passage not in source text")

    # The evidence must be the author's own words, not quoted correspondence.
    authored = prepared.authored_text
    pos = authored.find(passage)
    if pos < 0:
        return ExtractionOutcome(
            S.REJECTED, reason="supporting passage is only in quoted/forwarded text")

    date_err = _date_evidence_error(
        action, dates, passage, _context(authored, pos, pos + len(passage)),
        _reference_date(prepared))
    if date_err:
        return ExtractionOutcome(S.REJECTED, reason=date_err)

    bkey = data.get("business_key")
    if bkey is not None and not isinstance(bkey, str):
        return ExtractionOutcome(S.REJECTED, reason="business_key is not a string")
    bkey = bkey.strip() if isinstance(bkey, str) and bkey.strip() else None
    if bkey is not None and bkey not in authored:
        return ExtractionOutcome(
            S.REJECTED, reason=f"business_key {bkey!r} does not appear in the author's text")

    return ExtractionOutcome(S.EXTRACTED, ExtractionResult(
        event_type=event_type,
        action=action,
        actor_role=prepared.actor_role or "",
        business_key=bkey,
        confirmed_date=dates["confirmed_date"],
        proposed_date=dates["proposed_date"],
        prior_confirmed_date=dates["prior_confirmed_date"],
        thread_id=prepared.thread_id,
        supporting_passage=passage,
        confidence=confidence,
        cache_hit=cache_hit,
    ))


# ── Cache key, call estimate, reply shape ────────────────────────────────────

def _cache_key(prompt: str, model_id: str) -> str:
    """Cache key: extraction contract version + model identity + exact prompt."""
    material = json.dumps(
        {"extraction_version": EXTRACTION_VERSION, "model": model_id, "prompt": prompt},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _estimate_call_tokens(prompt: str, max_output_tokens: int) -> int:
    """Conservative token estimate for one call: ~3 chars/token + output cap."""
    return -(-len(prompt) // 3) + max_output_tokens


def _split_reply(reply: object) -> tuple[str, tuple[int, ...]]:
    """``(text, usage)`` from ``(text, total)`` or ``(text, input, output)``."""
    if not isinstance(reply, tuple) or len(reply) not in (2, 3):
        raise ValueError("model reply must be (text, total) or (text, input, output)")
    text = reply[0] if isinstance(reply[0], str) else ""
    return text, tuple(int(x or 0) for x in reply[1:])


# ── Main entry points ────────────────────────────────────────────────────────

_PREP_TO_STATUS: dict[str, ExtractionStatus] = {
    PREP_NO_TEXT:          ExtractionStatus.NO_INPUT,
    PREP_UNSUPPORTED_KIND: ExtractionStatus.NO_INPUT,
    PREP_NO_DIRECTION:     ExtractionStatus.REJECTED,
    PREP_MISSING_CONTENT:  ExtractionStatus.CONTENT_ERROR,
    PREP_CONTENT_ERROR:    ExtractionStatus.CONTENT_ERROR,
}


def extract_prepared(
    prepared: PreparedSignal,
    source: str,
    *,
    budget: Optional[Budget] = None,
    cache_conn: Optional[sqlite3.Connection] = None,
    model_fn: Optional[Callable[[str], tuple]] = None,
    model_id: Optional[str] = None,
) -> ExtractionOutcome:
    """Extract a commitment update from a prepared signal.

    Only a fully verified extraction is EXTRACTED.  Model/transport errors
    and an exhausted budget are distinct, retryable statuses; unreadable
    content is CONTENT_ERROR; none is ever reported as NO_COMMITMENT.
    Unusable model output is not cached.  A call whose conservative token
    estimate exceeds the per-call limit is never made.
    """
    S = ExtractionStatus
    if not prepared.ready:
        return ExtractionOutcome(_PREP_TO_STATUS.get(prepared.status, S.NO_INPUT),
                                 reason=prepared.reason)

    prompt = _PROMPT_TEMPLATE.format(
        kind=prepared.kind,
        source=source,
        actor_role=prepared.actor_role,
        text=_extract_relevant_window(prepared.full_text),
    )
    fn = model_fn or _default_model_fn
    resolved_model = model_id or getattr(fn, "model_id", None) or "unidentified-model"
    key = _cache_key(prompt, resolved_model)

    raw_response: Optional[str] = None
    tokens_used = 0
    if cache_conn is not None:
        row = cache_conn.execute(
            "SELECT response, tokens_used FROM extraction_cache WHERE prompt_hash=?",
            (key,),
        ).fetchone()
        if row:
            raw_response = row[0]
            if budget is not None:
                budget.record_cache_hit()

    if raw_response is not None:
        outcome = _parse_and_verify_outcome(raw_response, prepared, True)
    else:
        if budget is not None:
            refusal = budget.call_refusal(_estimate_call_tokens(
                prompt, int(getattr(fn, "max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS))))
            if refusal:
                return ExtractionOutcome(S.BUDGET_EXHAUSTED, reason=refusal)
        try:
            raw_response, usage = _split_reply(fn(prompt))
        except Exception as exc:
            return ExtractionOutcome(S.MODEL_ERROR, reason=f"model call failed: {type(exc).__name__}")
        tokens_used = sum(usage)
        if budget is not None:
            budget.charge_reply(usage)
        if not raw_response:
            return ExtractionOutcome(S.MODEL_ERROR, reason="model returned an empty response")

        outcome = _parse_and_verify_outcome(raw_response, prepared, False)
        # Cache only usable model output, so a malformed reply is retried.
        if cache_conn is not None and outcome.status != S.MODEL_ERROR:
            now = datetime.now(timezone.utc).isoformat()
            try:
                cache_conn.execute(
                    "INSERT OR IGNORE INTO extraction_cache"
                    " (prompt_hash, response, tokens_used, cached_at)"
                    " VALUES (?,?,?,?)",
                    (key, raw_response, tokens_used, now),
                )
                cache_conn.commit()
            except sqlite3.Error:
                pass  # the cache is an optimisation; a write failure costs a retry

    if outcome.status != S.EXTRACTED or outcome.result is None:
        return outcome
    result = outcome.result

    # For call transcripts: use speaker markers to confirm or override actor_role.
    if prepared.kind == "call.transcript" and _has_speaker_markers(prepared.full_text):
        attributed = _find_speaker_for_passage(prepared.full_text, result.supporting_passage)
        if attributed is None:
            # Markers present but passage not attributable → unresolved.
            return ExtractionOutcome(
                S.REJECTED, reason="passage cannot be attributed to a speaker")
        if attributed != result.actor_role:
            # Transcript attribution overrides direction-based initial role.
            result = replace(result, actor_role=attributed)

    return ExtractionOutcome(S.EXTRACTED, result)


def extract_commitment_outcome(
    kind: str,
    payload: dict,
    source: str,
    *,
    budget: Optional[Budget] = None,
    cache_conn: Optional[sqlite3.Connection] = None,
    _model_fn: Optional[Callable[[str], tuple]] = None,
    model_id: Optional[str] = None,
    occurred_at: object = None,
) -> ExtractionOutcome:
    """Prepare an inline signal payload and extract from it.

    ``occurred_at`` (the signal's trusted timestamp) enables unambiguous
    year-less dates; without it, year-less dates stay unresolved.
    """
    prepared = prepare_signal(kind, payload, occurred_at=occurred_at)
    return extract_prepared(
        prepared, source, budget=budget, cache_conn=cache_conn,
        model_fn=_model_fn, model_id=model_id,
    )


def extract_commitment_update(
    kind: str,
    payload: dict,
    source: str,
    *,
    budget: Optional[Budget] = None,
    cache_conn: Optional[sqlite3.Connection] = None,
    _model_fn: Optional[Callable[[str], tuple]] = None,
) -> Optional[ExtractionResult]:
    """Return the verified ExtractionResult, or None for every other outcome.

    Compatibility wrapper around extract_commitment_outcome().  Callers that
    must tell a failure from "no commitment" (the signal worker) use
    extract_prepared() / extract_commitment_outcome() instead.

    Args:
        kind:       Signal kind (e.g. 'email.received', 'call.transcript').
        payload:    Signal payload dict.
        source:     Signal source label (e.g. 'gmail_alice').
        budget:     Shared Budget for this session; None = unlimited.
        cache_conn: SQLite connection with an extraction_cache table (call
                    migrate_extraction_cache first); None = no caching.
        _model_fn:  Injected model backend for testing.
                    Signature: prompt -> (text, total) or (text, input, output).
                    Defaults to _default_model_fn (DeepSeek via DEEPSEEK_API_KEY).
    """
    return extract_commitment_outcome(
        kind, payload, source,
        budget=budget, cache_conn=cache_conn, _model_fn=_model_fn,
    ).result
