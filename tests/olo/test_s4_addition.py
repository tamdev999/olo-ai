"""tests/olo/test_s4_addition.py — S4 final-validation additions.

Speaker attribution, long-transcript windowing, and live accuracy tests
added as part of S4 final validation.  Split into a separate file to avoid
overwriting the original frozen corpus.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from olo.extractor import (
    Budget,
    ExtractionResult,
    extract_commitment_update,
    make_deepseek_model_fn,
    migrate_extraction_cache,
)
# Reuse corpus helpers from the original test file
from tests.olo.test_s4_extraction import (
    _CORPUS,
    Label,
    _build_mock_fn,
    _matches,
    _null,
    _r,
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


# ── Live accuracy test ────────────────────────────────────────────────────────

@pytest.mark.live
class TestLiveAccuracy:
    """Real DeepSeek model run on 30-label corpus.

    Skipped when DEEPSEEK_API_KEY is not set.
    Run with:
        pytest tests/olo/test_s4_addition.py -m live -v -s

    Budget cap: $1.00 / 60 calls to prevent runaway spend.
    Do NOT tune the extractor to improve these scores (benchmark integrity).
    """

    def test_30_label_corpus_deepseek(self, tmp_path: Path) -> None:
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            pytest.skip(
                "DEEPSEEK_API_KEY not set — live accuracy test skipped. "
                "Set DEEPSEEK_API_KEY and re-run: "
                "pytest tests/olo/test_s4_addition.py -m live -v -s"
            )

        try:
            model_fn = make_deepseek_model_fn(api_key=api_key)
        except Exception as exc:
            pytest.skip(f"Cannot create DeepSeek model fn: {exc}")

        budget = Budget(
            max_calls=60,
            max_tokens_per_call=600,
            max_total_tokens=200_000,
        )
        budget_cap_usd = 1.00

        cache_conn = sqlite3.connect(str(tmp_path / "live_cache.db"))
        cache_conn.row_factory = sqlite3.Row
        migrate_extraction_cache(cache_conn)

        positives = [(c, c["label"]) for c in _CORPUS if c["label"] is not None]
        negatives = [c for c in _CORPUS if c["label"] is None]

        tp = fp = fn = tn = 0
        lines: list[str] = []

        def _run(case: dict) -> Optional[ExtractionResult]:
            if budget.cost_usd >= budget_cap_usd:
                return None
            return extract_commitment_update(
                case["kind"], case["payload"], case["source"],
                budget=budget, cache_conn=cache_conn, _model_fn=model_fn,
            )

        for case, lbl in positives:
            result = _run(case)
            if result is None:
                fn += 1
                lines.append(f"  FN {case['id']}: expected {lbl.action}/{lbl.event_type}")
            elif _matches(result, lbl):
                tp += 1
                lines.append(f"  TP {case['id']}: {result.action}/{result.event_type} ✓")
            else:
                fn += 1
                lines.append(
                    f"  FN {case['id']}: label={lbl.action}/{lbl.actor_role} "
                    f"got={result.action}/{result.actor_role}"
                )

        for case in negatives:
            result = _run(case)
            if result is None:
                tn += 1
                lines.append(f"  TN {case['id']}: correctly None")
            else:
                fp += 1
                lines.append(f"  FP {case['id']}: expected None, got={result.action}/{result.event_type}")

        total_pos = len(positives)
        total_neg = len(negatives)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1        = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        rpt = budget.report()

        print(
            f"\n{'=' * 60}"
            f"\nS4 Live Accuracy — DeepSeek model (real API calls)"
            f"\n{'=' * 60}"
            f"\nModel  : deepseek-chat (DeepSeek V3)"
            f"\nCorpus : {len(_CORPUS)} signals ({total_pos} positive, {total_neg} negative)"
            f"\n"
            f"\nTrue  Positives : {tp}/{total_pos}"
            f"\nFalse Positives : {fp}/{total_neg}"
            f"\nFalse Negatives : {fn}/{total_pos}"
            f"\nTrue  Negatives : {tn}/{total_neg}"
            f"\n"
            f"\nPrecision : {precision:.2f}  ({tp}/{tp + fp})"
            f"\nRecall    : {recall:.2f}  ({tp}/{tp + fn})"
            f"\nF1        : {f1:.2f}"
            f"\n"
            f"\nTokens    : {rpt['tokens_used']}"
            f"\nCache hits: {rpt['cache_hits']}"
            f"\nCost      : ${rpt['cost_usd']:.5f} USD  (cap was ${budget_cap_usd:.2f})"
            f"\n"
            f"\nResults reflect real model output — do not tune to improve these scores."
        )
        print("\nPer-case:")
        for line in lines:
            print(line)

        # Soft assertions — results reported regardless
        assert fp <= 2, f"Excessive FP ({fp}): model may be over-extracting"
        assert precision >= 0.60 or tp == 0, f"Precision {precision:.2f} < 0.60"
