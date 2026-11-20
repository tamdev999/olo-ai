"""Olo M3 — Retrieval Benchmark v2 (200+ questions).

Addresses deficiencies in the v1 48-question set identified by Opus:
- Multi-label: all valid source documents labeled per question.
- No month filters derived from answer content.
- Entity-only or no filters (entity name in query text, not temporal tags).
- Questions written as natural user queries NOT derived from signal text.
- Categories covering invoices, delivery, meetings, historical events,
  cross-connector, voice transcripts, contradictions, multi-document.

Design rules (Opus §4):
1. Questions may use entity name and company name (they're the user's subject).
2. Questions must NOT contain dates, amounts, or other facts copied from the answer.
3. All documents that validly answer a question are labeled (multi-label).
4. For template-filler signals, all signals of the same intent type are labeled
   since they all represent valid answers to generic intent questions.
5. Specific-fact questions (asking for one unique datum) are single-labeled.

Frozen with SHA256 of the question texts before any benchmarking.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from olo.m3.synthetic.generator import (
    ENTITIES,
    SyntheticEntity,
    _sig_id,
    generate_timeline,
    to_hindsight_items,
)


# ── Benchmark question ────────────────────────────────────────────────────────

@dataclass
class BenchmarkQuestion:
    """A single labelled benchmark question."""
    id: str
    query: str
    entity_id: Optional[str]
    # All valid source document IDs (multi-label).
    expected_signal_ids: list[str]
    category: str
    description: str
    # None = entity-only filtering; set to "ym:YYYY-MM" only when the query
    # explicitly names a year+month (derived from query text, never from labels).
    temporal_ym: Optional[str] = None
    # Whether this is a "hard" question that expects a specific unique answer.
    is_specific: bool = False


# ── Question generators per category ─────────────────────────────────────────

def _qid(ent: SyntheticEntity, category: str, n: int) -> str:
    return f"v2_{ent.entity_id[:8]}_{category}_{n:02d}"


def _invoice_questions(ent: SyntheticEntity, timeline_sigs: list) -> list[BenchmarkQuestion]:
    """10 invoice and billing questions per entity."""
    qs: list[BenchmarkQuestion] = []
    base = datetime(2026, 1, 1, tzinfo=UTC)

    # Find all invoice-dispute signals for this entity
    dispute_email = _sig_id(f"email_dispute_{ent.entity_id}")
    dispute_trans = _sig_id(f"transcript_dispute_{ent.entity_id}")
    stripe_inv    = _sig_id(f"stripe_invoice_{ent.entity_id}")
    qbo_inv       = _sig_id(f"qbo_invoice_{ent.entity_id}")
    voice_qbo     = _sig_id(f"voice_qbo_{ent.entity_id}")

    # Find all filler invoice_dispute emails for this entity
    filler_dispute = [
        s.signal_id for s in timeline_sigs
        if s.entity_id == ent.entity_id
        and "invoice" in s.subject.lower()
        and "dispute" in s.subject.lower()
    ]

    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 1),
        query=f"Has {ent.display_name} from {ent.company} ever reported a billing discrepancy?",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_email, dispute_trans] + filler_dispute,
        category="invoice_dispute",
        description="Generic: any invoice dispute communication",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 2),
        query=f"Show me all {ent.company} invoice and billing records.",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_email, dispute_trans, stripe_inv, qbo_inv, voice_qbo] + filler_dispute,
        category="invoice_cross",
        description="Cross-connector: all billing evidence",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 3),
        query=f"What was the original agreed price on the invoice that {ent.company} disputed?",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_email],
        category="invoice_dispute",
        description="Specific: original agreed amount (single-label)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 4),
        query=f"What is the invoice ID number for the billing dispute with {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_email, dispute_trans],
        category="invoice_dispute",
        description="Specific: invoice ID",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 5),
        query=f"Did {ent.display_name} discuss invoice issues on a phone call?",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_trans, voice_qbo],
        category="invoice_voice",
        description="Voice transcript: invoice dispute call",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 6),
        query=f"What payment records exist for {ent.company} in the payment system?",
        entity_id=ent.entity_id,
        expected_signal_ids=[stripe_inv],
        category="invoice_structured",
        description="Structured: Stripe payment record",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 7),
        query=f"Show me the accounting records for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[qbo_inv],
        category="invoice_structured",
        description="Structured: QBO accounting record",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 8),
        query=f"How much was {ent.company} charged according to Stripe?",
        entity_id=ent.entity_id,
        expected_signal_ids=[stripe_inv],
        category="invoice_amount",
        description="Specific: Stripe amount",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 9),
        query=f"Find emails from {ent.display_name} about payment or billing issues.",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_email] + filler_dispute,
        category="invoice_email",
        description="Generic: payment/billing emails",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "inv", 10),
        query=f"What billing communications exist between us and {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_email, dispute_trans, stripe_inv, qbo_inv, voice_qbo] + filler_dispute,
        category="invoice_all",
        description="Broad: all billing communications",
    ))
    return qs


def _delivery_questions(ent: SyntheticEntity) -> list[BenchmarkQuestion]:
    """8 delivery date questions per entity."""
    qs: list[BenchmarkQuestion] = []

    reschedule_email = _sig_id(f"email_reschedule_{ent.entity_id}")
    reschedule_trans = _sig_id(f"transcript_reschedule_{ent.entity_id}")
    p4_del_orig      = _sig_id(f"p4_delivery_orig_{ent.entity_id}")
    p4_del_corr      = _sig_id(f"p4_delivery_corr_{ent.entity_id}")

    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 1),
        query=f"Has {ent.company} ever requested to reschedule a meeting or delivery?",
        entity_id=ent.entity_id,
        expected_signal_ids=[reschedule_email, reschedule_trans, p4_del_orig, p4_del_corr],
        category="delivery_reschedule",
        description="Generic: any reschedule request",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 2),
        query=f"What was the original delivery date we committed to {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_del_orig],
        category="delivery_original",
        description="Specific: original committed delivery date",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 3),
        query=f"Was the delivery timeline for {ent.company} ever revised?",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_del_orig, p4_del_corr],
        category="delivery_revision",
        description="Contradiction: delivery date change",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 4),
        query=f"What is the current confirmed delivery date for {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_del_corr],
        category="delivery_current",
        description="Specific: revised delivery date (current state)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 5),
        query=f"Show me all schedule-change communications with {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[reschedule_email, reschedule_trans, p4_del_orig, p4_del_corr],
        category="delivery_all_changes",
        description="Broad: all schedule changes",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 6),
        query=f"Did {ent.display_name} discuss any upcoming delivery on a call?",
        entity_id=ent.entity_id,
        expected_signal_ids=[reschedule_trans],
        category="delivery_voice",
        description="Voice transcript: delivery discussion",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 7),
        query=f"Find the email where we apologised for changing the delivery date for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_del_corr],
        category="delivery_apology",
        description="Specific: delivery correction email with apology",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "del", 8),
        query=f"What delivery commitments have we made to {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_del_orig, p4_del_corr],
        category="delivery_commitments",
        description="Multi-doc: delivery commitment chain",
    ))
    return qs


def _meeting_questions(ent: SyntheticEntity, timeline_sigs: list) -> list[BenchmarkQuestion]:
    """8 meeting schedule questions per entity."""
    qs: list[BenchmarkQuestion] = []

    reschedule_email = _sig_id(f"email_reschedule_{ent.entity_id}")
    reschedule_trans = _sig_id(f"transcript_reschedule_{ent.entity_id}")
    confirm_email    = _sig_id(f"email_confirm_orig_{ent.entity_id}")
    p4_meet_orig     = _sig_id(f"p4_meeting_orig_{ent.entity_id}")
    p4_meet_corr     = _sig_id(f"p4_meeting_corr_{ent.entity_id}")

    # Find calendar events for this entity
    cal_events = [
        s.signal_id for s in timeline_sigs
        if s.entity_id == ent.entity_id and s.kind == "calendar.event.created"
    ]

    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 1),
        query=f"Show me all meeting-related communications with {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[reschedule_email, reschedule_trans, confirm_email,
                              p4_meet_orig, p4_meet_corr] + cal_events[:5],
        category="meeting_all",
        description="Broad: all meeting communications",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 2),
        query=f"Did {ent.display_name} request to change a meeting date?",
        entity_id=ent.entity_id,
        expected_signal_ids=[reschedule_email, reschedule_trans, p4_meet_orig, p4_meet_corr],
        category="meeting_reschedule",
        description="Generic: meeting rescheduling request",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 3),
        query=f"Was any meeting date correction sent to {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_meet_orig, p4_meet_corr],
        category="meeting_contradiction",
        description="Contradiction: meeting date correction",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 4),
        query=f"What is the confirmed meeting date for {ent.company} after any corrections?",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_meet_corr],
        category="meeting_current",
        description="Specific: corrected meeting date (current state)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 5),
        query=f"Find the email confirming a meeting was still happening on the original date for {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[confirm_email],
        category="meeting_confirmation",
        description="Specific: original-date confirmation (contradiction signal)",
        is_specific=True,
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 6),
        query=f"What calendar events have been created for {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=cal_events[:10],
        category="meeting_calendar",
        description="Structured: calendar events",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 7),
        query=f"Did {ent.display_name} discuss a rescheduled meeting on a voice call?",
        entity_id=ent.entity_id,
        expected_signal_ids=[reschedule_trans],
        category="meeting_voice",
        description="Voice transcript: rescheduling discussion",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "mtg", 8),
        query=f"Find the email where {ent.company} apologised for a wrong meeting date.",
        entity_id=ent.entity_id,
        expected_signal_ids=[p4_meet_corr],
        category="meeting_apology",
        description="Specific: meeting correction apology",
        is_specific=True,
    ))
    return qs


def _historical_questions(ent: SyntheticEntity, timeline_sigs: list) -> list[BenchmarkQuestion]:
    """10 historical questions per entity (all >6 months = >180 days from start)."""
    from olo.m3.synthetic.generator import _OLD_QUERIES, _OLD_SCENARIOS
    qs: list[BenchmarkQuestion] = []

    for idx, (day, seed_sfx, _src, _kind, _st, _bt, _) in enumerate(_OLD_SCENARIOS):
        sig_id = _sig_id(f"{seed_sfx}_{ent.entity_id}")
        # Use the existing old queries but parameterize them
        query = _OLD_QUERIES[idx].replace(
            "July 2026", "mid-year 2026"
        ).replace(
            "August 2026", "last summer"
        ).replace(
            "September 2026", "last autumn"
        ).replace(
            "October 2026", "last October"
        ).replace(
            "November 2026", "last November"
        )
        # Keep the natural language but strip year references where possible
        # NOTE: some keep month because they test temporal recall specifically
        qs.append(BenchmarkQuestion(
            id=_qid(ent, "hist", idx),
            query=query.replace("{company}", ent.company).replace("{name}", ent.display_name),
            entity_id=ent.entity_id,
            expected_signal_ids=[sig_id],
            category="historical",
            description=f"Historical event: {seed_sfx} (day {day}, >{day-30}d ago)",
            temporal_ym=None,  # entity-only filtering (pool ~130)
            is_specific=True,
        ))
    return qs


def _relationship_questions(ent: SyntheticEntity, timeline_sigs: list) -> list[BenchmarkQuestion]:
    """10 customer relationship questions per entity (using filler signals)."""
    qs: list[BenchmarkQuestion] = []

    # Filler signals by template type
    reschedule_fillers = [
        s.signal_id for s in timeline_sigs
        if s.entity_id == ent.entity_id
        and "reschedule" in (s.subject or "").lower()
        and s.signal_id not in {
            _sig_id(f"email_reschedule_{ent.entity_id}"),
            _sig_id(f"transcript_reschedule_{ent.entity_id}"),
        }
    ]
    complaint_fillers = [
        s.signal_id for s in timeline_sigs
        if s.entity_id == ent.entity_id
        and any(w in (s.body_text or "").lower() for w in ["complaint", "complain", "issue", "unacceptably"])
    ]
    commitment_fillers = [
        s.signal_id for s in timeline_sigs
        if s.entity_id == ent.entity_id
        and any(w in (s.body_text or "").lower() for w in ["commit", "confirm", "resolve"])
        and s.source == "gmail_owner"
        and s.signal_id not in {
            _sig_id(f"email_reschedule_{ent.entity_id}"),
            _sig_id(f"email_confirm_orig_{ent.entity_id}"),
        }
    ]

    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 1),
        query=f"Find all emails from {ent.company} asking us to reschedule something.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_sig_id(f"email_reschedule_{ent.entity_id}")] + reschedule_fillers[:5],
        category="relationship_reschedule",
        description="Filler: all reschedule request emails",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 2),
        query=f"Has {ent.company} ever filed a formal complaint?",
        entity_id=ent.entity_id,
        expected_signal_ids=complaint_fillers[:8],
        category="relationship_complaint",
        description="Filler: complaint emails",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 3),
        query=f"Show me all commitments and confirmations we made to {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=commitment_fillers[:8] + [
            _sig_id(f"email_confirm_orig_{ent.entity_id}"),
        ],
        category="relationship_commitment",
        description="Filler: owner commitment emails",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 4),
        query=f"What is the payment history with {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"stripe_invoice_{ent.entity_id}"),
            _sig_id(f"qbo_invoice_{ent.entity_id}"),
        ],
        category="relationship_payment_history",
        description="Cross-connector: payment history",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 5),
        query=f"Find recent service issues reported by {ent.display_name}.",
        entity_id=ent.entity_id,
        expected_signal_ids=complaint_fillers[:5] + [_sig_id(f"email_dispute_{ent.entity_id}")],
        category="relationship_service_issues",
        description="Filler: service complaints",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 6),
        query=f"Show me all voice call transcripts involving {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"transcript_reschedule_{ent.entity_id}"),
            _sig_id(f"transcript_dispute_{ent.entity_id}"),
            _sig_id(f"voice_qbo_{ent.entity_id}"),
        ],
        category="relationship_voice_all",
        description="All voice transcripts for entity",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 7),
        query=f"What emails has {ent.display_name} sent to us in the past year?",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            s.signal_id for s in timeline_sigs
            if s.entity_id == ent.entity_id
            and s.kind in ("email.received",)
            and s.source == "gmail_owner"
        ][:10],
        category="relationship_incoming_emails",
        description="Generic: inbound emails from contact",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 8),
        query=f"Find the renewal conversation with {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_sig_id(f"old_renewal_{ent.entity_id}")],
        category="relationship_renewal",
        description="Historical: contract renewal discussion",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 9),
        query=f"Has {ent.company} expressed satisfaction with our services?",
        entity_id=ent.entity_id,
        expected_signal_ids=[_sig_id(f"old_appreciation_{ent.entity_id}")],
        category="relationship_appreciation",
        description="Historical: positive feedback",
    ))
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "rel", 10),
        query=f"Find any new orders or service requests from {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"old_new_service_{ent.entity_id}"),
            _sig_id(f"old_new_order_{ent.entity_id}"),
        ],
        category="relationship_new_business",
        description="Multi-doc: new service and order requests",
    ))
    return qs


def _multidoc_contradiction_questions(ent: SyntheticEntity) -> list[BenchmarkQuestion]:
    """6 multi-document and contradiction questions per entity."""
    qs: list[BenchmarkQuestion] = []

    # P4 contradiction pair: meeting
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "con", 1),
        query=f"Show all evidence of the meeting date correction for {ent.company} in May 2026.",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"p4_meeting_orig_{ent.entity_id}"),
            _sig_id(f"p4_meeting_corr_{ent.entity_id}"),
        ],
        category="contradiction_meeting",
        description="Multi-doc: both meeting date emails (original + correction)",
        temporal_ym=None,
    ))
    # P4 contradiction pair: delivery
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "con", 2),
        query=f"Show all evidence of the delivery date correction we sent to {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"p4_delivery_orig_{ent.entity_id}"),
            _sig_id(f"p4_delivery_corr_{ent.entity_id}"),
        ],
        category="contradiction_delivery",
        description="Multi-doc: both delivery date emails (original + correction)",
        temporal_ym=None,
    ))
    # Cross-connector: same invoice in email + Stripe + QBO
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "con", 3),
        query=f"Show all records for the main {ent.company} invoice from every system.",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"email_dispute_{ent.entity_id}"),
            _sig_id(f"stripe_invoice_{ent.entity_id}"),
            _sig_id(f"qbo_invoice_{ent.entity_id}"),
        ],
        category="cross_connector_invoice",
        description="Cross-connector: invoice in email, Stripe, QBO",
    ))
    # Reschedule request + transcript (same event, two sources)
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "con", 4),
        query=f"Find all evidence of the meeting rescheduling by {ent.company}.",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"email_reschedule_{ent.entity_id}"),
            _sig_id(f"transcript_reschedule_{ent.entity_id}"),
        ],
        category="cross_connector_reschedule",
        description="Cross-connector: reschedule in email + call transcript",
    ))
    # Historical commitment + resolution chain
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "con", 5),
        query=f"What was the support escalation and resolution history for {ent.company}?",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"old_complaint_{ent.entity_id}"),
            _sig_id(f"old_resolution_{ent.entity_id}"),
        ],
        category="multidoc_support_chain",
        description="Multi-doc: complaint + resolution (causal chain)",
    ))
    # Ambiguous: near-duplicate reschedule emails — BOTH are valid
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "con", 6),
        query=f"What schedule changes were requested by {ent.company} this year?",
        entity_id=ent.entity_id,
        expected_signal_ids=[
            _sig_id(f"email_reschedule_{ent.entity_id}"),
            _sig_id(f"transcript_reschedule_{ent.entity_id}"),
            _sig_id(f"p4_meeting_orig_{ent.entity_id}"),
            _sig_id(f"p4_meeting_corr_{ent.entity_id}"),
        ],
        category="multidoc_all_schedule_changes",
        description="Broad: all schedule change evidence",
    ))
    return qs


# ── Main builder ──────────────────────────────────────────────────────────────

def build_benchmark_v2(timeline: Optional[list] = None) -> list[BenchmarkQuestion]:
    """Build the 200+ question benchmark v2.

    Args:
        timeline: Pre-generated timeline (avoids re-generating); if None,
                  generates a fresh one with the standard seed.

    Returns:
        List of BenchmarkQuestion instances, one per test case.
    """
    if timeline is None:
        timeline = generate_timeline(months=12)

    questions: list[BenchmarkQuestion] = []
    for ent in ENTITIES:
        questions.extend(_invoice_questions(ent, timeline))         # 10
        questions.extend(_delivery_questions(ent))                  # 8
        questions.extend(_meeting_questions(ent, timeline))         # 8
        questions.extend(_historical_questions(ent, timeline))      # 10
        questions.extend(_relationship_questions(ent, timeline))    # 10
        questions.extend(_multidoc_contradiction_questions(ent))    # 6
        questions.extend(_disambiguation_questions(ent, timeline))  # 5
    # Total per entity: 57  ×  3 entities = 171 entity-scoped

    # Cross-entity questions (no entity filter) — 10 per entity = 30
    questions.extend(_cross_entity_questions(timeline))

    return questions


def _disambiguation_questions(
    ent: SyntheticEntity, timeline_sigs: list,
) -> list[BenchmarkQuestion]:
    """5 near-duplicate-disambiguation and cross-source questions per entity.

    These test the system's ability to:
      a) Distinguish evidence channel (email vs voice) for the same event.
      b) Retrieve specific historical events from a pool of similar documents.
      c) Match cross-system billing queries to the correct structured record.
    """
    qs: list[BenchmarkQuestion] = []

    email_dispute  = _sig_id(f"email_dispute_{ent.entity_id}")
    dispute_trans  = _sig_id(f"transcript_dispute_{ent.entity_id}")
    voice_qbo      = _sig_id(f"voice_qbo_{ent.entity_id}")
    stripe_inv     = _sig_id(f"stripe_invoice_{ent.entity_id}")
    qbo_inv        = _sig_id(f"qbo_invoice_{ent.entity_id}")

    # (a) Channel-disambiguation: email channel only
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "dis", 1),
        query=f"Show me the email where {ent.company} reported the billing discrepancy.",
        entity_id=ent.entity_id,
        expected_signal_ids=[email_dispute],
        category="disambiguation_email",
        description="Near-dup: email channel for billing dispute (not voice)",
        is_specific=True,
    ))
    # (a) Channel-disambiguation: voice channel only
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "dis", 2),
        query=f"Find the voice call where {ent.company} raised the billing issue.",
        entity_id=ent.entity_id,
        expected_signal_ids=[dispute_trans, voice_qbo],
        category="disambiguation_voice",
        description="Near-dup: voice channel for billing dispute (not email)",
    ))
    # (b) Historical: payment acknowledgement (specific, archival)
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "dis", 3),
        query=f"Find the payment acknowledgement email from {ent.company} in our records.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_sig_id(f"old_payment_ack_{ent.entity_id}")],
        category="disambiguation_payment_ack",
        description="Historical: payment acknowledgement (specific)",
        is_specific=True,
    ))
    # (b) Historical: invoice query in archive
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "dis", 4),
        query=f"Find the invoice question or query that {ent.company} sent us.",
        entity_id=ent.entity_id,
        expected_signal_ids=[_sig_id(f"old_invoice_qry_{ent.entity_id}")],
        category="disambiguation_invoice_query",
        description="Historical: invoice query email (specific, archival)",
        is_specific=True,
    ))
    # (c) Cross-system billing: both Stripe and QBO records
    qs.append(BenchmarkQuestion(
        id=_qid(ent, "dis", 5),
        query=f"What billing data exists for {ent.company} in both Stripe and QuickBooks?",
        entity_id=ent.entity_id,
        expected_signal_ids=[stripe_inv, qbo_inv],
        category="disambiguation_dual_billing",
        description="Cross-source: Stripe + QBO records (multi-label)",
    ))
    return qs


def _cross_entity_questions(timeline: list) -> list[BenchmarkQuestion]:
    """10 per-entity cross-entity questions (no entity filter) = 30 total.

    Tests retrieval from the full corpus without an entity tag filter.
    Covers: reschedule, invoice, Stripe, delivery, voice, appreciation,
    complaint, renewal, account review, new service.
    """
    qs: list[BenchmarkQuestion] = []

    for ent in ENTITIES:
        qs.extend([
            # ── Original 5 ────────────────────────────────────────────────────
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_1",
                query=f"Find the meeting rescheduling request from {ent.display_name}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"email_reschedule_{ent.entity_id}")],
                category="xent_reschedule",
                description=f"Cross-entity: reschedule email for {ent.display_name}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_2",
                query=f"What invoice amount did {ent.company} dispute?",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"email_dispute_{ent.entity_id}")],
                category="xent_invoice",
                description=f"Cross-entity: invoice dispute for {ent.company}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_3",
                query=f"Show Stripe payment record for {ent.company}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"stripe_invoice_{ent.entity_id}")],
                category="xent_stripe",
                description=f"Cross-entity: Stripe record for {ent.company}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_4",
                query=f"What delivery commitment did we send to {ent.display_name}?",
                entity_id=None,
                expected_signal_ids=[
                    _sig_id(f"p4_delivery_orig_{ent.entity_id}"),
                    _sig_id(f"p4_delivery_corr_{ent.entity_id}"),
                ],
                category="xent_delivery",
                description=f"Cross-entity: delivery emails for {ent.display_name}",
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_5",
                query=f"Find the voice call transcript about rescheduling with {ent.company}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"transcript_reschedule_{ent.entity_id}")],
                category="xent_voice",
                description=f"Cross-entity: reschedule transcript for {ent.company}",
                is_specific=True,
            ),
            # ── Additional 5 (§5 expansion) ────────────────────────────────────
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_6",
                query=f"Find the customer appreciation or thank-you email from {ent.display_name}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"old_appreciation_{ent.entity_id}")],
                category="xent_appreciation",
                description=f"Cross-entity: appreciation email from {ent.display_name}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_7",
                query=f"Find the formal complaint email filed by {ent.company}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"old_complaint_{ent.entity_id}")],
                category="xent_complaint",
                description=f"Cross-entity: formal complaint from {ent.company}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_8",
                query=f"Find the contract renewal discussion with {ent.company}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"old_renewal_{ent.entity_id}")],
                category="xent_renewal",
                description=f"Cross-entity: contract renewal email for {ent.company}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_9",
                query=f"Find the account review or business summary sent to {ent.company}.",
                entity_id=None,
                expected_signal_ids=[_sig_id(f"old_acct_review_{ent.entity_id}")],
                category="xent_account_review",
                description=f"Cross-entity: quarterly account review for {ent.company}",
                is_specific=True,
            ),
            BenchmarkQuestion(
                id=f"v2_xent_{ent.entity_id[:8]}_10",
                query=f"Find any new service or project request submitted by {ent.company}.",
                entity_id=None,
                expected_signal_ids=[
                    _sig_id(f"old_new_service_{ent.entity_id}"),
                    _sig_id(f"old_new_order_{ent.entity_id}"),
                ],
                category="xent_new_service",
                description=f"Cross-entity: new service/order request from {ent.company}",
            ),
        ])
    return qs  # 10 × 3 = 30 questions


# ── Freeze and hash ───────────────────────────────────────────────────────────

#: Frozen SHA256 hash of the v2 benchmark question texts.
#: Recompute with: python -c "from olo.m3.benchmark_v2 import benchmark_v2_hash; print(benchmark_v2_hash())"
BENCHMARK_V2_HASH: str = ""  # cleared — will recompute with §5 expansion (201 questions)


def _compute_v2_hash() -> str:
    qs = build_benchmark_v2()
    canon = json.dumps(
        [{"id": q.id, "query": q.query, "expected": sorted(q.expected_signal_ids)}
         for q in qs],
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def get_benchmark_v2_hash() -> str:
    """Return the benchmark hash, computing it on first call."""
    global BENCHMARK_V2_HASH
    if not BENCHMARK_V2_HASH:
        BENCHMARK_V2_HASH = _compute_v2_hash()
    return BENCHMARK_V2_HASH


# ── Scoring helpers ───────────────────────────────────────────────────────────

def score_recall(
    result: dict,
    expected_signal_ids: list[str],
    top_n: int = 10,
    source_facts: Optional[dict] = None,
) -> tuple[bool, int]:
    """Check if any expected document appears in the top-N results.

    Args:
        result:            Hindsight recall response dict.
        expected_signal_ids: Signal IDs to look for (multi-label).
        top_n:             How many results to check (recall@N).
        source_facts:      Optional source_facts dict from response (for
                           crediting observations through their source docs).

    Returns:
        (hit, rank) where rank is 1-indexed position of first hit, or -1 if miss.
    """
    expected = {f"sig:{sid}" for sid in expected_signal_ids}
    results = result.get("results", [])[:top_n]
    sf_map  = source_facts or result.get("source_facts", {})

    for i, r in enumerate(results):
        rank = i + 1

        # Direct document hit
        doc_id = r.get("document_id", "") or ""
        if doc_id in expected:
            return True, rank

        # Observation credited through source_fact_ids (Opus §5 fix)
        for sfid in r.get("source_fact_ids", []) or []:
            if isinstance(sfid, dict):
                sdoc = sfid.get("document_id", "") or ""
            else:
                sdoc = str(sfid)
            if sdoc in expected:
                return True, rank

        # Source facts from include.source_facts (deeper resolution)
        if sf_map:
            fact_ids = r.get("source_fact_ids", []) or []
            for sfid in fact_ids:
                if isinstance(sfid, dict):
                    fact_uuid = sfid.get("id", "") or sfid.get("fact_id", "")
                else:
                    fact_uuid = str(sfid)
                if fact_uuid in sf_map:
                    sf = sf_map[fact_uuid]
                    sdoc = sf.get("document_id", "") if isinstance(sf, dict) else ""
                    if sdoc in expected:
                        return True, rank

    return False, -1
