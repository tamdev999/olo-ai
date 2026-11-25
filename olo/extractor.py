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

__all__ = [
    "ExtractionResult",
    "ExtractionOutcome",
    "ExtractionStatus",
    "Budget",
    "extract_commitment_outcome",
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
    BUDGET_EXHAUSTED = "budget_exhausted"  # call not made: budget spent
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


# ── Source evidence: authored text, date mentions, grounding ─────────────────

#: Lines that start a quoted/forwarded block in an email body.  Everything
#: from the first such line onward is treated as not authored by the sender.
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

_EMAIL_KINDS: frozenset[str] = frozenset({"email.sent", "email.received", "email.content"})


def _authored_text(kind: str, full_text: str) -> str:
    """The part of the signal written by its author (quoted replies removed).

    For email kinds: drops ``>``-quoted lines and everything from the first
    reply/forward header line onward.  Other kinds are returned unchanged.
    """
    if kind not in _EMAIL_KINDS:
        return full_text
    kept: list[str] = []
    for line in full_text.splitlines():
        if _REPLY_HEADER_RE.match(line):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept)


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


def _grounds(mention: _DateMention, iso: str) -> bool:
    """True when the mention unambiguously denotes exactly *iso*."""
    return mention.dates == frozenset({iso})


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
) -> Optional[str]:
    """Why the extracted dates are not supported by the source, or None.

    * every non-null extracted date is stated explicitly and unambiguously
      in the supporting passage (year included);
    * the passage's sentence offers no alternatives ("X or Y", "between X
      and Y") — an alternative is never resolved by picking one;
    * no unexplained date competes with the extraction: none for commit /
      reaffirm, at most one (the superseded date) for superseding actions.
    """
    extracted = {v for v in dates.values() if v is not None}
    passage_mentions = _date_mentions(passage)
    for field, value in dates.items():
        if value is not None and not any(_grounds(m, value) for m in passage_mentions):
            return f"{field}={value} is not stated explicitly in the supporting passage"

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


def _parse_and_verify_outcome(
    raw: str,
    kind: str,
    actor_role: str,
    full_text: str,
    thread_id: Optional[str],
    cache_hit: bool,
) -> ExtractionOutcome:
    """Parse a raw model response and verify it against the source signal.

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
    if passage not in full_text:
        return ExtractionOutcome(S.REJECTED, reason="supporting passage not in source text")

    # The evidence must be the author's own words, not quoted correspondence.
    authored = _authored_text(kind, full_text)
    pos = authored.find(passage)
    if pos < 0:
        return ExtractionOutcome(
            S.REJECTED, reason="supporting passage is only in quoted/forwarded text")

    date_err = _date_evidence_error(
        action, dates, passage, _context(authored, pos, pos + len(passage)))
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
        actor_role=actor_role,
        business_key=bkey,
        confirmed_date=dates["confirmed_date"],
        proposed_date=dates["proposed_date"],
        prior_confirmed_date=dates["prior_confirmed_date"],
        thread_id=thread_id,
        supporting_passage=passage,
        confidence=confidence,
        cache_hit=cache_hit,
    ))


# ── Main entry points ────────────────────────────────────────────────────────

def extract_commitment_outcome(
    kind: str,
    payload: dict,
    source: str,
    *,
    budget: Optional[Budget] = None,
    cache_conn: Optional[sqlite3.Connection] = None,
    _model_fn: Optional[Callable[[str], tuple[str, int]]] = None,
) -> ExtractionOutcome:
    """Extract a commitment update and say exactly why when there is none.

    Actor role for call.transcript:
      1. Initial role comes from direction (inbound → customer, else business).
      2. After verification, Agent:/Customer: markers confirm or override it;
         a passage that cannot be attributed is REJECTED.

    Only a fully verified extraction is EXTRACTED.  Model/transport errors
    and an exhausted budget are distinct, retryable statuses; they are never
    reported as NO_COMMITMENT.  Unusable model output is not cached.
    """
    S = ExtractionStatus
    full_text = _signal_text(kind, payload)
    if not full_text.strip():
        return ExtractionOutcome(S.NO_INPUT, reason="signal has no text")

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

    if cache_conn is not None:
        row = cache_conn.execute(
            "SELECT response, tokens_used FROM extraction_cache WHERE prompt_hash=?",
            (prompt_hash,),
        ).fetchone()
        if row:
            raw_response = row[0]
            tokens_used  = int(row[1])
            if budget is not None:
                budget.record_cache_hit()

    # ── Model call ───────────────────────────────────────────────────────────
    if raw_response is None:
        if budget is not None and not budget.within_budget():
            return ExtractionOutcome(S.BUDGET_EXHAUSTED, reason="extraction budget exhausted")

        fn = _model_fn or _default_model_fn
        try:
            raw_response, tokens_used = fn(prompt)
        except Exception as exc:
            return ExtractionOutcome(S.MODEL_ERROR, reason=f"model call failed: {type(exc).__name__}")

        if budget is not None:
            budget.charge(tokens_used)

        if not raw_response:
            return ExtractionOutcome(S.MODEL_ERROR, reason="model returned an empty response")

        outcome = _parse_and_verify_outcome(raw_response, kind, role, full_text, thread_id, False)
        # Cache only usable model output, so a malformed reply is retried.
        if cache_conn is not None and outcome.status != S.MODEL_ERROR:
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
    else:
        outcome = _parse_and_verify_outcome(raw_response, kind, role, full_text, thread_id, True)

    if outcome.status != S.EXTRACTED or outcome.result is None:
        return outcome
    result = outcome.result

    # For call transcripts: use speaker markers to confirm or override actor_role.
    if kind == "call.transcript":
        transcript = str(payload.get("transcript", ""))
        if _has_speaker_markers(transcript):
            attributed = _find_speaker_for_passage(transcript, result.supporting_passage)
            if attributed is None:
                # Markers present but passage not attributable → unresolved.
                return ExtractionOutcome(
                    S.REJECTED, reason="passage cannot be attributed to a speaker")
            if attributed != result.actor_role:
                # Transcript attribution overrides direction-based initial role.
                result = replace(result, actor_role=attributed)

    return ExtractionOutcome(S.EXTRACTED, result)


def extract_commitment_update(
    kind: str,
    payload: dict,
    source: str,
    *,
    budget: Optional[Budget] = None,
    cache_conn: Optional[sqlite3.Connection] = None,
    _model_fn: Optional[Callable[[str], tuple[str, int]]] = None,
) -> Optional[ExtractionResult]:
    """Return the verified ExtractionResult, or None for every other outcome.

    Compatibility wrapper around extract_commitment_outcome().  Callers that
    must tell a failure from "no commitment" (the signal worker) use
    extract_commitment_outcome() instead.

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
    """
    return extract_commitment_outcome(
        kind, payload, source,
        budget=budget, cache_conn=cache_conn, _model_fn=_model_fn,
    ).result
