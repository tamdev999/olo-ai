"""tests/olo/test_s4_extraction.py — S4 commitment-update extractor reliability.

S4 final-validation additions
------------------------------
TestSpeakerAttribution  — Agent:/Customer: marker regression tests
TestLongTranscript      — keyword-window + full-text verification regression
TestLiveAccuracy        — real DeepSeek model run on 30-label corpus
                          (tests/olo/test_s4_addition.py; opt-in, see there)

Labels are versioned: see LABEL_VERSION / corpus_label_hash() below and
docs/olo/m3/S4_LABEL_CHANGELOG.md.


Task 2: 30 hand-labelled signals covering emails, calls, ambiguous requests,
confirmations and cancellations.  Reports precision, recall, false positives,
missed events, cost and cache behaviour.

All 30 cases run offline using an injected mock model function — no network
access.  The mock routes responses by a unique reference code embedded in
each signal's text, ensuring deterministic, isolated test execution.

Customer-cancel integration note
---------------------------------
Cases s06, s09, s19, s20 are customer-initiated cancellations.  The extractor
correctly derives actor_role="customer" from the signal kind (email.received)
rather than from the model.  When these ExtractionResult objects are passed to
apply_event (a future stage), _write_version will produce is_confirmed=0
(a proposal awaiting business confirmation), not is_confirmed=1.  This
verifies that the S3 customer-cancel fix is correctly surfaced through S4.

Reliability targets (mock data, ideal model outputs):
  Precision ≥ 0.90 (of extracted results, fraction that match the label)
  Recall    ≥ 0.80 (of labelled events, fraction correctly extracted)
  FP        ==   0 (never extract on a negative-label signal)
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Optional

import pytest

from olo.ledger import CANCEL_ACTION, CANCEL_NULL_FIELDS

from olo.extractor import (
    Budget,
    ExtractionResult,
    extract_commitment_update,
    make_deepseek_model_fn,
    migrate_extraction_cache,
)

pytestmark = pytest.mark.not_integration


# ── Corpus helpers ────────────────────────────────────────────────────────────

def _r(**kw) -> str:
    """Return a mock model JSON response with optional field overrides."""
    defaults: dict = {
        "event_type": None,
        "action": None,
        "business_key": None,
        "confirmed_date": None,
        "proposed_date": None,
        "prior_confirmed_date": None,
        "supporting_passage": None,
        "confidence": 0.0,
    }
    defaults.update(kw)
    return json.dumps(defaults)


def _null() -> str:
    """Return a mock response indicating no commitment was found."""
    return _r()


# ── Label dataclass ───────────────────────────────────────────────────────────

@dataclass
class Label:
    """Expected extraction outcome for one corpus signal."""
    event_type:           str
    action:               str
    actor_role:           str
    confirmed_date:       Optional[str] = None
    proposed_date:        Optional[str] = None
    prior_confirmed_date: Optional[str] = None
    business_key:         Optional[str] = None


# ── 30-signal corpus ──────────────────────────────────────────────────────────
#
# Each entry:
#   id            — unique case identifier
#   kind          — signal kind
#   payload       — signal payload dict
#   source        — signal source label
#   marker        — unique reference code IN the signal text (for mock routing)
#   mock_response — JSON the model would ideally return
#   label         — Label(…) for a positive case, None for a negative case
#
# S01–S16: clean true-positive cases (TP)
# S17:     TP case where mock returns a supporting_passage not in the text (FN)
# S18–S19: clean TP cases
# S20:     TP case where mock returns confidence=0.5 (FN — below threshold)
# S21–S30: negative cases (TN)

_CORPUS: list[dict] = [

    # ── S01: email.sent — business commits delivery ───────────────────────────
    {
        "id": "s01", "kind": "email.sent",
        "payload": {
            "subject": "Delivery Confirmation REF-S01",
            "body": (
                "Hi Alice, your delivery for REF-S01 has been confirmed "
                "for 2024-03-15. Please ensure someone is available."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S01",
        "mock_response": _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-03-15",
            supporting_passage="delivery for REF-S01 has been confirmed for 2024-03-15",
            confidence=0.95,
        ),
        "label": Label("delivery", "commit", "business", confirmed_date="2024-03-15"),
    },

    # ── S02: email.sent — business commits meeting ────────────────────────────
    {
        "id": "s02", "kind": "email.sent",
        "payload": {
            "subject": "Appointment Confirmation BOOK-S02",
            "body": (
                "Your consultation BOOK-S02 is booked for 2024-04-10 at 2 pm. "
                "We look forward to seeing you."
            ),
        },
        "source": "gmail_biz",
        "marker": "BOOK-S02",
        "mock_response": _r(
            event_type="meeting", action="commit",
            confirmed_date="2024-04-10",
            supporting_passage="consultation BOOK-S02 is booked for 2024-04-10",
            confidence=0.93,
        ),
        "label": Label("meeting", "commit", "business", confirmed_date="2024-04-10"),
    },

    # ── S03: email.received — customer requests delivery reschedule ───────────
    {
        "id": "s03", "kind": "email.received",
        "payload": {
            "subject": "Need to Reschedule REF-S03",
            "body": (
                "Hi, I need to reschedule my delivery REF-S03 "
                "to 2024-03-22 if possible. The original date does not work."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S03",
        "mock_response": _r(
            event_type="delivery", action="request_change",
            proposed_date="2024-03-22",
            supporting_passage="reschedule my delivery REF-S03 to 2024-03-22",
            confidence=0.91,
        ),
        "label": Label("delivery", "request_change", "customer", proposed_date="2024-03-22"),
    },

    # ── S04: email.received — customer requests meeting reschedule ────────────
    {
        "id": "s04", "kind": "email.received",
        "payload": {
            "subject": "Appointment Change Request BOOK-S04",
            "body": (
                "Could we move my appointment BOOK-S04 to 2024-04-18 instead? "
                "I have a conflict on the original date."
            ),
        },
        "source": "gmail_biz",
        "marker": "BOOK-S04",
        "mock_response": _r(
            event_type="meeting", action="request_change",
            proposed_date="2024-04-18",
            supporting_passage="appointment BOOK-S04 to 2024-04-18",
            confidence=0.89,
        ),
        "label": Label("meeting", "request_change", "customer", proposed_date="2024-04-18"),
    },

    # ── S05: email.sent — business confirms customer's change request ─────────
    {
        "id": "s05", "kind": "email.sent",
        "payload": {
            "subject": "New Delivery Date Confirmed REF-S05",
            "body": (
                "We are pleased to confirm your new delivery date of "
                "2024-03-22 for order REF-S05. This supersedes the previous booking."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S05",
        "mock_response": _r(
            event_type="delivery", action="confirm_change",
            confirmed_date="2024-03-22",
            business_key="REF-S05",
            supporting_passage=(
                "confirm your new delivery date of 2024-03-22 for order REF-S05"
            ),
            confidence=0.94,
        ),
        "label": Label(
            "delivery", "confirm_change", "business",
            confirmed_date="2024-03-22", business_key="REF-S05",
        ),
    },

    # ── S06: email.received — customer cancels delivery (KEY TEST) ────────────
    #
    # actor_role MUST be "customer" (email.received), not "business".
    # When passed to apply_event, _write_version produces is_confirmed=0
    # (S3 customer-cancel fix) so this remains a proposal, NOT a confirmed cancel.
    {
        "id": "s06", "kind": "email.received",
        "payload": {
            "subject": "Cancel My Delivery REF-S06",
            "body": (
                "Please cancel my delivery REF-S06 scheduled for 2024-03-15. "
                "I no longer need this service."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S06",
        "mock_response": _r(
            event_type="delivery", action="cancel",
            prior_confirmed_date="2024-03-15",
            supporting_passage="cancel my delivery REF-S06 scheduled for 2024-03-15",
            confidence=0.97,
        ),
        "label": Label("delivery", "cancel", "customer", prior_confirmed_date="2024-03-15"),
    },

    # ── S07: email.sent — business cancels meeting ────────────────────────────
    {
        "id": "s07", "kind": "email.sent",
        "payload": {
            "subject": "Meeting Cancellation BOOK-S07",
            "body": (
                "We regret that we must cancel your meeting BOOK-S07 "
                "on 2024-04-10 due to unforeseen circumstances."
            ),
        },
        "source": "gmail_biz",
        "marker": "BOOK-S07",
        "mock_response": _r(
            event_type="meeting", action="cancel",
            prior_confirmed_date="2024-04-10",
            supporting_passage="cancel your meeting BOOK-S07 on 2024-04-10",
            confidence=0.92,
        ),
        "label": Label("meeting", "cancel", "business", prior_confirmed_date="2024-04-10"),
    },

    # ── S08: email.sent — business reaffirms delivery date ───────────────────
    {
        "id": "s08", "kind": "email.sent",
        "payload": {
            "subject": "Delivery Update REF-S08",
            "body": (
                "Just a quick note: your delivery REF-S08 is still on track "
                "for 2024-03-15 as originally scheduled."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S08",
        "mock_response": _r(
            event_type="delivery", action="reaffirm",
            confirmed_date="2024-03-15",
            supporting_passage="delivery REF-S08 is still on track for 2024-03-15",
            confidence=0.88,
        ),
        "label": Label("delivery", "reaffirm", "business", confirmed_date="2024-03-15"),
    },

    # ── S09: email.received — customer cancels meeting ────────────────────────
    {
        "id": "s09", "kind": "email.received",
        "payload": {
            "subject": "Cancel Appointment BOOK-S09",
            "body": (
                "I would like to cancel my appointment BOOK-S09 "
                "scheduled for 2024-04-10. Please confirm receipt."
            ),
        },
        "source": "gmail_biz",
        "marker": "BOOK-S09",
        "mock_response": _r(
            event_type="meeting", action="cancel",
            prior_confirmed_date="2024-04-10",
            supporting_passage="cancel my appointment BOOK-S09 scheduled for 2024-04-10",
            confidence=0.95,
        ),
        "label": Label("meeting", "cancel", "customer", prior_confirmed_date="2024-04-10"),
    },

    # ── S10: email.sent — delivery commit with order ID ───────────────────────
    {
        "id": "s10", "kind": "email.sent",
        "payload": {
            "subject": "Order Confirmation",
            "body": (
                "Your order ORD-2024-S10 has been confirmed for delivery "
                "on 2024-03-18. Thank you for your business."
            ),
        },
        "source": "gmail_biz",
        "marker": "ORD-2024-S10",
        "mock_response": _r(
            event_type="delivery", action="commit",
            business_key="ORD-2024-S10",
            confirmed_date="2024-03-18",
            supporting_passage=(
                "order ORD-2024-S10 has been confirmed for delivery on 2024-03-18"
            ),
            confidence=0.96,
        ),
        "label": Label(
            "delivery", "commit", "business",
            confirmed_date="2024-03-18", business_key="ORD-2024-S10",
        ),
    },

    # ── S11: call.transcript outbound — business commits delivery ─────────────
    {
        "id": "s11", "kind": "call.transcript",
        "payload": {
            "direction": "outbound",
            "transcript": (
                "Agent: I can confirm that your delivery CALL-S11 "
                "will arrive on 2024-03-25. "
                "Customer: That works perfectly, thank you."
            ),
        },
        "source": "calls_biz",
        "marker": "CALL-S11",
        "mock_response": _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-03-25",
            supporting_passage=(
                "delivery CALL-S11 will arrive on 2024-03-25"
            ),
            confidence=0.90,
        ),
        "label": Label("delivery", "commit", "business", confirmed_date="2024-03-25"),
    },

    # ── S12: call.transcript inbound — customer requests change ───────────────
    {
        "id": "s12", "kind": "call.transcript",
        "payload": {
            "direction": "inbound",
            "transcript": (
                "Customer: Can you change my delivery CALL-S12 to 2024-04-02 please? "
                "Agent: Let me check availability for you."
            ),
        },
        "source": "calls_biz",
        "marker": "CALL-S12",
        "mock_response": _r(
            event_type="delivery", action="request_change",
            proposed_date="2024-04-02",
            supporting_passage="change my delivery CALL-S12 to 2024-04-02",
            confidence=0.87,
        ),
        "label": Label("delivery", "request_change", "customer", proposed_date="2024-04-02"),
    },

    # ── S13: email.sent — business confirms customer cancellation ─────────────
    {
        "id": "s13", "kind": "email.sent",
        "payload": {
            "subject": "Cancellation Processed REF-S13",
            "body": (
                "We have processed the cancellation of your delivery REF-S13 "
                "previously scheduled for 2024-03-20."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S13",
        "mock_response": _r(
            event_type="delivery", action="cancel",
            prior_confirmed_date="2024-03-20",
            supporting_passage=(
                "cancellation of your delivery REF-S13 previously scheduled for 2024-03-20"
            ),
            confidence=0.93,
        ),
        "label": Label("delivery", "cancel", "business", prior_confirmed_date="2024-03-20"),
    },

    # ── S14: email.received — change request with prior date ──────────────────
    {
        "id": "s14", "kind": "email.received",
        "payload": {
            "subject": "Move My Delivery REF-S14",
            "body": (
                "I would like to move my delivery REF-S14 "
                "from 2024-03-15 to 2024-03-28 please."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S14",
        "mock_response": _r(
            event_type="delivery", action="request_change",
            proposed_date="2024-03-28",
            prior_confirmed_date="2024-03-15",
            supporting_passage="delivery REF-S14 from 2024-03-15 to 2024-03-28",
            confidence=0.92,
        ),
        "label": Label(
            "delivery", "request_change", "customer",
            proposed_date="2024-03-28",
            prior_confirmed_date="2024-03-15",
        ),
    },

    # ── S15: email.sent — business reaffirms meeting ──────────────────────────
    {
        "id": "s15", "kind": "email.sent",
        "payload": {
            "subject": "Meeting Reminder BOOK-S15",
            "body": (
                "This is a reminder that your meeting BOOK-S15 "
                "on 2024-04-10 is confirmed and unchanged."
            ),
        },
        "source": "gmail_biz",
        "marker": "BOOK-S15",
        "mock_response": _r(
            event_type="meeting", action="reaffirm",
            confirmed_date="2024-04-10",
            supporting_passage="meeting BOOK-S15 on 2024-04-10 is confirmed and unchanged",
            confidence=0.88,
        ),
        "label": Label("meeting", "reaffirm", "business", confirmed_date="2024-04-10"),
    },

    # ── S16: call.transcript outbound — business confirms change ──────────────
    {
        "id": "s16", "kind": "call.transcript",
        "payload": {
            "direction": "outbound",
            "transcript": (
                "Agent: I have updated your delivery CALL-S16 "
                "booking to 2024-04-05 as you requested. "
                "Customer: Perfect, thank you."
            ),
        },
        "source": "calls_biz",
        "marker": "CALL-S16",
        "mock_response": _r(
            event_type="delivery", action="confirm_change",
            confirmed_date="2024-04-05",
            supporting_passage="delivery CALL-S16 booking to 2024-04-05",
            confidence=0.91,
        ),
        "label": Label("delivery", "confirm_change", "business", confirmed_date="2024-04-05"),
    },

    # ── S17: email.sent — intentional FN: mock returns unverifiable passage ───
    # The signal clearly contains a commitment, so the label is positive.
    # But the mock response includes a supporting_passage that is NOT in the
    # signal text — this tests that the verifier correctly rejects it.
    {
        "id": "s17", "kind": "email.sent",
        "payload": {
            "subject": "Shipment Notice SHIP-S17",
            "body": (
                "Shipment SHIP-S17 is scheduled for delivery on 2024-03-20. "
                "Tracking details will follow."
            ),
        },
        "source": "gmail_biz",
        "marker": "SHIP-S17",
        "mock_response": _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-03-20",
            # This passage does NOT appear in the body — verifier must reject it
            supporting_passage="your shipment is booked for arrival on 2024-03-20",
            confidence=0.90,
        ),
        "label": Label("delivery", "commit", "business", confirmed_date="2024-03-20"),
    },

    # ── S18: email.sent — business confirms meeting change ───────────────────
    {
        "id": "s18", "kind": "email.sent",
        "payload": {
            "subject": "Updated Appointment BOOK-S18",
            "body": (
                "We have updated your appointment BOOK-S18 to 2024-04-18 "
                "as you requested. Same location as before."
            ),
        },
        "source": "gmail_biz",
        "marker": "BOOK-S18",
        "mock_response": _r(
            event_type="meeting", action="confirm_change",
            confirmed_date="2024-04-18",
            supporting_passage="appointment BOOK-S18 to 2024-04-18",
            confidence=0.94,
        ),
        "label": Label("meeting", "confirm_change", "business", confirmed_date="2024-04-18"),
    },

    # ── S19: email.received — delivery cancel with order ID ───────────────────
    {
        "id": "s19", "kind": "email.received",
        "payload": {
            "subject": "Cancel Order ORD-2024-S19",
            "body": (
                "Please cancel order ORD-2024-S19. "
                "The delivery scheduled for 2024-03-18 is no longer needed."
            ),
        },
        "source": "gmail_biz",
        "marker": "ORD-2024-S19",
        "mock_response": _r(
            event_type="delivery", action="cancel",
            business_key="ORD-2024-S19",
            prior_confirmed_date="2024-03-18",
            supporting_passage="cancel order ORD-2024-S19",
            confidence=0.96,
        ),
        "label": Label(
            "delivery", "cancel", "customer",
            prior_confirmed_date="2024-03-18", business_key="ORD-2024-S19",
        ),
    },

    # ── S20: email.received — intentional FN: mock returns low confidence ─────
    # Clear customer cancellation, but mock returns confidence=0.5 (below 0.7).
    # This tests that the confidence threshold correctly filters out uncertain
    # extractions even when the signal itself is unambiguous.
    {
        "id": "s20", "kind": "email.received",
        "payload": {
            "subject": "Cancel Delivery REF-S20",
            "body": (
                "I am cancelling my delivery REF-S20 "
                "for the 2024-04-01 slot. Please confirm."
            ),
        },
        "source": "gmail_biz",
        "marker": "REF-S20",
        "mock_response": _r(
            event_type="delivery", action="cancel",
            prior_confirmed_date="2024-04-01",
            supporting_passage="cancelling my delivery REF-S20 for the 2024-04-01 slot",
            confidence=0.50,  # below threshold — extractor must reject
        ),
        "label": Label("delivery", "cancel", "customer", prior_confirmed_date="2024-04-01"),
    },

    # ── S21–S30: NEGATIVE cases (expect None) ─────────────────────────────────

    # ── S21: email.sent — no date, no commitment ──────────────────────────────
    {
        "id": "s21", "kind": "email.sent",
        "payload": {
            "subject": "Following Up",
            "body": "Hi Alice, just wanted to follow up FOLL-S21 on our last conversation. Looking forward to hearing from you.",
        },
        "source": "gmail_biz",
        "marker": "FOLL-S21",
        "mock_response": _null(),
        "label": None,
    },

    # ── S22: email.received — no commitment, general reply ───────────────────
    {
        "id": "s22", "kind": "email.received",
        "payload": {
            "subject": "Re: Your Email",
            "body": "Thank you for reaching out GENR-S22. Let me know if anything changes on your end.",
        },
        "source": "gmail_biz",
        "marker": "GENR-S22",
        "mock_response": _null(),
        "label": None,
    },

    # ── S23: email.sent — invoice paid notification ───────────────────────────
    {
        "id": "s23", "kind": "email.sent",
        "payload": {
            "subject": "Invoice #INV-S23 Payment Received",
            "body": "We confirm receipt of payment for invoice INV-S23 of $450.00. No further action required.",
        },
        "source": "gmail_biz",
        "marker": "INV-S23",
        "mock_response": _null(),
        "label": None,
    },

    # ── S24: call.transcript — IT support (unrelated) ─────────────────────────
    {
        "id": "s24", "kind": "call.transcript",
        "payload": {
            "direction": "inbound",
            "transcript": (
                "Customer: I forgot my password for the ITSUP-S24 portal. "
                "Agent: I can reset that for you right away."
            ),
        },
        "source": "calls_biz",
        "marker": "ITSUP-S24",
        "mock_response": _null(),
        "label": None,
    },

    # ── S25: email.received — vague, no specific date ────────────────────────
    {
        "id": "s25", "kind": "email.received",
        "payload": {
            "subject": "Date Question VAGUE-S25",
            "body": "Hi, would it be possible to look at a later date VAGUE-S25? Nothing urgent, just wondering.",
        },
        "source": "gmail_biz",
        "marker": "VAGUE-S25",
        "mock_response": _null(),
        "label": None,
    },

    # ── S26: email.sent — shipping tracking update ────────────────────────────
    {
        "id": "s26", "kind": "email.sent",
        "payload": {
            "subject": "Your Package Is On Its Way TRACK-S26",
            "body": (
                "Your package TRACK-S26 has been picked up by the carrier. "
                "Track at: https://example.com/track?id=TRACK-S26"
            ),
        },
        "source": "gmail_biz",
        "marker": "TRACK-S26",
        "mock_response": _null(),
        "label": None,
    },

    # ── S27: email.received — positive feedback ───────────────────────────────
    {
        "id": "s27", "kind": "email.received",
        "payload": {
            "subject": "Great Service FEED-S27",
            "body": "Just wanted to say thank you for the excellent service FEED-S27. Everything was perfect.",
        },
        "source": "gmail_biz",
        "marker": "FEED-S27",
        "mock_response": _null(),
        "label": None,
    },

    # ── S28: call.transcript — HR onboarding (unrelated) ─────────────────────
    {
        "id": "s28", "kind": "call.transcript",
        "payload": {
            "direction": "outbound",
            "transcript": (
                "HR: Welcome to the company HRONB-S28. Let me walk you through the benefits package. "
                "New hire: Great, I am excited to get started."
            ),
        },
        "source": "calls_biz",
        "marker": "HRONB-S28",
        "mock_response": _null(),
        "label": None,
    },

    # ── S29: email.sent — newsletter / promotional ────────────────────────────
    {
        "id": "s29", "kind": "email.sent",
        "payload": {
            "subject": "Special Offer This Month PROMO-S29",
            "body": "Hi valued customer, check out our PROMO-S29 deals this month. Use code PROMO20 for 20% off.",
        },
        "source": "gmail_biz",
        "marker": "PROMO-S29",
        "mock_response": _null(),
        "label": None,
    },

    # ── S30: email.received — ambiguous multi-date, mock confident but ambiguous
    # Even if a mock could extract something, two dates with no clear action
    # mean this should stay unextracted.  Mock correctly returns null here.
    {
        "id": "s30", "kind": "email.received",
        "payload": {
            "subject": "Date Options MULTI-S30",
            "body": (
                "Hi, regarding MULTI-S30 — would either 2024-04-10 or 2024-04-17 work? "
                "Let me know which one fits your schedule."
            ),
        },
        "source": "gmail_biz",
        "marker": "MULTI-S30",
        "mock_response": _null(),
        "label": None,
    },
]


# ── Mock model function ───────────────────────────────────────────────────────

def _build_mock_fn(corpus: list[dict]):
    """Build a deterministic mock model function routed by corpus markers."""
    _null_resp = _null()

    def mock_fn(prompt: str) -> tuple[str, int]:
        for case in corpus:
            if case["marker"] in prompt:
                resp = case["mock_response"]
                return resp, max(len(resp) // 4, 10)
        return _null_resp, 10

    return mock_fn


# ── Match helper ──────────────────────────────────────────────────────────────

def _matches(result: ExtractionResult, lbl: Label) -> bool:
    """True when result agrees with the label on the core fields.

    Cancellations must also carry null confirmed_date and proposed_date
    (cancellation contract, olo.ledger.cancellation_field_error).
    """
    if result.event_type != lbl.event_type:
        return False
    if result.action != lbl.action:
        return False
    if result.actor_role != lbl.actor_role:
        return False
    if lbl.action == CANCEL_ACTION and any(
        getattr(result, f) is not None for f in CANCEL_NULL_FIELDS
    ):
        return False
    # At least one date must match when the label specifies one
    if lbl.confirmed_date and result.confirmed_date != lbl.confirmed_date:
        return False
    if lbl.proposed_date and result.proposed_date != lbl.proposed_date:
        return False
    if lbl.prior_confirmed_date and result.prior_confirmed_date != lbl.prior_confirmed_date:
        return False
    if lbl.business_key and result.business_key != lbl.business_key:
        return False
    return True


# ── Label versioning ──────────────────────────────────────────────────────────
#
# The label hash covers (id, kind, label) for every case — not payloads or
# mock responses.  Any label edit changes it; record a new version below and
# in docs/olo/m3/S4_LABEL_CHANGELOG.md.  Results are only comparable across
# runs that report the same label hash.

def corpus_label_hash(corpus: list[dict] = _CORPUS) -> str:
    """sha256 of the canonical JSON of every case's id, kind and label."""
    rows = [
        {"id": c["id"], "kind": c["kind"],
         "label": asdict(c["label"]) if c["label"] is not None else None}
        for c in corpus
    ]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


LABEL_VERSION: int = 2

#: v1 — labels as first committed (3e648ac62d), scored by the first live run
#: (commit 2882157253, report s4_live_accuracy_2882157253.json).
LABEL_HASH_V1: str = "ee3821e92f838bfe7aa065e0f84e79f3c064a5858ea11dd72432a622c8e7099d"
#: v2 — cancellation contract (S4 C1–C4).
LABEL_HASH_V2: str = "3fa1265d25007e6692f325d864a84ff557624e5497e7275ba0c8967f9670cc49"

LABEL_CHANGELOG: tuple[tuple[int, str], ...] = (
    (1, "Initial 30-label corpus (3e648ac62d)."),
    (2, "Cancellation contract: cancelled date in prior_confirmed_date, "
        "null confirmed_date/proposed_date. s07, s13: confirmed_date -> "
        "prior_confirmed_date. s06, s09, s19, s20: require the prior date "
        "stated in the signal text. Mock responses updated to match."),
)


class TestLabelVersion:
    def test_label_hash_matches_recorded_version(self) -> None:
        assert LABEL_CHANGELOG[-1][0] == LABEL_VERSION
        assert corpus_label_hash() == LABEL_HASH_V2, (
            "corpus labels changed: bump LABEL_VERSION, record the new hash "
            "and update docs/olo/m3/S4_LABEL_CHANGELOG.md"
        )

    def test_cancel_labels_follow_contract(self) -> None:
        cancels = [c for c in _CORPUS if c["label"] and c["label"].action == CANCEL_ACTION]
        assert {c["id"] for c in cancels} == {"s06", "s07", "s09", "s13", "s19", "s20"}
        for c in cancels:
            lbl = c["label"]
            assert lbl.confirmed_date is None and lbl.proposed_date is None, c["id"]
            assert lbl.prior_confirmed_date, c["id"]
            # The prior date is documented in the signal text itself.
            assert lbl.prior_confirmed_date in json.dumps(c["payload"]), c["id"]

    def test_cancel_mocks_follow_contract(self) -> None:
        for c in _CORPUS:
            data = json.loads(c["mock_response"])
            if data.get("action") == CANCEL_ACTION:
                assert data["confirmed_date"] is None, c["id"]
                assert data["proposed_date"] is None, c["id"]

    def test_matches_rejects_cancel_with_confirmed_date(self) -> None:
        lbl = _CORPUS[[c["id"] for c in _CORPUS].index("s07")]["label"]
        good = ExtractionResult(
            event_type="meeting", action="cancel", actor_role="business",
            business_key=None, confirmed_date=None, proposed_date=None,
            prior_confirmed_date="2024-04-10", thread_id=None,
            supporting_passage="x", confidence=0.9,
        )
        assert _matches(good, lbl)
        assert not _matches(replace(good, confirmed_date="2024-04-10"), lbl)
        assert not _matches(replace(good, proposed_date="2024-04-10"), lbl)


# ── Corpus reliability test ───────────────────────────────────────────────────

class TestReliabilityCorpus:
    """Run all 30 labelled signals through the extractor and report metrics."""

    def test_corpus_metrics(self, tmp_path: Path) -> None:
        """30-label precision/recall test with mock model responses."""
        mock_fn = _build_mock_fn(_CORPUS)
        budget  = Budget(max_calls=30, max_tokens_per_call=400, max_total_tokens=12_000)

        # Set up an in-memory SQLite cache for this run
        cache_conn = sqlite3.connect(str(tmp_path / "ext_cache.db"))
        cache_conn.row_factory = sqlite3.Row
        migrate_extraction_cache(cache_conn)

        positives = [(c, c["label"]) for c in _CORPUS if c["label"] is not None]
        negatives = [c for c in _CORPUS if c["label"] is None]

        tp = fp = fn = tn = 0
        failures: list[str] = []

        # Positive cases
        for case, lbl in positives:
            result = extract_commitment_update(
                case["kind"], case["payload"], case["source"],
                budget=budget, cache_conn=cache_conn, _model_fn=mock_fn,
            )
            if result is None:
                fn += 1
                failures.append(f"  FN {case['id']}: expected {lbl.action}/{lbl.event_type}, got None")
            elif _matches(result, lbl):
                tp += 1
            else:
                fn += 1
                failures.append(
                    f"  FN {case['id']}: expected {lbl.action}/{lbl.event_type}/{lbl.actor_role}, "
                    f"got {result.action}/{result.event_type}/{result.actor_role}"
                )

        # Negative cases
        for case in negatives:
            result = extract_commitment_update(
                case["kind"], case["payload"], case["source"],
                budget=budget, cache_conn=cache_conn, _model_fn=mock_fn,
            )
            if result is None:
                tn += 1
            else:
                fp += 1
                failures.append(
                    f"  FP {case['id']}: expected None, "
                    f"got {result.action}/{result.event_type}"
                )

        total_positive = len(positives)
        total_negative = len(negatives)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 1.0
        f1        = (2 * precision * recall / (precision + recall)
                     if (precision + recall) > 0 else 0.0)
        rpt = budget.report()

        print(
            f"\nS4 Extraction Reliability Report"
            f"\n================================="
            f"\nCorpus: {len(_CORPUS)} signals "
            f"({total_positive} positive, {total_negative} negative)"
            f"\nTrue  Positives : {tp}/{total_positive}"
            f"\nFalse Positives : {fp}/{total_negative}"
            f"\nFalse Negatives : {fn}/{total_positive}"
            f"\nTrue  Negatives : {tn}/{total_negative}"
            f"\nPrecision : {precision:.2f}  ({tp}/{tp + fp})"
            f"\nRecall    : {recall:.2f}  ({tp}/{tp + fn})"
            f"\nF1        : {f1:.2f}"
            f"\nBudget    : calls={rpt['calls_used']}, "
            f"tokens={rpt['tokens_used']}, "
            f"cache_hits={rpt['cache_hits']}, "
            f"cost_usd={rpt['cost_usd']}"
        )
        if failures:
            print("Case details:")
            for f_msg in failures:
                print(f_msg)

        # ── Reliability assertions ────────────────────────────────────────────
        assert fp == 0, (
            f"False positives detected: {fp}. "
            "Extractor must never fire on non-commitment signals."
        )
        assert precision >= 0.90, f"Precision {precision:.2f} < 0.90"
        assert recall    >= 0.80, f"Recall {recall:.2f} < 0.80"
        assert total_positive == 20
        assert total_negative == 10

    def test_customer_cancel_actor_role(self) -> None:
        """Customer cancellations must have actor_role='customer'.

        When the result is passed to apply_event, _write_version produces
        is_confirmed=0 (S3 customer-cancel gate), not is_confirmed=1.
        This test verifies the extractor correctly surfaces the actor role
        so the ledger can apply the fix.
        """
        mock_fn = _build_mock_fn(_CORPUS)
        for case_id in ("s06", "s09", "s18", "s19", "s20"):
            case = next(c for c in _CORPUS if c["id"] == case_id)
            lbl  = case["label"]
            if lbl is None or lbl.action != "cancel":
                continue
            result = extract_commitment_update(
                case["kind"], case["payload"], case["source"],
                _model_fn=mock_fn,
            )
            if result is not None:
                assert result.actor_role == "customer", (
                    f"{case_id}: expected actor_role='customer', got {result.actor_role!r}. "
                    "Customer cancellations must NOT carry actor_role='business' "
                    "or they would be treated as confirmed by apply_event."
                )

    def test_business_cancel_actor_role(self) -> None:
        """Business cancellations (email.sent) must have actor_role='business'."""
        mock_fn = _build_mock_fn(_CORPUS)
        for case_id in ("s07", "s13"):
            case   = next(c for c in _CORPUS if c["id"] == case_id)
            result = extract_commitment_update(
                case["kind"], case["payload"], case["source"],
                _model_fn=mock_fn,
            )
            assert result is not None, f"{case_id}: expected extraction, got None"
            assert result.actor_role == "business", (
                f"{case_id}: expected actor_role='business', got {result.actor_role!r}"
            )
            assert result.action == "cancel"


# ── Pipeline unit tests ───────────────────────────────────────────────────────

class TestExtractionPipeline:
    """Unit tests for budget guard, cache, confidence threshold, verification."""

    def test_budget_guard_blocks_at_zero_calls(self) -> None:
        """Budget exhausted before any call → returns None immediately."""
        exhausted = Budget(max_calls=0)
        result = extract_commitment_update(
            "email.sent",
            {"body": "Your delivery is confirmed for 2024-03-15."},
            "gmail_biz",
            budget=exhausted,
            _model_fn=lambda p: ("should not be called", 0),
        )
        assert result is None
        assert exhausted.calls_used == 0

    def test_budget_guard_blocks_at_zero_tokens(self) -> None:
        """Budget exhausted on tokens → returns None immediately."""
        exhausted = Budget(max_calls=100, max_total_tokens=0)
        result = extract_commitment_update(
            "email.sent",
            {"body": "Your delivery is confirmed for 2024-03-15."},
            "gmail_biz",
            budget=exhausted,
            _model_fn=lambda p: ("should not be called", 0),
        )
        assert result is None

    def test_budget_charged_on_model_call(self) -> None:
        """Budget.calls_used and tokens_used are incremented after a model call."""
        budget = Budget()
        good_response = _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-03-15",
            supporting_passage="delivery is confirmed for 2024-03-15",
            confidence=0.90,
        )
        result = extract_commitment_update(
            "email.sent",
            {"body": "Your delivery is confirmed for 2024-03-15."},
            "gmail_biz",
            budget=budget,
            _model_fn=lambda p: (good_response, 42),
        )
        assert result is not None
        assert budget.calls_used == 1
        assert budget.tokens_used == 42
        assert budget.cost_usd > 0

    def test_cache_serves_second_call(self, tmp_path: Path) -> None:
        """Second call with identical payload hits the cache; model not called twice."""
        call_count = [0]

        def counting_fn(prompt: str) -> tuple[str, int]:
            call_count[0] += 1
            return _r(
                event_type="delivery", action="commit",
                confirmed_date="2024-05-01",
                supporting_passage="delivery cached for 2024-05-01",
                confidence=0.90,
            ), 50

        payload = {"body": "Your delivery cached for 2024-05-01 is set."}
        cache_conn = sqlite3.connect(str(tmp_path / "cache_test.db"))
        cache_conn.row_factory = sqlite3.Row
        migrate_extraction_cache(cache_conn)
        budget = Budget()

        r1 = extract_commitment_update(
            "email.sent", payload, "s",
            budget=budget, cache_conn=cache_conn, _model_fn=counting_fn,
        )
        r2 = extract_commitment_update(
            "email.sent", payload, "s",
            budget=budget, cache_conn=cache_conn, _model_fn=counting_fn,
        )

        assert r1 is not None
        assert r2 is not None
        assert r1.confirmed_date == r2.confirmed_date
        assert call_count[0] == 1, "Model should be called exactly once; second hit must be cached"
        assert r2.cache_hit is True
        assert budget.cache_hits == 1

    def test_confidence_threshold_rejects_low_score(self) -> None:
        """confidence < 0.7 → None."""
        result = extract_commitment_update(
            "email.sent",
            {"body": "Delivery might be on 2024-04-01, not sure."},
            "gmail_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="commit",
                    confirmed_date="2024-04-01",
                    supporting_passage="Delivery might be on 2024-04-01",
                    confidence=0.5,
                ),
                20,
            ),
        )
        assert result is None

    def test_confidence_at_threshold_accepted(self) -> None:
        """confidence == 0.7 (at the threshold) → accepted."""
        result = extract_commitment_update(
            "email.sent",
            {"body": "Your delivery is confirmed on 2024-04-15."},
            "gmail_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="commit",
                    confirmed_date="2024-04-15",
                    supporting_passage="Your delivery is confirmed on 2024-04-15",
                    confidence=0.7,
                ),
                20,
            ),
        )
        assert result is not None
        assert result.confidence == pytest.approx(0.7)

    def test_verification_rejects_hallucinated_passage(self) -> None:
        """supporting_passage not in signal text → None (not in evidence)."""
        result = extract_commitment_update(
            "email.sent",
            {"body": "Your order is confirmed for 2024-04-20."},
            "gmail_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="commit",
                    confirmed_date="2024-04-20",
                    # This text does NOT appear in the payload body
                    supporting_passage="shipment is booked for April twentieth",
                    confidence=0.95,
                ),
                20,
            ),
        )
        assert result is None

    def test_verification_accepts_exact_substring(self) -> None:
        """supporting_passage that is a verbatim substring is accepted."""
        body   = "Order #X99 is confirmed for delivery on 2024-04-25."
        result = extract_commitment_update(
            "email.sent",
            {"body": body},
            "gmail_biz",
            _model_fn=lambda p: (
                _r(
                    event_type="delivery", action="commit",
                    confirmed_date="2024-04-25",
                    business_key="X99",
                    supporting_passage="Order #X99 is confirmed for delivery on 2024-04-25",
                    confidence=0.95,
                ),
                20,
            ),
        )
        assert result is not None
        assert result.business_key == "X99"
        assert result.confirmed_date == "2024-04-25"

    def test_null_model_response_returns_none(self) -> None:
        """event_type=null in model response → None."""
        result = extract_commitment_update(
            "email.received",
            {"body": "Let me know if you need anything."},
            "gmail_biz",
            _model_fn=lambda p: (_null(), 10),
        )
        assert result is None

    def test_empty_payload_returns_none(self) -> None:
        """Empty payload produces no signal text → None immediately."""
        result = extract_commitment_update(
            "email.sent", {}, "gmail_biz",
            _model_fn=lambda p: (_ for _ in ()).throw(AssertionError("model must not be called")),
        )
        assert result is None

    def test_actor_role_from_email_kind(self) -> None:
        """email.sent → 'business'; email.received → 'customer'; no model needed."""
        good = _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-05-10",
            supporting_passage="delivery confirmed for 2024-05-10",
            confidence=0.9,
        )
        sent_result = extract_commitment_update(
            "email.sent",
            {"body": "Your delivery confirmed for 2024-05-10."},
            "s",
            _model_fn=lambda p: (good, 10),
        )
        recv_result = extract_commitment_update(
            "email.received",
            {"body": "Your delivery confirmed for 2024-05-10."},
            "s",
            _model_fn=lambda p: (good, 10),
        )
        assert sent_result is not None and sent_result.actor_role == "business"
        assert recv_result is not None and recv_result.actor_role == "customer"

    def test_call_outbound_is_business(self) -> None:
        """call.transcript outbound → actor_role='business'."""
        resp = _r(
            event_type="delivery", action="commit",
            confirmed_date="2024-05-15",
            supporting_passage="delivery will arrive on 2024-05-15",
            confidence=0.9,
        )
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "outbound", "transcript": "Agent: delivery will arrive on 2024-05-15."},
            "calls_biz",
            _model_fn=lambda p: (resp, 10),
        )
        assert result is not None
        assert result.actor_role == "business"

    def test_call_inbound_is_customer(self) -> None:
        """call.transcript inbound → actor_role='customer'."""
        resp = _r(
            event_type="delivery", action="request_change",
            proposed_date="2024-05-20",
            supporting_passage="change my delivery to 2024-05-20",
            confidence=0.9,
        )
        result = extract_commitment_update(
            "call.transcript",
            {"direction": "inbound", "transcript": "Customer: change my delivery to 2024-05-20."},
            "calls_biz",
            _model_fn=lambda p: (resp, 10),
        )
        assert result is not None
        assert result.actor_role == "customer"

    def test_invalid_json_returns_none(self) -> None:
        """Malformed JSON from model → None (never crashes)."""
        result = extract_commitment_update(
            "email.sent",
            {"body": "Delivery confirmed for 2024-05-01."},
            "gmail_biz",
            _model_fn=lambda p: ("this is not json {{", 5),
        )
        assert result is None

    def test_model_exception_returns_none(self) -> None:
        """Model raising an exception → None (fail open)."""
        def raise_fn(prompt: str) -> tuple[str, int]:
            raise ConnectionError("network down")

        result = extract_commitment_update(
            "email.sent",
            {"body": "Delivery confirmed for 2024-05-01."},
            "gmail_biz",
            _model_fn=raise_fn,
        )
        assert result is None

    def test_thread_id_carried_through(self) -> None:
        """thread_id from payload is preserved in the ExtractionResult."""
        resp = _r(
            event_type="meeting", action="commit",
            confirmed_date="2024-06-01",
            supporting_passage="meeting confirmed on 2024-06-01",
            confidence=0.9,
        )
        result = extract_commitment_update(
            "email.sent",
            {
                "body": "Your meeting confirmed on 2024-06-01.",
                "thread_id": "thread-abc123",
            },
            "gmail_biz",
            _model_fn=lambda p: (resp, 10),
        )
        assert result is not None
        assert result.thread_id == "thread-abc123"

    def test_date_format_validated(self) -> None:
        """Dates that do not match YYYY-MM-DD are coerced to None."""
        resp = _r(
            event_type="delivery", action="commit",
            confirmed_date="March 15 2024",   # invalid format
            supporting_passage="delivery scheduled for March 15 2024",
            confidence=0.85,
        )
        result = extract_commitment_update(
            "email.sent",
            {"body": "Your delivery scheduled for March 15 2024."},
            "gmail_biz",
            _model_fn=lambda p: (resp, 10),
        )
        assert result is not None
        assert result.confirmed_date is None   # invalid format stripped

    def test_prior_confirmed_date_carried_through(self) -> None:
        """prior_confirmed_date is extracted and preserved."""
        resp = _r(
            event_type="delivery", action="request_change",
            proposed_date="2024-06-15",
            prior_confirmed_date="2024-06-01",
            supporting_passage="change delivery from 2024-06-01 to 2024-06-15",
            confidence=0.88,
        )
        result = extract_commitment_update(
            "email.received",
            {"body": "Please change delivery from 2024-06-01 to 2024-06-15."},
            "gmail_biz",
            _model_fn=lambda p: (resp, 10),
        )
        assert result is not None
        assert result.prior_confirmed_date == "2024-06-01"
        assert result.proposed_date == "2024-06-15"

