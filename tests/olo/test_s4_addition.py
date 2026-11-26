"""tests/olo/test_s4_addition.py — S4 final-validation additions.

Speaker attribution, long-transcript windowing, and live accuracy tests
added as part of S4 final validation.  Split into a separate file to avoid
overwriting the original frozen corpus.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from olo.extractor import (
    DEFAULT_DEEPSEEK_MODEL,
    _DEEPSEEK_INPUT_PER_M,
    _DEEPSEEK_OUTPUT_PER_M,
    Budget,
    ExtractionResult,
    _signal_text,
    extract_commitment_update,
    make_deepseek_model_fn,
    migrate_extraction_cache,
)
from olo.ledger import ACTIONS, CANCEL_ACTION, CANCEL_NULL_FIELDS
# Reuse corpus helpers from the original test file
from tests.olo.test_s4_extraction import (
    _CORPUS,
    LABEL_VERSION,
    Label,
    _build_mock_fn,
    _matches,
    _null,
    _r,
    corpus_label_hash,
)

pytestmark = pytest.mark.not_integration


# ── Speaker attribution regression tests ─────────────────────────────────────

class TestSpeakerAttribution:
    """Agent:/Customer: marker-based speaker detection regression tests (S4 final)."""

    def _commit_resp(self, passage: str) -> str:
        return _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-05-01",
            supporting_passage=passage, confidence=0.9,
        )

    def test_agent_segment_returns_business(self) -> None:
        """Passage in an Agent: segment → actor_role='business'."""
        transcript = (
            "Agent: I can confirm delivery will arrive on 2024-05-01. "
            "Customer: Great, thanks."
        )
        passage = "delivery will arrive on 2024-05-01"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (self._commit_resp(passage), 10),
        )
        assert result is not None
        assert result.actor_role == "business"

    def test_customer_segment_returns_customer(self) -> None:
        """Passage in a Customer: segment → actor_role='customer'."""
        transcript = (
            "Customer: Please change my delivery to 2024-05-01. "
            "Agent: Let me check that."
        )
        passage = "change my delivery to 2024-05-01"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="request_change",
                    proposed_date="2024-05-01",
                    supporting_passage=passage, confidence=0.9,
                ),
                10,
            ),
        )
        assert result is not None
        assert result.actor_role == "customer"

    def test_marker_overrides_inbound_direction(self) -> None:
        """Agent: marker overrides inbound direction when they disagree."""
        transcript = (
            "Customer: When does it arrive? "
            "Agent: I confirm delivery on 2024-05-01."
        )
        passage = "I confirm delivery on 2024-05-01"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "inbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (self._commit_resp(passage), 10),
        )
        assert result is not None
        # direction says customer; transcript says Agent → should be business
        assert result.actor_role == "business"

    def test_passage_before_any_marker_returns_none(self) -> None:
        """Passage in preamble before first speaker label → unresolved → None."""
        transcript = (
            "Delivery on 2024-05-01 is scheduled. "
            "Agent: Hello. Customer: Hi."
        )
        passage = "Delivery on 2024-05-01 is scheduled"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (self._commit_resp(passage), 10),
        )
        # Markers present but passage not attributable to any speaker
        assert result is None

    def test_no_markers_keeps_direction_role_outbound(self) -> None:
        """Transcript without Agent:/Customer: markers uses direction fallback."""
        transcript = "I can confirm that delivery will arrive on 2024-05-01."
        passage = "delivery will arrive on 2024-05-01"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (self._commit_resp(passage), 10),
        )
        assert result is not None
        assert result.actor_role == "business"

    def test_no_markers_inbound_is_customer(self) -> None:
        """Inbound call without markers → customer (direction fallback)."""
        transcript = "Please move my delivery to 2024-05-01."
        passage = "move my delivery to 2024-05-01"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "inbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="request_change",
                    proposed_date="2024-05-01",
                    supporting_passage=passage, confidence=0.9,
                ),
                10,
            ),
        )
        assert result is not None
        assert result.actor_role == "customer"

    def test_customer_cancel_via_markers_is_customer_pending(self) -> None:
        """Customer: segment cancel → actor_role='customer'.

        When passed to apply_event, _write_version produces is_confirmed=0
        (S3 customer-cancel gate), keeping the cancellation pending until the
        business confirms it.
        """
        transcript = (
            "Customer: I want to cancel my delivery for 2024-05-01. "
            "Agent: Understood, I will process that."
        )
        passage = "cancel my delivery for 2024-05-01"
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="cancel",
                    supporting_passage=passage, confidence=0.93,
                ),
                10,
            ),
        )
        assert result is not None
        assert result.actor_role == "customer"
        assert result.action == "cancel"
        # Caller passes this to apply_event → is_confirmed=0 (pending)


# ── Long transcript regression tests ─────────────────────────────────────────

class TestLongTranscript:
    """Keyword-windowing and full-text verification for long signals."""

    def test_commitment_near_end_extracted(self) -> None:
        """Commitment buried near end of a 10k+ char transcript is extracted."""
        filler = "Agent: Good morning. Customer: Hello. " * 220   # ~8360 chars
        tail   = "Agent: I confirm your delivery will arrive on 2024-07-01. Customer: Perfect."
        transcript = filler + tail
        assert len(transcript) > 8_000

        passage = "I confirm your delivery will arrive on 2024-07-01"
        assert passage in transcript  # pre-condition

        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": transcript},
            "calls_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="commit",
                    confirmed_date="2024-07-01",
                    supporting_passage=passage, confidence=0.9,
                ),
                50,
            ),
        )
        assert result is not None, (
            "Keyword-window should select the tail section; "
            "verification uses full text so the passage is found."
        )
        assert result.confirmed_date == "2024-07-01"

    def test_passage_verified_against_full_text(self) -> None:
        """Passage in full text but potentially outside the prompt window → accepted."""
        filler   = "Unrelated content. " * 300        # ~5700 chars of noise
        relevant = "Your delivery is scheduled for 2024-07-15."
        full_body = filler + relevant
        passage = "Your delivery is scheduled for 2024-07-15"
        assert passage in full_body

        result = extract_commitment_update(
            "email.sent",
            {"body": full_body},
            "gmail_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="commit",
                    confirmed_date="2024-07-15",
                    supporting_passage=passage, confidence=0.9,
                ),
                50,
            ),
        )
        assert result is not None
        assert result.confirmed_date == "2024-07-15"

    def test_window_is_pure_substring(self) -> None:
        """_extract_relevant_window returns a contiguous substring, no markers appended."""
        from olo.extractor import _extract_relevant_window

        full = ("Noise noise noise. " * 500) + "Delivery confirmed for 2024-07-20. " + ("More noise. " * 200)
        windowed = _extract_relevant_window(full, max_chars=4_000)
        assert len(windowed) <= 4_000
        assert windowed in full, (
            "_extract_relevant_window must return a pure substring so any "
            "passage quoted from it can be verified against the full text."
        )

    def test_short_text_unchanged(self) -> None:
        """Text shorter than max_chars is returned as-is."""
        from olo.extractor import _extract_relevant_window
        short = "Delivery confirmed for 2024-07-20."
        assert _extract_relevant_window(short, max_chars=4_000) == short

    def test_window_contains_commitment_keyword(self) -> None:
        """When commitment keyword is in the second half, window includes it."""
        from olo.extractor import _extract_relevant_window
        prefix = "Unrelated content " * 100   # ~1800 chars
        suffix = " delivery confirm scheduled 2024-08-01 " * 20  # ~760 chars with keywords
        text   = prefix + suffix
        window = _extract_relevant_window(text, max_chars=1_000)
        # Window should gravitate toward the keyword-dense suffix
        assert any(kw in window.lower() for kw in ("delivery", "confirm", "scheduled"))


# ── Live accuracy evaluation ─────────────────────────────────────────────────
#
# Acceptance criteria for the real-model run.  Fixed BEFORE any live run;
# never tune the extractor, its prompt or these thresholds to the corpus.
#
#   Run validity   Every one of the 30 cases makes exactly one fresh model
#                  call: no model errors, no unparseable responses, no budget
#                  or cost-cap skips, no cache hits, cost upper bound <= cap.
#                  Any violation fails the run; none is scored as TN or FN.
#   Scoring        TP     extraction made and _matches(label)
#                  WRONG  positive case, extraction made, wrong field(s)
#                  FN     positive case, no extraction
#                  FP     negative case, extraction made
#                  TN     negative case, no extraction
#   Accuracy       recall    = TP / 20 positives            >= 0.75
#                  precision = TP / (TP + WRONG + FP)        >= 0.90
#                              (0.0 when nothing was extracted)
#                  FP on the 10 negatives                    <= 1
#   Authoritative  Every extraction attributed to the business actor must be
#                  a TP: business commit/reaffirm/confirm_change/cancel all
#                  write is_confirmed=1 ledger versions.
#   Customer       Labelled customer cancellations (s06, s09, s19, s20) are
#   cancels        either unextracted or carry actor_role='customer'.
#   Coverage       >= 1 TP for each of the 5 actions and the 3 signal kinds.
#
# The report never contains prompts, raw model responses, supporting
# passages, exception messages or credentials: only counts, extracted field
# values and exception type names.

LIVE_MIN_RECALL:    float = 0.75
LIVE_MIN_PRECISION: float = 0.90
LIVE_MAX_FP:        int   = 1
LIVE_COST_CAP_USD:  float = 1.00
# The recorded S4 live results (docs/olo/m3/s4_live_accuracy_*.json) were
# measured on deepseek-chat, discontinued 2026-07-24.  A rerun measures the
# current extractor model and is a new result, not a reproduction.
LIVE_MODEL:         str   = DEFAULT_DEEPSEEK_MODEL

REQUIRED_ACTIONS: tuple[str, ...] = (
    "commit", "reaffirm", "request_change", "confirm_change", "cancel",
)
REQUIRED_KINDS: tuple[str, ...] = ("email.sent", "email.received", "call.transcript")

_N_POSITIVE: int = sum(1 for c in _CORPUS if c["label"] is not None)

_CUSTOMER_CANCEL_IDS: frozenset[str] = frozenset(
    c["id"] for c in _CORPUS
    if c["label"] is not None
    and c["label"].action == "cancel"
    and c["label"].actor_role == "customer"
)

_LABEL_FIELDS: tuple[str, ...] = (
    "confirmed_date", "proposed_date", "prior_confirmed_date", "business_key",
)


def _cost_upper_bound(tokens: int) -> float:
    """Worst-case USD cost: every token priced at the higher (output) rate.

    The model fn only reports combined tokens, so the extractor's Budget
    prices everything at the input rate.  The cap is enforced on this bound.
    """
    rate = max(_DEEPSEEK_INPUT_PER_M, _DEEPSEEK_OUTPUT_PER_M)
    return tokens * rate / 1_000_000


def _error_label(exc: BaseException) -> str:
    """Exception type name plus HTTP status when present; never the message."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return f"{name}({status})" if isinstance(status, int) else name


class _RecordingModelFn:
    """Wrap a model fn to count calls, tokens, errors and unparseable output.

    Exceptions are recorded and re-raised so the extractor follows its normal
    production path.  Nothing from the prompt or response is retained.
    """

    def __init__(self, inner: Callable[[str], tuple]) -> None:
        self._inner = inner
        # Keep the inner model's identity and output cap visible to the
        # extractor (cache key and per-call budget check).
        self.model_id = getattr(inner, "model_id", None)
        self.max_output_tokens = getattr(inner, "max_output_tokens", 400)
        self.calls = 0
        self.tokens = 0
        self.errors: list[str] = []
        self.unparseable = 0

    def __call__(self, prompt: str) -> tuple:
        """Pass the reply through unchanged: (text, total) or (text, in, out)."""
        self.calls += 1
        try:
            reply = self._inner(prompt)
        except Exception as exc:
            self.errors.append(_error_label(exc))
            raise
        text = reply[0]
        self.tokens += sum(int(t or 0) for t in reply[1:])
        try:
            ok = isinstance(json.loads(text), dict)
        except (TypeError, ValueError):
            ok = False
        if not ok:
            self.unparseable += 1
        return reply


@dataclass
class CaseOutcome:
    case_id:      str
    kind:         str
    outcome:      str                      # TP|WRONG|FN|FP|TN|ERROR|SKIPPED
    label_action: Optional[str] = None     # None for negative cases
    expected:     Optional[str] = None
    got:          Optional[str] = None
    actor_role:   Optional[str] = None     # actor of the extraction, if any
    mismatched:   tuple[str, ...] = ()
    model_calls:  int = 0
    error:        Optional[str] = None


def _describe_label(lbl: Label) -> str:
    parts = [f"{lbl.action}/{lbl.event_type}/{lbl.actor_role}"]
    parts += [f"{f}={getattr(lbl, f)}" for f in _LABEL_FIELDS if getattr(lbl, f)]
    return " ".join(parts)


def _describe_result(res: ExtractionResult) -> str:
    parts = [f"{res.action}/{res.event_type}/{res.actor_role}"]
    parts += [f"{f}={getattr(res, f)}" for f in _LABEL_FIELDS if getattr(res, f)]
    return " ".join(parts)


def _mismatched_fields(res: ExtractionResult, lbl: Label) -> tuple[str, ...]:
    """Fields where res disagrees with lbl (same rules as _matches)."""
    bad = [f for f in ("event_type", "action", "actor_role")
           if getattr(res, f) != getattr(lbl, f)]
    bad += [f for f in _LABEL_FIELDS
            if getattr(lbl, f) and getattr(res, f) != getattr(lbl, f)]
    if lbl.action == CANCEL_ACTION:
        bad += [f for f in CANCEL_NULL_FIELDS
                if getattr(res, f) is not None and f not in bad]
    return tuple(bad)


def _score_case(
    case: dict,
    result: Optional[ExtractionResult],
    *,
    model_calls: int,
    errors: list[str],
    unparseable: int,
    skipped: Optional[str],
) -> CaseOutcome:
    lbl: Optional[Label] = case["label"]
    out = CaseOutcome(
        case_id=case["id"],
        kind=case["kind"],
        outcome="",
        label_action=lbl.action if lbl else None,
        expected=_describe_label(lbl) if lbl else None,
        model_calls=model_calls,
    )
    if skipped:
        out.outcome, out.error = "SKIPPED", skipped
        return out
    if errors:
        out.outcome, out.error = "ERROR", ",".join(errors)
        return out
    if unparseable:
        out.outcome, out.error = "ERROR", "unparseable_response"
        return out
    if model_calls != 1:
        out.outcome, out.error = "ERROR", f"model_calls={model_calls} (expected 1)"
        return out

    if result is not None:
        out.got = _describe_result(result)
        out.actor_role = result.actor_role

    if lbl is not None:
        if result is None:
            out.outcome = "FN"
        elif _matches(result, lbl):
            out.outcome = "TP"
        else:
            out.outcome = "WRONG"
            out.mismatched = _mismatched_fields(result, lbl)
    else:
        out.outcome = "TN" if result is None else "FP"
    return out


@dataclass
class LiveReport:
    outcomes:             list[CaseOutcome]
    model_calls:          int
    tokens:               int
    cache_hits:           int
    cost_estimate_usd:    float
    cost_upper_bound_usd: float
    cost_cap_usd:         float
    corpus_size:          int = len(_CORPUS)
    n_positive:           int = _N_POSITIVE

    def count(self, outcome: str) -> int:
        return sum(1 for o in self.outcomes if o.outcome == outcome)

    @property
    def precision(self) -> float:
        made = self.count("TP") + self.count("WRONG") + self.count("FP")
        return self.count("TP") / made if made else 0.0

    @property
    def recall(self) -> float:
        return self.count("TP") / self.n_positive if self.n_positive else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def to_dict(self) -> dict:
        return {
            "model": LIVE_MODEL,
            "label_version": LABEL_VERSION,
            "label_hash": corpus_label_hash(),
            "corpus_size": self.corpus_size,
            "cases_evaluated": len(self.outcomes),
            "model_calls": self.model_calls,
            "skipped": self.count("SKIPPED"),
            "errors": self.count("ERROR"),
            "tokens": self.tokens,
            "cache_hits": self.cache_hits,
            "cost_estimate_usd": round(self.cost_estimate_usd, 6),
            "cost_upper_bound_usd": round(self.cost_upper_bound_usd, 6),
            "cost_cap_usd": self.cost_cap_usd,
            "counts": {k: self.count(k) for k in
                       ("TP", "WRONG", "FN", "FP", "TN", "ERROR", "SKIPPED")},
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "thresholds": {
                "min_recall": LIVE_MIN_RECALL,
                "min_precision": LIVE_MIN_PRECISION,
                "max_fp": LIVE_MAX_FP,
            },
            "acceptance_failures": _acceptance_failures(self),
            "cases": [
                {
                    "id": o.case_id, "kind": o.kind, "outcome": o.outcome,
                    "expected": o.expected, "got": o.got,
                    "mismatched": list(o.mismatched),
                    "model_calls": o.model_calls, "error": o.error,
                }
                for o in self.outcomes
            ],
        }


def _evaluate_corpus(
    model_fn: Callable[[str], tuple[str, int]],
    *,
    budget: Budget,
    cache_conn: Optional[sqlite3.Connection],
    cost_cap_usd: float = LIVE_COST_CAP_USD,
    corpus: list[dict] = _CORPUS,
) -> LiveReport:
    """Run every corpus case through extract_commitment_update and score it."""
    rec = _RecordingModelFn(model_fn)
    outcomes: list[CaseOutcome] = []
    for case in corpus:
        skipped: Optional[str] = None
        if not budget.within_budget():
            skipped = "budget_exhausted"
        elif _cost_upper_bound(rec.tokens) >= cost_cap_usd:
            skipped = "cost_cap_reached"

        calls0, err0, unp0 = rec.calls, len(rec.errors), rec.unparseable
        result: Optional[ExtractionResult] = None
        if skipped is None:
            result = extract_commitment_update(
                case["kind"], case["payload"], case["source"],
                budget=budget, cache_conn=cache_conn, _model_fn=rec,
            )
        outcomes.append(_score_case(
            case, result,
            model_calls=rec.calls - calls0,
            errors=rec.errors[err0:],
            unparseable=rec.unparseable - unp0,
            skipped=skipped,
        ))
    return LiveReport(
        outcomes=outcomes,
        model_calls=rec.calls,
        tokens=rec.tokens,
        cache_hits=budget.cache_hits,
        cost_estimate_usd=budget.cost_usd,
        cost_upper_bound_usd=_cost_upper_bound(rec.tokens),
        cost_cap_usd=cost_cap_usd,
    )


def _acceptance_failures(report: LiveReport) -> list[str]:
    """Every acceptance rule the report violates (empty list = accepted)."""
    fails: list[str] = []
    outs = report.outcomes

    # Run validity
    if len(outs) != report.corpus_size:
        fails.append(f"evaluated {len(outs)}/{report.corpus_size} cases")
    errored = [o for o in outs if o.outcome == "ERROR"]
    if errored:
        fails.append(f"{len(errored)} case(s) had model errors: "
                     + ", ".join(f"{o.case_id}={o.error}" for o in errored))
    skipped = [o for o in outs if o.outcome == "SKIPPED"]
    if skipped:
        fails.append(f"{len(skipped)} case(s) skipped: "
                     + ", ".join(f"{o.case_id}={o.error}" for o in skipped))
    if report.cache_hits:
        fails.append(f"{report.cache_hits} cache hit(s): results must come from fresh model calls")
    if report.model_calls != report.corpus_size:
        fails.append(f"model_calls={report.model_calls}, expected {report.corpus_size}")
    if report.cost_upper_bound_usd > report.cost_cap_usd:
        fails.append(f"cost upper bound ${report.cost_upper_bound_usd:.4f} "
                     f"exceeds cap ${report.cost_cap_usd:.2f}")

    # Accuracy
    if report.recall < LIVE_MIN_RECALL:
        fails.append(f"recall {report.recall:.2f} < {LIVE_MIN_RECALL:.2f} "
                     f"(TP {report.count('TP')}/{report.n_positive})")
    if report.precision < LIVE_MIN_PRECISION:
        fails.append(f"strict precision {report.precision:.2f} < {LIVE_MIN_PRECISION:.2f} "
                     f"(TP {report.count('TP')}, WRONG {report.count('WRONG')}, "
                     f"FP {report.count('FP')})")
    if report.count("FP") > LIVE_MAX_FP:
        fails.append(f"false positives {report.count('FP')} > {LIVE_MAX_FP}")

    # Authoritative business updates
    bad_auth = [o for o in outs if o.actor_role == "business" and o.outcome != "TP"]
    if bad_auth:
        fails.append("incorrect authoritative business update(s): "
                     + ", ".join(f"{o.case_id}({o.outcome}: {o.got})" for o in bad_auth))

    # Customer cancellations never become business-actor results
    bad_cancel = [o for o in outs
                  if o.case_id in _CUSTOMER_CANCEL_IDS
                  and o.actor_role is not None and o.actor_role != "customer"]
    if bad_cancel:
        fails.append("customer cancellation attributed to business: "
                     + ", ".join(o.case_id for o in bad_cancel))

    # Coverage
    tps = [o for o in outs if o.outcome == "TP"]
    missing_actions = [a for a in REQUIRED_ACTIONS
                       if not any(o.label_action == a for o in tps)]
    if missing_actions:
        fails.append(f"no TP for action(s): {', '.join(missing_actions)}")
    missing_kinds = [k for k in REQUIRED_KINDS if not any(o.kind == k for o in tps)]
    if missing_kinds:
        fails.append(f"no TP for signal kind(s): {', '.join(missing_kinds)}")

    return fails


def _format_report(report: LiveReport) -> str:
    d = report.to_dict()
    c = d["counts"]
    lines = [
        "=" * 64,
        f"S4 live accuracy — {LIVE_MODEL} (temperature 0)",
        "=" * 64,
        f"Labels     : v{d['label_version']} ({d['label_hash'][:12]})",
        f"Cases      : {d['cases_evaluated']}/{d['corpus_size']} "
        f"({report.n_positive} positive, {d['corpus_size'] - report.n_positive} negative)",
        f"Model calls: {d['model_calls']}   skipped: {d['skipped']}   "
        f"errors: {d['errors']}   cache hits: {d['cache_hits']}",
        f"Tokens     : {d['tokens']}",
        f"Cost       : ${d['cost_estimate_usd']:.5f} estimate, "
        f"${d['cost_upper_bound_usd']:.5f} upper bound (cap ${d['cost_cap_usd']:.2f})",
        f"Outcomes   : TP {c['TP']}  WRONG {c['WRONG']}  FN {c['FN']}  "
        f"FP {c['FP']}  TN {c['TN']}  ERROR {c['ERROR']}  SKIPPED {c['SKIPPED']}",
        f"Precision  : {d['precision']:.2f} (strict; >= {LIVE_MIN_PRECISION:.2f})",
        f"Recall     : {d['recall']:.2f} (>= {LIVE_MIN_RECALL:.2f})",
        f"F1         : {d['f1']:.2f}",
        "",
        "Per case:",
    ]
    for case in d["cases"]:
        line = f"  {case['outcome']:<7} {case['id']} {case['kind']:<16}"
        if case["expected"]:
            line += f" expected[{case['expected']}]"
        if case["got"]:
            line += f" got[{case['got']}]"
        if case["mismatched"]:
            line += f" mismatched={','.join(case['mismatched'])}"
        if case["error"]:
            line += f" error={case['error']}"
        lines.append(line)
    failures = d["acceptance_failures"]
    lines += ["", "Acceptance: " + ("PASS" if not failures else "FAIL")]
    lines += [f"  - {f}" for f in failures]
    return "\n".join(lines)


# ── Live credential loading ──────────────────────────────────────────────────
#
# tests/conftest.py::_hermetic_environment deletes every *_API_KEY variable
# before each test, so the live test never reads its key from os.environ.
# OLO_S4_LIVE_KEY_FILE names an owner-only env-format file (for example the
# existing /root/olo_m3_secrets/deepseek.env).  The key is read inside the
# test and passed straight to the client: it is never put in os.environ,
# printed, or written to the report, and error messages never quote the file.

LIVE_KEY_FILE_ENV: str = "OLO_S4_LIVE_KEY_FILE"
#: Variable names accepted in the key file, in order of preference.
LIVE_KEY_NAMES: tuple[str, ...] = ("DEEPSEEK_API_KEY", "HINDSIGHT_API_LLM_API_KEY")


class LiveKeyError(Exception):
    """The live key file is missing or unusable (message never holds the key)."""


def _load_live_key(path_str: Optional[str]) -> str:
    """Read the DeepSeek key from an owner-only env-format file."""
    if not path_str:
        raise LiveKeyError(f"{LIVE_KEY_FILE_ENV} is not set")
    path = Path(path_str)
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise LiveKeyError(f"key file not found: {path}") from None
    if stat.S_ISLNK(st.st_mode):
        raise LiveKeyError(f"key file must not be a symlink: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise LiveKeyError(f"key file is not a regular file: {path}")
    if sys.platform != "win32" and st.st_mode & 0o077:
        raise LiveKeyError(
            f"key file must be owner-only (mode {stat.S_IMODE(st.st_mode):o}, "
            f"expected 600): {path}"
        )
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    for name in LIVE_KEY_NAMES:
        if values.get(name):
            return values[name]
    raise LiveKeyError(f"key file defines none of {', '.join(LIVE_KEY_NAMES)}: {path}")


class TestLiveKeyFile:
    """OLO_S4_LIVE_KEY_FILE loading, exercised offline with fake keys."""

    _FAKE = "sk-fake-0123456789abcdef"

    def _write(self, tmp_path: Path, text: str, mode: int = 0o600) -> Path:
        p = tmp_path / "deepseek.env"
        p.write_text(text, encoding="utf-8")
        os.chmod(p, mode)
        return p

    def test_unset_is_an_error(self) -> None:
        with pytest.raises(LiveKeyError, match="OLO_S4_LIVE_KEY_FILE is not set"):
            _load_live_key(None)

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(LiveKeyError, match="not found"):
            _load_live_key(str(tmp_path / "absent.env"))

    def test_reads_existing_hindsight_variable(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, f"# comment\nHINDSIGHT_API_LLM_API_KEY={self._FAKE}\n")
        assert _load_live_key(str(p)) == self._FAKE

    def test_prefers_deepseek_variable_and_strips_quotes(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, f"HINDSIGHT_API_LLM_API_KEY=other\n"
                                  f"export DEEPSEEK_API_KEY=\"{self._FAKE}\"\n")
        assert _load_live_key(str(p)) == self._FAKE

    def test_no_known_variable_is_an_error_without_contents(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, f"OTHER_SETTING={self._FAKE}\n")
        with pytest.raises(LiveKeyError) as exc:
            _load_live_key(str(p))
        assert self._FAKE not in str(exc.value)

    def test_key_never_enters_environment(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, f"HINDSIGHT_API_LLM_API_KEY={self._FAKE}\n")
        _load_live_key(str(p))
        assert self._FAKE not in os.environ.values()

    @pytest.mark.platforms("posix")
    def test_group_readable_file_is_rejected(self, tmp_path: Path) -> None:
        p = self._write(tmp_path, f"DEEPSEEK_API_KEY={self._FAKE}\n", mode=0o640)
        with pytest.raises(LiveKeyError, match="owner-only") as exc:
            _load_live_key(str(p))
        assert self._FAKE not in str(exc.value)

    @pytest.mark.platforms("posix")
    def test_symlink_is_rejected(self, tmp_path: Path) -> None:
        target = self._write(tmp_path, f"DEEPSEEK_API_KEY={self._FAKE}\n")
        link = tmp_path / "link.env"
        link.symlink_to(target)
        with pytest.raises(LiveKeyError, match="symlink"):
            _load_live_key(str(link))


# ── Offline proof that the live acceptance cannot pass falsely ───────────────

def _marker_patch_fn(patches: dict[str, Optional[str]]):
    """Ideal mock, except cases whose marker is in ``patches`` get that response.

    A value of None makes the model raise for that case.
    """
    ideal = _build_mock_fn(_CORPUS)
    by_marker = {c["marker"]: patches[c["id"]] for c in _CORPUS if c["id"] in patches}

    def fn(prompt: str) -> tuple[str, int]:
        for marker, resp in by_marker.items():
            if marker in prompt:
                if resp is None:
                    raise RuntimeError("simulated failure")
                return resp, max(len(resp) // 4, 10)
        return ideal(prompt)

    return fn


def _case(case_id: str) -> dict:
    return next(c for c in _CORPUS if c["id"] == case_id)


def _with(case_id: str, **overrides) -> str:
    """The ideal mock response for case_id with field overrides."""
    data = json.loads(_case(case_id)["mock_response"])
    data.update(overrides)
    return json.dumps(data)


def _fp_response(case_id: str, action: str = "request_change") -> str:
    """A verifiable extraction on a negative case (passage is real text).

    Dateless, so it passes the extractor's date-grounding checks (the
    negative texts state no dates) and reaches the scorer as an FP.
    """
    c = _case(case_id)
    passage = _signal_text(c["kind"], c["payload"]).strip()[:40].strip()
    return _r(event_type="delivery", action=action,
              supporting_passage=passage, confidence=0.9)


def _run_offline(model_fn, *, budget: Optional[Budget] = None,
                 cost_cap_usd: float = LIVE_COST_CAP_USD) -> LiveReport:
    conn = sqlite3.connect(":memory:")
    migrate_extraction_cache(conn)
    return _evaluate_corpus(
        model_fn,
        budget=budget or Budget(max_calls=60, max_tokens_per_call=2_000,
                                max_total_tokens=200_000),
        cache_conn=conn,
        cost_cap_usd=cost_cap_usd,
    )


class TestLiveAcceptanceEvaluator:
    """The live acceptance rules, exercised offline with mock models."""

    def test_corpus_shape_matches_criteria(self) -> None:
        assert len(_CORPUS) == 30
        assert _N_POSITIVE == 20
        assert set(REQUIRED_ACTIONS) == set(ACTIONS)
        assert {c["kind"] for c in _CORPUS} == set(REQUIRED_KINDS)
        assert _CUSTOMER_CANCEL_IDS == {"s06", "s09", "s19", "s20"}

    def test_ideal_mock_is_accepted(self) -> None:
        rep = _run_offline(_build_mock_fn(_CORPUS))
        assert _acceptance_failures(rep) == []
        assert (rep.count("TP"), rep.count("FN"), rep.count("TN")) == (18, 2, 10)
        assert rep.count("WRONG") == rep.count("FP") == 0
        assert rep.model_calls == 30 and rep.cache_hits == 0
        assert rep.recall == pytest.approx(0.90)
        assert rep.precision == 1.0

    def test_zero_extractions_fail(self) -> None:
        rep = _run_offline(lambda p: (_null(), 10))
        fails = _acceptance_failures(rep)
        assert rep.count("TP") == 0
        assert rep.precision == 0.0, "no extractions must not yield precision 1.0"
        assert any(f.startswith("recall") for f in fails)
        assert any(f.startswith("strict precision") for f in fails)
        assert any(f.startswith("no TP for action") for f in fails)

    def test_model_failures_fail_and_are_not_scored(self) -> None:
        class AuthenticationError(Exception):
            status_code = 401

        def failing(prompt: str) -> tuple[str, int]:
            raise AuthenticationError("Incorrect API key provided: sk-SECRET-123")

        rep = _run_offline(failing)
        assert rep.count("ERROR") == 30
        assert rep.count("TN") == rep.count("FN") == 0
        fails = _acceptance_failures(rep)
        assert any("model errors" in f and "AuthenticationError(401)" in f for f in fails)
        assert "sk-SECRET-123" not in _format_report(rep)
        assert "sk-SECRET-123" not in json.dumps(rep.to_dict())

    def test_single_model_failure_fails_run(self) -> None:
        rep = _run_offline(_marker_patch_fn({"s25": None}))
        assert rep.count("ERROR") == 1 and rep.count("TN") == 9
        assert any("s25=RuntimeError" in f for f in _acceptance_failures(rep))

    def test_unparseable_response_is_an_error(self) -> None:
        rep = _run_offline(_marker_patch_fn({"s21": "not json at all"}))
        out = next(o for o in rep.outcomes if o.case_id == "s21")
        assert out.outcome == "ERROR" and out.error == "unparseable_response"
        assert _acceptance_failures(rep)

    def test_budget_exhaustion_fails(self) -> None:
        rep = _run_offline(_build_mock_fn(_CORPUS),
                           budget=Budget(max_calls=5, max_total_tokens=200_000))
        assert rep.model_calls == 5
        assert rep.count("SKIPPED") == 25
        assert rep.count("TN") == 0, "skipped negatives must not count as TN"
        fails = _acceptance_failures(rep)
        assert any("budget_exhausted" in f for f in fails)
        assert any(f.startswith("model_calls=5") for f in fails)

    def test_cost_cap_fails(self) -> None:
        rep = _run_offline(_build_mock_fn(_CORPUS), cost_cap_usd=1e-9)
        assert rep.model_calls == 1
        assert any("cost_cap_reached" in f for f in _acceptance_failures(rep))

    def test_cache_hits_fail(self) -> None:
        conn = sqlite3.connect(":memory:")
        migrate_extraction_cache(conn)
        mock = _build_mock_fn(_CORPUS)
        _evaluate_corpus(mock, budget=Budget(max_calls=60), cache_conn=conn)
        rep = _evaluate_corpus(mock, budget=Budget(max_calls=60), cache_conn=conn)
        assert rep.cache_hits == 30 and rep.model_calls == 0
        fails = _acceptance_failures(rep)
        assert any("cache hit" in f for f in fails)

    def test_wrong_customer_fields_count_against_precision(self) -> None:
        one = _run_offline(_marker_patch_fn({"s03": _with("s03", event_type="meeting")}))
        out = next(o for o in one.outcomes if o.case_id == "s03")
        assert out.outcome == "WRONG" and out.mismatched == ("event_type",)
        assert one.precision == pytest.approx(17 / 18)
        assert _acceptance_failures(one) == []

        two = _run_offline(_marker_patch_fn({
            "s03": _with("s03", event_type="meeting"),
            "s04": _with("s04", event_type="delivery"),
        }))
        assert two.count("WRONG") == 2
        assert two.precision == pytest.approx(16 / 18)
        assert any(f.startswith("strict precision") for f in _acceptance_failures(two))

    def test_wrong_business_date_is_rejected(self) -> None:
        """Hardening: a date the source does not state never leaves the extractor.

        Previously this reached the scorer as WRONG and was caught by the
        authority rule; it is now rejected at extraction (date grounding), so
        no business-actor result exists for it at all.
        """
        rep = _run_offline(_marker_patch_fn({"s01": _with("s01", confirmed_date="2024-03-16")}))
        out = next(o for o in rep.outcomes if o.case_id == "s01")
        assert out.outcome == "FN" and out.actor_role is None
        assert not any(f.startswith("incorrect authoritative business update")
                       for f in _acceptance_failures(rep))

    def test_wrong_business_field_in_source_is_caught_by_authority_rule(self) -> None:
        """A business result that passes extraction but is wrong still fails."""
        rep = _run_offline(_marker_patch_fn({"s01": _with("s01", event_type="meeting")}))
        out = next(o for o in rep.outcomes if o.case_id == "s01")
        assert out.outcome == "WRONG" and out.mismatched == ("event_type",)
        assert rep.precision >= LIVE_MIN_PRECISION  # caught by the authority rule
        assert any(f.startswith("incorrect authoritative business update")
                   and "s01" in f for f in _acceptance_failures(rep))

    def test_wrong_business_action_is_rejected(self) -> None:
        rep = _run_offline(_marker_patch_fn({"s08": _with("s08", action="commit")}))
        assert any(f.startswith("incorrect authoritative business update")
                   and "s08" in f for f in _acceptance_failures(rep))

    def test_business_false_positive_is_rejected(self) -> None:
        rep = _run_offline(_marker_patch_fn({"s21": _fp_response("s21", action="cancel")}))
        assert rep.count("FP") == 1
        assert any(f.startswith("incorrect authoritative business update")
                   and "s21" in f for f in _acceptance_failures(rep))

    def test_false_positive_limit(self) -> None:
        one = _run_offline(_marker_patch_fn({"s22": _fp_response("s22")}))
        assert one.count("FP") == 1
        assert _acceptance_failures(one) == []

        two = _run_offline(_marker_patch_fn({"s22": _fp_response("s22"),
                                             "s25": _fp_response("s25")}))
        assert two.count("FP") == 2
        assert any(f.startswith("false positives 2") for f in _acceptance_failures(two))

    def test_customer_cancel_as_business_is_rejected(self) -> None:
        rep = _run_offline(_build_mock_fn(_CORPUS))
        s06 = next(o for o in rep.outcomes if o.case_id == "s06")
        s06.outcome, s06.actor_role = "WRONG", "business"
        s06.got, s06.mismatched = "cancel/delivery/business", ("actor_role",)
        fails = _acceptance_failures(rep)
        assert any(f.startswith("customer cancellation attributed to business")
                   and "s06" in f for f in fails)
        assert any(f.startswith("incorrect authoritative business update") for f in fails)

    def test_action_coverage_required(self) -> None:
        rep = _run_offline(_marker_patch_fn({"s08": _null(), "s15": _null()}))
        assert rep.recall >= LIVE_MIN_RECALL and rep.precision == 1.0
        assert _acceptance_failures(rep) == ["no TP for action(s): reaffirm"]

    def test_kind_coverage_required(self) -> None:
        rep = _run_offline(_marker_patch_fn({"s11": _null(), "s12": _null(), "s16": _null()}))
        assert rep.recall == pytest.approx(LIVE_MIN_RECALL)
        assert _acceptance_failures(rep) == ["no TP for signal kind(s): call.transcript"]

    def test_report_has_counts_and_no_raw_text(self) -> None:
        rep = _run_offline(_build_mock_fn(_CORPUS))
        text = _format_report(rep)
        for needle in ("Model calls: 30", "skipped: 0", "errors: 0", "Tokens", "Cost",
                       "Acceptance: PASS"):
            assert needle in text
        dumped = json.dumps(rep.to_dict())
        for c in _CORPUS:
            passage = json.loads(c["mock_response"]).get("supporting_passage")
            if passage:
                assert passage not in text and passage not in dumped

    def test_report_records_label_version(self) -> None:
        d = _run_offline(_build_mock_fn(_CORPUS)).to_dict()
        assert d["label_version"] == LABEL_VERSION
        assert d["label_hash"] == corpus_label_hash()

    def test_v1_style_cancellation_is_rejected_not_scored(self) -> None:
        """First-run shape (cancelled date in confirmed_date) → no extraction."""
        v1_style = _with("s07", confirmed_date="2024-04-10", prior_confirmed_date=None)
        rep = _run_offline(_marker_patch_fn({"s07": v1_style}))
        s07 = next(o for o in rep.outcomes if o.case_id == "s07")
        assert s07.outcome == "FN" and s07.actor_role is None

    def test_cancel_with_confirmed_date_counts_as_wrong(self) -> None:
        """Scoring alone (bypassing the extractor) also flags the null fields."""
        lbl = _case("s13")["label"]
        res = ExtractionResult(
            event_type="delivery", action="cancel", actor_role="business",
            business_key=None, confirmed_date="2024-03-20", proposed_date=None,
            prior_confirmed_date="2024-03-20", thread_id=None,
            supporting_passage="x", confidence=0.9,
        )
        out = _score_case(_case("s13"), res, model_calls=1, errors=[],
                          unparseable=0, skipped=None)
        assert lbl.prior_confirmed_date == "2024-03-20"
        assert out.outcome == "WRONG" and out.mismatched == ("confirmed_date",)


# ── Live accuracy test (spends API credits) ──────────────────────────────────

@pytest.mark.live
class TestLiveAccuracy:
    """Real DeepSeek run on the 30-label corpus.

    Deselected by default (pyproject addopts: not live) and additionally
    gated on OLO_S4_LIVE=1, so an unrequested run skips rather than spends.
    The key comes only from OLO_S4_LIVE_KEY_FILE (see _load_live_key); the
    Hermes credential scrub stays in force.  Once requested, a missing or
    unusable key FAILS — it never skips or passes.

    OLO_S4_LIVE_REPORT=<path> writes the JSON report; an existing file is
    never overwritten (earlier runs are preserved as evidence).

    Budget: 60 calls / 200k tokens; cost cap $1.00 on the upper-bound price.
    Do NOT tune the extractor to improve these scores.
    """

    def test_30_label_corpus_deepseek(self, tmp_path: Path) -> None:
        if os.environ.get("OLO_S4_LIVE") != "1":
            pytest.skip("live DeepSeek run not requested; set OLO_S4_LIVE=1 "
                        "(spends API credits)")

        out_path = os.environ.get("OLO_S4_LIVE_REPORT")
        if out_path and Path(out_path).exists():
            pytest.fail(f"refusing to overwrite existing report: {out_path}",
                        pytrace=False)

        try:
            api_key = _load_live_key(os.environ.get(LIVE_KEY_FILE_ENV))
        except LiveKeyError as exc:
            pytest.fail(f"OLO_S4_LIVE=1 but no usable key: {exc}", pytrace=False)
        try:
            model_fn = make_deepseek_model_fn(model=LIVE_MODEL, api_key=api_key,
                                              temperature=0)
        except Exception as exc:
            pytest.fail(f"cannot create DeepSeek client: {_error_label(exc)}",
                        pytrace=False)

        cache_conn = sqlite3.connect(str(tmp_path / "live_cache.db"))
        migrate_extraction_cache(cache_conn)
        report = _evaluate_corpus(
            model_fn,
            budget=Budget(max_calls=60, max_tokens_per_call=2_000, max_total_tokens=200_000),
            cache_conn=cache_conn,
        )

        print("\n" + _format_report(report))
        if out_path:
            with open(out_path, "x", encoding="utf-8") as fh:
                fh.write(json.dumps(report.to_dict(), indent=2) + "\n")

        failures = _acceptance_failures(report)
        assert not failures, "S4 live acceptance failed:\n  - " + "\n  - ".join(failures)
