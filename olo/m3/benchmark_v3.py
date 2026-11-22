"""Olo M3 — Retrieval Benchmark v3 (Delta Finance holdout, ~50 questions).

This benchmark targets the v3 holdout corpus (Diana Patel / Delta Finance).
It is FROZEN BEFORE ANY RETRIEVAL RUN on the v3 corpus.

Design rules (mirror benchmark_v2.py §4):
1. Questions may use the contact name, company name, and stable identifiers
   (invoice IDs defined in generator_v3.py).
2. Questions must NOT contain amounts, dates, or other values copied from
   the randomly-generated answer body — amounts and dates are RNG-dependent
   and would break label correctness if the seed ever changed.
3. Expected signal IDs are computed deterministically from the same
   _sig_id() function used by generator_v3.py.
4. Each question has exactly one clear correct signal (single-label) unless
   the scenario explicitly involves multiple corroborating documents.
5. No temporal_ym filter is set unless the query text contains an explicit
   YYYY-MM reference.

FROZEN: SHA256 of question texts computed in _compute_v3_hash() BEFORE
        any retrieval results are recorded.  Stored in BENCHMARK_V3_HASH.
        Structural tests should verify this value matches the live questions.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from olo.m3.synthetic.generator import _sig_id
from olo.m3.synthetic.generator_v3 import (
    DELTA_ENTITY,
    DELTA_INVOICE_ID,
    DELTA_INVOICE_ID_2,
    DELTA_INVOICE_ID_3,
    HOLDOUT_SEED,
    generate_timeline_v3,
)

BENCHMARK_V3_SEED: int = HOLDOUT_SEED  # 0xB2E5_D3CC

# ── Benchmark question dataclass ──────────────────────────────────────────────

@dataclass
class BenchmarkQuestionV3:
    """A single labelled v3 holdout benchmark question."""
    id: str
    query: str
    entity_id: Optional[str]
    expected_signal_ids: list[str]
    category: str
    description: str
    temporal_ym: Optional[str] = None
    is_specific: bool = False


# ── Deterministic signal IDs for the Delta entity ────────────────────────────

_E  = DELTA_ENTITY.entity_id   # "ent_01DELT000000000000000000"

# Scenario anchors — referenced by multiple question generators.
_RESCHEDULE_EMAIL_SIG   = _sig_id(f"v3_email_reschedule_{_E}")
_RESCHEDULE_TRANS_SIG   = _sig_id(f"v3_transcript_reschedule_{_E}")
_MTG_TRANS_SIG          = _sig_id(f"v3_transcript_meeting_reschedule_{_E}")
_CONFIRM_ORIG_SIG       = _sig_id(f"v3_email_confirm_orig_{_E}")

_DISPUTE_EMAIL_SIG      = _sig_id(f"v3_email_dispute_{_E}")
_DISPUTE_TRANS_SIG      = _sig_id(f"v3_transcript_dispute_{_E}")
_STRIPE_SIG             = _sig_id(f"v3_stripe_invoice_{_E}")
_QBO_SIG                = _sig_id(f"v3_qbo_invoice_{_E}")

_PAY_CONFIRM_SIG        = _sig_id(f"v3_email_payment_confirm_{_E}")
_STRIPE2_SIG            = _sig_id(f"v3_stripe_invoice2_{_E}")
_QBO3_SIG               = _sig_id(f"v3_qbo_invoice3_{_E}")
_VOICE_QBO_SIG          = _sig_id(f"v3_voice_qbo_{_E}")

_P4_MTG_ORIG_SIG        = _sig_id(f"v3_p4_meeting_orig_{_E}")
_P4_MTG_CORR_SIG        = _sig_id(f"v3_p4_meeting_corr_{_E}")
_P4_DEL_ORIG_SIG        = _sig_id(f"v3_p4_delivery_orig_{_E}")
_P4_DEL_CORR_SIG        = _sig_id(f"v3_p4_delivery_corr_{_E}")

# Old-evidence signals
_OLD_COMMITMENT_SIG     = _sig_id(f"v3_old_commitment_{_E}")
_OLD_PAYMENT_ACK_SIG    = _sig_id(f"v3_old_payment_ack_{_E}")
_OLD_NEW_SERVICE_SIG    = _sig_id(f"v3_old_new_service_{_E}")
_OLD_COMPLAINT_SIG      = _sig_id(f"v3_old_complaint_{_E}")
_OLD_RESOLUTION_SIG     = _sig_id(f"v3_old_resolution_{_E}")
_OLD_RENEWAL_SIG        = _sig_id(f"v3_old_renewal_{_E}")
_OLD_INVOICE_QRY_SIG    = _sig_id(f"v3_old_invoice_qry_{_E}")
_OLD_ACCT_REVIEW_SIG    = _sig_id(f"v3_old_acct_review_{_E}")
_OLD_APPRECIATION_SIG   = _sig_id(f"v3_old_appreciation_{_E}")
_OLD_NEW_ORDER_SIG      = _sig_id(f"v3_old_new_order_{_E}")


# ── Question generators ───────────────────────────────────────────────────────

def _invoice_questions() -> list[BenchmarkQuestionV3]:
    """10 invoice and billing questions for Delta Finance."""
    qs: list[BenchmarkQuestionV3] = []
    ent = DELTA_ENTITY

    qs.append(BenchmarkQuestionV3(
        id="v3_inv_01",
        query=f"Show the email from {ent.company} reporting a billing discrepancy.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_DISPUTE_EMAIL_SIG],
        category="invoice",
        description="P2: Invoice dispute email (email channel only)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_02",
        query=f"What was the dispute on invoice {DELTA_INVOICE_ID}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_DISPUTE_EMAIL_SIG, _DISPUTE_TRANS_SIG],
        category="invoice",
        description="P2: Invoice dispute — email + transcript (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_03",
        query=f"Show the Stripe record for invoice {DELTA_INVOICE_ID}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_STRIPE_SIG],
        category="invoice",
        description="P2: Stripe invoice record (structured)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_04",
        query=f"Show the QuickBooks record for invoice {DELTA_INVOICE_ID}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_QBO_SIG],
        category="invoice",
        description="P2: QBO invoice record (structured)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_05",
        query=f"Show both Stripe and QuickBooks records for {ent.company} invoice {DELTA_INVOICE_ID}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_STRIPE_SIG, _QBO_SIG],
        category="invoice",
        description="P2: Cross-system billing records (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_06",
        query=f"Show the payment confirmation email for invoice {DELTA_INVOICE_ID_2} from {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_PAY_CONFIRM_SIG],
        category="invoice",
        description="Payment confirmation email for INV-DELT-2026-002",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_07",
        query=f"Show the Stripe record for the paid invoice {DELTA_INVOICE_ID_2}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_STRIPE2_SIG],
        category="invoice",
        description="Stripe paid invoice record for INV-DELT-2026-002",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_08",
        query=f"Show the QuickBooks record for invoice {DELTA_INVOICE_ID_3} for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_QBO3_SIG],
        category="invoice",
        description="QBO record for quarterly reconciliation INV-DELT-2026-003",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_09",
        query=f"Did {ent.display_name} ever query an invoice amount on a voice call?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_VOICE_QBO_SIG],
        category="invoice",
        description="Voice call QBO balance inquiry",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_inv_10",
        query=f"What billing records exist across all systems for {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_STRIPE_SIG, _QBO_SIG, _STRIPE2_SIG, _QBO3_SIG],
        category="invoice",
        description="Cross-system billing overview (multi-label)",
    ))

    return qs


def _delivery_questions() -> list[BenchmarkQuestionV3]:
    """8 delivery and logistics questions for Delta Finance."""
    qs: list[BenchmarkQuestionV3] = []
    ent = DELTA_ENTITY

    qs.append(BenchmarkQuestionV3(
        id="v3_del_01",
        query=f"Show the email where {ent.company} asked to reschedule their delivery.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_RESCHEDULE_EMAIL_SIG],
        category="delivery",
        description="P1: Delivery reschedule request email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_02",
        query=f"Show the voice transcript confirming the delivery reschedule for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_RESCHEDULE_TRANS_SIG],
        category="delivery",
        description="P1: Delivery reschedule confirmation transcript",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_03",
        query=f"Show all evidence of the delivery reschedule for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_RESCHEDULE_EMAIL_SIG, _RESCHEDULE_TRANS_SIG],
        category="delivery",
        description="P1: Delivery reschedule — email + transcript (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_04",
        query=f"Show the original delivery confirmation email we sent to {ent.display_name}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_DEL_ORIG_SIG],
        category="delivery",
        description="P4: Original delivery commitment email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_05",
        query=f"Show the revised delivery date email sent to {ent.display_name}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_DEL_CORR_SIG],
        category="delivery",
        description="P4: Corrected delivery date email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_06",
        query=f"Show all delivery timeline communications with {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_DEL_ORIG_SIG, _P4_DEL_CORR_SIG],
        category="delivery",
        description="P4: Both delivery timeline emails (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_07",
        query=f"Show the email confirming that {ent.company} delivery was still on the original date.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_CONFIRM_ORIG_SIG],
        category="delivery",
        description="P4-contradiction: Original-schedule confirmation email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_del_08",
        query=f"Did {ent.company} request any delivery changes by phone?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_RESCHEDULE_TRANS_SIG],
        category="delivery",
        description="P1: Delivery reschedule confirmed via voice",
        is_specific=True,
    ))

    return qs


def _meeting_questions() -> list[BenchmarkQuestionV3]:
    """8 meeting and calendar questions for Delta Finance."""
    qs: list[BenchmarkQuestionV3] = []
    ent = DELTA_ENTITY

    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_01",
        query=f"Did {ent.display_name} discuss rescheduling a meeting on a voice call?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_MTG_TRANS_SIG],
        category="meeting",
        description="Meeting reschedule voice transcript",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_02",
        query=f"Show the original meeting date email from {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_MTG_ORIG_SIG],
        category="meeting",
        description="P4: Original meeting date confirmation email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_03",
        query=f"Show the correction email where {ent.display_name} updated the meeting date.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_MTG_CORR_SIG],
        category="meeting",
        description="P4: Meeting date correction email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_04",
        query=f"Show all meeting date change communications from {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_MTG_ORIG_SIG, _P4_MTG_CORR_SIG],
        category="meeting",
        description="P4: Both meeting date emails (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_05",
        query=f"What is the most recent meeting scheduling email from {ent.display_name}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_MTG_CORR_SIG],
        category="meeting",
        description="P4: Correction is the most recent/authoritative meeting record",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_06",
        query=f"Show the voice call where {ent.company} asked to move a scheduled meeting.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_MTG_TRANS_SIG],
        category="meeting",
        description="Meeting reschedule voice transcript (channel-specific)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_07",
        query=f"Show all rescheduling communications (email and voice) for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_RESCHEDULE_EMAIL_SIG, _RESCHEDULE_TRANS_SIG, _MTG_TRANS_SIG],
        category="meeting",
        description="All reschedule signals (email + 2 voice transcripts)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_mtg_08",
        query=f"Show the email from {ent.company} with CORRECTION in the subject about the meeting.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_MTG_CORR_SIG],
        category="meeting",
        description="P4: Correction email identified by subject keyword",
        is_specific=True,
    ))

    return qs


def _historical_questions() -> list[BenchmarkQuestionV3]:
    """10 historical / archival questions for Delta Finance (>180 days old)."""
    qs: list[BenchmarkQuestionV3] = []
    ent = DELTA_ENTITY

    qs.append(BenchmarkQuestionV3(
        id="v3_hist_01",
        query=f"Show the support issue resolution commitment we sent to {ent.company} July 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_COMMITMENT_SIG],
        category="historical",
        description="Old-evidence: support commitment email July 2026",
        temporal_ym="ym:2026-07",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_02",
        query=f"Show the payment acknowledgement received from {ent.company} July 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_PAYMENT_ACK_SIG],
        category="historical",
        description="Old-evidence: payment acknowledgement July 2026",
        temporal_ym="ym:2026-07",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_03",
        query=f"Show the new service request email from {ent.company} August 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_NEW_SERVICE_SIG],
        category="historical",
        description="Old-evidence: new service engagement request August 2026",
        temporal_ym="ym:2026-08",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_04",
        query=f"Show the formal complaint {ent.company} filed with us August 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_COMPLAINT_SIG],
        category="historical",
        description="Old-evidence: formal complaint August 2026",
        temporal_ym="ym:2026-08",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_05",
        query=f"Show the issue resolution confirmation we sent to {ent.company} September 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_RESOLUTION_SIG],
        category="historical",
        description="Old-evidence: issue resolved notification September 2026",
        temporal_ym="ym:2026-09",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_06",
        query=f"Show the contract renewal discussion with {ent.company} October 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_RENEWAL_SIG],
        category="historical",
        description="Old-evidence: contract renewal October 2026",
        temporal_ym="ym:2026-10",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_07",
        query=f"Show the invoice amount query {ent.company} sent us October 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_INVOICE_QRY_SIG],
        category="historical",
        description="Old-evidence: invoice query October 2026",
        temporal_ym="ym:2026-10",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_08",
        query=f"Show the quarterly account review we sent to {ent.company} November 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_ACCT_REVIEW_SIG],
        category="historical",
        description="Old-evidence: quarterly account review November 2026",
        temporal_ym="ym:2026-11",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_09",
        query=f"Show the appreciation note {ent.display_name} sent us November 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_APPRECIATION_SIG],
        category="historical",
        description="Old-evidence: customer appreciation November 2026",
        temporal_ym="ym:2026-11",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_hist_10",
        query=f"Show the new purchase order request from {ent.company} November 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_NEW_ORDER_SIG],
        category="historical",
        description="Old-evidence: new order request November 2026",
        temporal_ym="ym:2026-11",
        is_specific=True,
    ))

    return qs


def _relationship_questions(timeline_sigs: list) -> list[BenchmarkQuestionV3]:
    """8 relationship and account-history questions for Delta Finance."""
    qs: list[BenchmarkQuestionV3] = []
    ent = DELTA_ENTITY

    # Collect filler invoice-dispute emails involving the entity
    filler_dispute = [
        s.signal_id for s in timeline_sigs
        if s.entity_id == ent.entity_id
        and "invoice" in (s.subject or "").lower()
        and s.kind in ("email.received", "email.sent")
        and s.signal_id not in {_DISPUTE_EMAIL_SIG, _PAY_CONFIRM_SIG}
    ][:3]

    qs.append(BenchmarkQuestionV3(
        id="v3_rel_01",
        query=f"Show the owner's written commitment to resolve a support issue for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_COMMITMENT_SIG],
        category="relationship",
        description="Relationship: owner commitment email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_02",
        query=f"Show all complaint-related emails involving {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_COMPLAINT_SIG],
        category="relationship",
        description="Relationship: formal complaint email",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_03",
        query=f"Did {ent.company} express positive feedback or appreciation?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_APPRECIATION_SIG],
        category="relationship",
        description="Relationship: customer appreciation note",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_04",
        query=f"Show emails about contract or account renewal with {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_RENEWAL_SIG],
        category="relationship",
        description="Relationship: contract renewal discussion",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_05",
        query=f"Show emails where {ent.company} placed a new service or product order.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_NEW_SERVICE_SIG, _OLD_NEW_ORDER_SIG],
        category="relationship",
        description="Relationship: new service and new order requests (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_06",
        query=f"Show the quarterly business review summary sent to {ent.display_name}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_ACCT_REVIEW_SIG],
        category="relationship",
        description="Relationship: quarterly account review",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_07",
        query=f"What evidence exists of billing disputes or payment issues for {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_DISPUTE_EMAIL_SIG, _DISPUTE_TRANS_SIG,
                             _STRIPE_SIG, _QBO_SIG],
        category="relationship",
        description="Relationship: all P2 dispute signals (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_rel_08",
        query=f"Show the issue resolution email confirming a problem was fixed for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_RESOLUTION_SIG],
        category="relationship",
        description="Relationship: issue resolved confirmation",
        is_specific=True,
    ))

    return qs


def _disambiguation_questions() -> list[BenchmarkQuestionV3]:
    """6 channel-disambiguation and cross-source questions for Delta Finance."""
    qs: list[BenchmarkQuestionV3] = []
    ent = DELTA_ENTITY

    qs.append(BenchmarkQuestionV3(
        id="v3_dis_01",
        query=f"Show only the email (not voice) where {ent.company} reported the billing discrepancy.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_DISPUTE_EMAIL_SIG],
        category="disambiguation",
        description="Channel-dis: email channel for billing dispute (not voice)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_dis_02",
        query=f"Show only the voice call where {ent.company} raised the billing issue.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_DISPUTE_TRANS_SIG, _VOICE_QBO_SIG],
        category="disambiguation",
        description="Channel-dis: voice calls for billing (dispute transcript + QBO inquiry)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_dis_03",
        query=f"Show the payment acknowledgement email from {ent.company} in our records.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_PAYMENT_ACK_SIG],
        category="disambiguation",
        description="Hist-dis: payment acknowledgement email (archival, specific)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_dis_04",
        query=f"Show the invoice query or question that {ent.company} sent us.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_OLD_INVOICE_QRY_SIG],
        category="disambiguation",
        description="Hist-dis: invoice query email (archival, specific)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_dis_05",
        query=f"What billing data exists for {ent.company} in both Stripe and QuickBooks for invoice {DELTA_INVOICE_ID}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_STRIPE_SIG, _QBO_SIG],
        category="disambiguation",
        description="Cross-source: Stripe + QBO for INV-DELT-2026-001 (multi-label)",
    ))
    qs.append(BenchmarkQuestionV3(
        id="v3_dis_06",
        query=f"Was the corrected delivery date communicated by email or phone to {ent.display_name}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_P4_DEL_CORR_SIG],
        category="disambiguation",
        description="Delivery correction: revised email is the correction channel",
        is_specific=True,
    ))

    return qs


# ── Main builder ──────────────────────────────────────────────────────────────

def build_benchmark_v3(
    timeline: Optional[list] = None,
) -> list[BenchmarkQuestionV3]:
    """Build the ~50 question v3 holdout benchmark.

    Args:
        timeline: Pre-generated v3 timeline (avoids re-generating).  If None,
                  generates fresh with HOLDOUT_SEED.

    Returns:
        List of BenchmarkQuestionV3 (approximately 50 questions).
    """
    if timeline is None:
        timeline = generate_timeline_v3()

    questions: list[BenchmarkQuestionV3] = []
    questions.extend(_invoice_questions())       # 10
    questions.extend(_delivery_questions())      #  8
    questions.extend(_meeting_questions())       #  8
    questions.extend(_historical_questions())    # 10
    questions.extend(_relationship_questions(timeline))  #  8
    questions.extend(_disambiguation_questions())        #  6
    # Total: 50

    return questions


# ── Freeze and hash ───────────────────────────────────────────────────────────

#: Frozen SHA256 hash of the v3 benchmark question texts.
#: Computed on first call, cached for the process lifetime.
#: Recompute after intentional question changes:
#:   python -c "
#:   from olo.m3.benchmark_v3 import _compute_v3_hash
#:   print(_compute_v3_hash())
#:   "
BENCHMARK_V3_HASH: Optional[str] = None


def _compute_v3_hash() -> str:
    """Compute SHA256 of the benchmark question texts + expected signal IDs."""
    qs = build_benchmark_v3()
    canon = json.dumps(
        [
            {
                "id": q.id,
                "query": q.query,
                "expected": sorted(q.expected_signal_ids),
            }
            for q in qs
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def get_benchmark_v3_hash() -> str:
    """Return the benchmark hash, computing and caching it on first call."""
    global BENCHMARK_V3_HASH
    if not BENCHMARK_V3_HASH:
        BENCHMARK_V3_HASH = _compute_v3_hash()
    return BENCHMARK_V3_HASH
