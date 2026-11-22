"""Olo M3 — v3 Holdout Benchmark Generator.

Produces ~130 signals for a single 4th entity (Diana Patel / Delta Finance)
using a DIFFERENT fixed seed from the main corpus.

Key design invariants
---------------------
- Seed 0xB2E5_D3CC is distinct from the main corpus seed 0xA1D3_B1BB.
- A fresh random.Random(HOLDOUT_SEED) is created on every call so repeated
  calls always return an identical corpus.
- Only the Delta Finance entity is generated here — this corpus is completely
  independent of the three main-corpus entities.
- The corpus is FROZEN BEFORE ANY RETRIEVAL RUN.  Do not modify after first
  retrieval results are recorded.

Opus architecture ref: §9 (PoC data); holdout isolation requirement §4.3.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

# Re-use shared dataclasses and helpers from the main generator.
from olo.m3.synthetic.generator import (
    OWNER_EMAIL,
    SyntheticEntity,
    SyntheticSignal,
    _EMAIL_TEMPLATES,
    _OLD_SCENARIOS,
    _TRANSCRIPT_TEMPLATES,
    _sig_id,
    _ym,
)

# ── v3 Holdout seed ───────────────────────────────────────────────────────────
# MUST differ from _DEFAULT_SEED = 0xA1D3_B1BB used by the main corpus.
HOLDOUT_SEED: int = 0xB2E5_D3CC

# ── Delta Finance entity ──────────────────────────────────────────────────────
DELTA_ENTITY = SyntheticEntity(
    entity_id="ent_01DELT000000000000000000",
    display_name="Diana Patel",
    email="diana.patel@delta-finance.example",
    phone="+14155550404",
    company="Delta Finance",
    company_email="billing@delta-finance.example",
    stripe_customer_id="stripe:cus_delta_004",
    qbo_customer_id="qbo:realm123:cust_004",
)

# Deterministic invoice IDs — referenced directly in benchmark questions.
DELTA_INVOICE_ID       = "INV-DELT-2026-001"   # P2 dispute invoice
DELTA_INVOICE_ID_2     = "INV-DELT-2026-002"   # payment confirmation invoice
DELTA_INVOICE_ID_3     = "INV-DELT-2026-003"   # quarterly reconciliation invoice

# ── Old-evidence scenario seeds for Delta (days 185–318) ─────────────────────
# Same structure as _OLD_SCENARIOS but with delta-specific seed suffixes so
# signal IDs never collide with the main-corpus signals.
_OLD_SCENARIOS_V3 = [
    # (day, seed_suffix, source, kind, subject_tmpl, body_tmpl, is_llm_eligible)
    (185, "v3_old_commitment",   "gmail_owner", "email.sent",
     "Commitment: Support Resolution — {company}",
     ("Following our recent discussion, I want to confirm my commitment to {company}: "
      "we will resolve the outstanding support issue by {resolve_date}. "
      "I have personally escalated this to our engineering team. — Owner"),
     True),
    (200, "v3_old_payment_ack",  "gmail_owner", "email.received",
     "Payment Acknowledgement — {company}",
     ("Hi, confirming that payment of ${amount:.2f} has been processed for {company}. "
      "Payment reference PAY-OLD-V3-{idx:04d} is on file. The account is now current. "
      "Thanks, {name}"),
     True),
    (220, "v3_old_new_service",  "gmail_owner", "email.received",
     "New Service Request — {company}",
     ("Hello, I would like to request a new service engagement for {company}. "
      "We need {service_type} support for our upcoming project. "
      "Please send a proposal at your earliest convenience. Thanks, {name}"),
     True),
    (240, "v3_old_complaint",    "gmail_owner", "email.received",
     "Formal Complaint — {company}",
     ("I am writing to formally complain about the recent support experience with {company}. "
      "The response time was unacceptably slow and the issue remains unresolved. "
      "This needs immediate attention from management. — {name}, {company}"),
     True),
    (260, "v3_old_resolution",   "gmail_owner", "email.sent",
     "Issue Resolved — {company}",
     ("Dear {name}, I am pleased to confirm that the issue raised by {company} "
      "has been fully resolved. The root cause was identified and corrected. "
      "Please let us know if you need anything further. — Owner"),
     True),
    (280, "v3_old_renewal",      "gmail_owner", "email.received",
     "Contract Renewal Discussion — {company}",
     ("Hi, as we approach the end of our current agreement with {company}, "
      "I would like to discuss renewal options. "
      "Could we schedule a call to review pricing and scope? Best, {name}"),
     True),
    (300, "v3_old_invoice_qry",  "gmail_owner", "email.received",
     "Invoice Query — {company}",
     ("Hello, I noticed an invoice for ${amount:.2f} from {company} but I am "
      "missing the itemised breakdown. Could you resend the details? Thanks, {name}"),
     True),
    (310, "v3_old_acct_review",  "gmail_owner", "email.sent",
     "Quarterly Account Review — {company}",
     ("Dear {name}, following our quarterly account review for {company}, "
      "here is a summary: total engagements on track, all support tickets resolved, "
      "upcoming renewal due next quarter. Please confirm if you need any changes. — Owner"),
     True),
    (315, "v3_old_appreciation", "gmail_owner", "email.received",
     "Appreciation Note — {company}",
     ("Hi, I wanted to take a moment to appreciate the excellent service "
      "{company} has received from your team this quarter. "
      "The turnaround time and quality have been outstanding. Thank you! — {name}"),
     True),
    (318, "v3_old_new_order",    "gmail_owner", "email.received",
     "New Order Request — {company}",
     ("Hello, I would like to place a new order for {company}. "
      "Please quote for {product} with delivery by end of this month. "
      "Looking forward to continuing our business relationship. — {name}"),
     True),
]

# ── Signal generation ─────────────────────────────────────────────────────────

def _add_delta_signals(
    out: list[SyntheticSignal],
    ent: SyntheticEntity,
    start: datetime,
    end: datetime,
    rng: random.Random,
) -> None:
    """Add all signal types for the Delta Finance entity.

    Mirrors _add_entity_signals() from generator.py but uses:
    - v3-specific invoice IDs (INV-DELT-2026-*)
    - Seed-suffixes prefixed with 'v3_' so signal IDs never collide with
      main-corpus signals.
    - ~130 total signals (≈ per-entity scale of main corpus).
    """
    total_days = (end - start).days

    # ── P1 scenario: delivery reschedule (email day 90 + transcript day 92) ──
    day_90 = start + timedelta(days=90)
    day_92 = start + timedelta(days=92)
    date_orig = (day_90 + timedelta(days=3)).strftime("%A %B %d")
    date_new  = (day_90 + timedelta(days=7)).strftime("%A %B %d")

    reschedule_body = _EMAIL_TEMPLATES["delivery_reschedule_request"].format(
        date_orig=date_orig,
        date_new=date_new,
        name=ent.display_name,
    )
    reschedule_thread = f"thread_{ent.entity_id[:12]}_reschedule"

    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_email_reschedule_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_90,
        entity_id=ent.entity_id,
        subject=f"Need to reschedule our delivery — {ent.company}",
        body_text=reschedule_body,
        metadata={"from": ent.email, "to": OWNER_EMAIL, "thread_id": reschedule_thread},
        tags=[f"src:gmail_owner", "kind:email.received",
              f"ent:{ent.entity_id}", f"thread:{reschedule_thread}",
              "dc:gmail_restricted", _ym(day_90)],
        thread_id=reschedule_thread,
    ))

    # P1 confirming voice transcript
    transcript_body = _TRANSCRIPT_TEMPLATES["delivery_change"].format(
        owner_name="Owner",
        date_orig=date_orig,
        date_new=date_new,
        order_id=f"ORD-DELT-001",
        phone=ent.phone,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_transcript_reschedule_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_92,
        entity_id=ent.entity_id,
        subject=f"Call: delivery reschedule — {ent.company}",
        body_text=transcript_body,
        metadata={"phone": ent.phone, "duration_s": 180},
        tags=[f"src:voice_fixture", "kind:call.transcript",
              f"ent:{ent.entity_id}", "dc:voice", _ym(day_92)],
    ))

    # Meeting reschedule voice transcript (separate from P1)
    day_93 = day_90 + timedelta(days=3)
    meeting_transcript_body = _TRANSCRIPT_TEMPLATES["meeting_change"].format(
        owner_name="Owner",
        date_orig=date_orig,
        date_new=date_new,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_transcript_meeting_reschedule_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_93,
        entity_id=ent.entity_id,
        subject=f"Call: meeting reschedule — {ent.company}",
        body_text=meeting_transcript_body,
        metadata={"phone": ent.phone, "duration_s": 150},
        tags=[f"src:voice_fixture", "kind:call.transcript",
              f"ent:{ent.entity_id}", "dc:voice", _ym(day_93)],
    ))

    # Confirm-original email (creates P4-style contradiction)
    day_91 = start + timedelta(days=91)
    confirm_original_body = (
        f"Hi {ent.display_name}, just to confirm, our meeting is still on "
        f"for {date_orig} as originally scheduled. See you then. - Owner"
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_email_confirm_orig_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.sent",
        occurred_at=day_91,
        entity_id=ent.entity_id,
        subject=f"RE: Delivery for {date_orig} — {ent.company}",
        body_text=confirm_original_body,
        metadata={"from": OWNER_EMAIL, "to": ent.email, "thread_id": reschedule_thread},
        tags=[f"src:gmail_owner", "kind:email.sent",
              f"ent:{ent.entity_id}", f"thread:{reschedule_thread}",
              "dc:gmail_restricted", _ym(day_91)],
        thread_id=reschedule_thread,
    ))

    # ── P2 scenario: invoice dispute ─────────────────────────────────────────
    day_45 = start + timedelta(days=45)
    invoice_id    = DELTA_INVOICE_ID
    amount_agreed  = round(rng.uniform(1500, 8000), 2)
    amount_charged = round(amount_agreed * 1.1, 2)   # 10% overcharge
    dispute_thread = f"thread_{ent.entity_id[:12]}_dispute"

    dispute_body = _EMAIL_TEMPLATES["invoice_dispute"].format(
        invoice_id=invoice_id,
        amount_charged=amount_charged,
        amount_agreed=amount_agreed,
        name=ent.display_name,
        company=ent.company,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_email_dispute_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_45,
        entity_id=ent.entity_id,
        subject=f"RE: Invoice {invoice_id} — Billing Discrepancy",
        body_text=dispute_body,
        metadata={"from": ent.email, "to": OWNER_EMAIL,
                  "thread_id": dispute_thread, "invoice_id": invoice_id},
        tags=[f"src:gmail_owner", "kind:email.received",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}",
              f"thread:{dispute_thread}", "dc:gmail_restricted", _ym(day_45)],
        thread_id=dispute_thread,
    ))

    # Stripe invoice record (structured)
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_stripe_invoice_{ent.entity_id}"),
        source="stripe_fixture",
        kind="invoice.created",
        occurred_at=day_45 - timedelta(days=3),
        entity_id=ent.entity_id,
        subject=f"Stripe invoice {invoice_id}",
        body_text=f"Invoice {invoice_id} created for {ent.company}. Amount: ${amount_charged:.2f}",
        metadata={
            "invoice_id": invoice_id,
            "customer_id": ent.stripe_customer_id,
            "amount_due": amount_charged,
            "currency": "usd",
            "status": "open",
        },
        tags=[f"src:stripe_fixture", "kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:payments",
              _ym(day_45 - timedelta(days=3))],
        is_llm_eligible=False,
    ))

    # QBO invoice record (structured)
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_qbo_invoice_{ent.entity_id}"),
        source="qbo_fixture",
        kind="invoice.created",
        occurred_at=day_45 - timedelta(days=3),
        entity_id=ent.entity_id,
        subject=f"QBO invoice {invoice_id}",
        body_text=f"QBO Invoice {invoice_id} for {ent.company}. Balance: ${amount_charged:.2f}",
        metadata={
            "invoice_id": invoice_id,
            "customer_id": ent.qbo_customer_id,
            "balance": amount_charged,
            "agreed_amount": amount_agreed,
            "status": "open",
        },
        tags=[f"src:qbo_fixture", "kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:accounting",
              _ym(day_45 - timedelta(days=3))],
        is_llm_eligible=False,
    ))

    # P2 dispute transcript
    day_47 = start + timedelta(days=47)
    dispute_transcript = _TRANSCRIPT_TEMPLATES["payment_dispute"].format(
        owner_name="Owner",
        name=ent.display_name,
        company=ent.company,
        invoice_id=invoice_id,
        amount_charged=amount_charged,
        amount_agreed=amount_agreed,
        email=ent.email,
        resolution_date=(day_47 + timedelta(days=2)).strftime("%B %d"),
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_transcript_dispute_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_47,
        entity_id=ent.entity_id,
        subject=f"Call: invoice dispute {invoice_id}",
        body_text=dispute_transcript,
        metadata={"phone": ent.phone, "invoice_id": invoice_id},
        tags=[f"src:voice_fixture", "kind:call.transcript",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:voice",
              _ym(day_47)],
    ))

    # ── Additional invoices: payment confirmation (INV-DELT-2026-002) ────────
    day_60 = start + timedelta(days=60)
    pay_ref = f"PAY-DELT-{rng.randint(100000, 999999)}"
    pay_amount = round(rng.uniform(2000, 9000), 2)
    pay_body = _EMAIL_TEMPLATES["payment_confirmation"].format(
        invoice_id=DELTA_INVOICE_ID_2,
        amount=pay_amount,
        payment_ref=pay_ref,
        name=ent.display_name,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_email_payment_confirm_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_60,
        entity_id=ent.entity_id,
        subject=f"Payment Confirmed — Invoice {DELTA_INVOICE_ID_2}",
        body_text=pay_body,
        metadata={"from": ent.email, "to": OWNER_EMAIL,
                  "invoice_id": DELTA_INVOICE_ID_2, "payment_ref": pay_ref},
        tags=[f"src:gmail_owner", "kind:email.received",
              f"ent:{ent.entity_id}", f"invoice:{DELTA_INVOICE_ID_2}",
              "dc:gmail_restricted", _ym(day_60)],
    ))

    # Stripe record for INV-DELT-2026-002
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_stripe_invoice2_{ent.entity_id}"),
        source="stripe_fixture",
        kind="invoice.paid",
        occurred_at=day_60,
        entity_id=ent.entity_id,
        subject=f"Stripe invoice {DELTA_INVOICE_ID_2} paid",
        body_text=(f"Invoice {DELTA_INVOICE_ID_2} paid by {ent.company}. "
                   f"Amount: ${pay_amount:.2f}. Payment ref: {pay_ref}."),
        metadata={
            "invoice_id": DELTA_INVOICE_ID_2,
            "customer_id": ent.stripe_customer_id,
            "amount_paid": pay_amount,
            "payment_ref": pay_ref,
            "currency": "usd",
            "status": "paid",
        },
        tags=[f"src:stripe_fixture", "kind:invoice.paid",
              f"ent:{ent.entity_id}", f"invoice:{DELTA_INVOICE_ID_2}", "dc:payments",
              _ym(day_60)],
        is_llm_eligible=False,
    ))

    # QBO record for INV-DELT-2026-003 (quarterly reconciliation)
    day_75 = start + timedelta(days=75)
    reconcile_amount = round(rng.uniform(3000, 12000), 2)
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_qbo_invoice3_{ent.entity_id}"),
        source="qbo_fixture",
        kind="invoice.created",
        occurred_at=day_75,
        entity_id=ent.entity_id,
        subject=f"QBO invoice {DELTA_INVOICE_ID_3}",
        body_text=(f"QBO Invoice {DELTA_INVOICE_ID_3} for {ent.company}. "
                   f"Quarterly reconciliation. Balance: ${reconcile_amount:.2f}"),
        metadata={
            "invoice_id": DELTA_INVOICE_ID_3,
            "customer_id": ent.qbo_customer_id,
            "balance": reconcile_amount,
            "status": "open",
            "note": "quarterly reconciliation",
        },
        tags=[f"src:qbo_fixture", "kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{DELTA_INVOICE_ID_3}", "dc:accounting",
              _ym(day_75)],
        is_llm_eligible=False,
    ))

    # ── P4-style: meeting date correction pair ────────────────────────────────
    day_p4_1_orig = start + timedelta(days=122)
    day_p4_1_corr = start + timedelta(days=125)
    _p4_date_orig = (start + timedelta(days=126)).strftime("%A %B %d")
    _p4_date_corr = (start + timedelta(days=130)).strftime("%A %B %d")

    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_p4_meeting_orig_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_p4_1_orig,
        entity_id=ent.entity_id,
        subject=f"Meeting confirmed for {_p4_date_orig} — {ent.company}",
        body_text=(
            f"Hi, just confirming our meeting is scheduled for {_p4_date_orig}. "
            f"Please add it to your calendar. Looking forward to it. {ent.display_name}"
        ),
        metadata={"from": ent.email, "to": OWNER_EMAIL},
        tags=[f"src:gmail_owner", "kind:email.received",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_1_orig)],
    ))

    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_p4_meeting_corr_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_p4_1_corr,
        entity_id=ent.entity_id,
        subject=f"CORRECTION: Meeting rescheduled to {_p4_date_corr} — {ent.company}",
        body_text=(
            f"Hi, apologies — the meeting date I sent earlier ({_p4_date_orig}) "
            f"was incorrect. The confirmed date is {_p4_date_corr}. "
            f"Please update your calendar. Sorry for the confusion. {ent.display_name}"
        ),
        metadata={"from": ent.email, "to": OWNER_EMAIL},
        tags=[f"src:gmail_owner", "kind:email.received",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_1_corr)],
    ))

    # ── P4-style: delivery timeline correction pair ───────────────────────────
    day_p4_2_orig = start + timedelta(days=130)
    day_p4_2_corr = start + timedelta(days=133)
    _p4_del_orig = (start + timedelta(days=140)).strftime("%B %d, %Y")
    _p4_del_corr = (start + timedelta(days=148)).strftime("%B %d, %Y")

    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_p4_delivery_orig_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.sent",
        occurred_at=day_p4_2_orig,
        entity_id=ent.entity_id,
        subject=f"Delivery timeline confirmed for {ent.company}",
        body_text=(
            f"Dear {ent.display_name}, I can confirm your order will be "
            f"delivered by {_p4_del_orig}. This is our committed timeline. — Owner"
        ),
        metadata={"from": OWNER_EMAIL, "to": ent.email},
        tags=[f"src:gmail_owner", "kind:email.sent",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_2_orig)],
    ))

    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_p4_delivery_corr_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.sent",
        occurred_at=day_p4_2_corr,
        entity_id=ent.entity_id,
        subject=f"REVISED delivery date for {ent.company}",
        body_text=(
            f"Dear {ent.display_name}, I need to correct my previous email: "
            f"the delivery date of {_p4_del_orig} is no longer achievable. "
            f"The revised confirmed delivery date is {_p4_del_corr}. "
            f"I apologise for the change. — Owner"
        ),
        metadata={"from": OWNER_EMAIL, "to": ent.email},
        tags=[f"src:gmail_owner", "kind:email.sent",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_2_corr)],
    ))

    # ── Voice QBO query call (matches billing disambiguation questions) ────────
    day_80 = start + timedelta(days=80)
    voice_qbo_body = (
        f"Agent: {OWNER_EMAIL.split('@')[0].title()} speaking. "
        f"Customer: Hi, this is {ent.display_name} from {ent.company}. "
        f"I'm calling to check on our QuickBooks account balance. "
        f"Can you confirm the balance on invoice {DELTA_INVOICE_ID_3}? "
        f"Agent: I can see your account. Invoice {DELTA_INVOICE_ID_3} shows "
        f"a balance of ${reconcile_amount:.2f}. "
        f"Customer: Great, we'll process that this week. "
        f"Agent: Perfect. I've noted that. Have a great day."
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"v3_voice_qbo_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_80,
        entity_id=ent.entity_id,
        subject=f"Call: QBO balance inquiry — {ent.company}",
        body_text=voice_qbo_body,
        metadata={"phone": ent.phone, "invoice_id": DELTA_INVOICE_ID_3},
        tags=[f"src:voice_fixture", "kind:call.transcript",
              f"ent:{ent.entity_id}", f"invoice:{DELTA_INVOICE_ID_3}", "dc:voice",
              _ym(day_80)],
    ))

    # ── Calendar events: ~50 ─────────────────────────────────────────────────
    for i in range(50):
        ev_day = start + timedelta(days=rng.randint(5, total_days - 5))
        ev_duration = rng.choice([30, 60, 90])
        ev_body = (
            f"Meeting with {ent.display_name} from {ent.company}. "
            f"Agenda: quarterly review and next steps. "
            f"Duration: {ev_duration} minutes."
        )
        cal_id = f"cal_{ent.entity_id[:8]}_{i:03d}"
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"v3_cal_{ent.entity_id}_{i}"),
            source="gcal_owner",
            kind="calendar.event.created",
            occurred_at=ev_day,
            entity_id=ent.entity_id,
            subject=f"Q-Review: {ent.company}",
            body_text=ev_body,
            metadata={
                "event_id": cal_id,
                "organizer": OWNER_EMAIL,
                "attendee": ent.email,
                "start": ev_day.isoformat(),
                "duration_minutes": ev_duration,
            },
            tags=[f"src:gcal_owner", "kind:calendar.event.created",
                  f"ent:{ent.entity_id}", "dc:calendar_restricted", _ym(ev_day)],
            is_llm_eligible=False,
        ))

    # ── Regular filler emails: ~55 ───────────────────────────────────────────
    email_count = rng.randint(55, 65)
    email_dates = sorted(
        start + timedelta(days=rng.randint(0, total_days))
        for _ in range(email_count)
    )
    for i, dt in enumerate(email_dates):
        # Skip dates close to the special scenario anchors already added
        if abs((dt - day_90).days) < 5 or abs((dt - day_45).days) < 5:
            continue
        tmpl_name = rng.choice(list(_EMAIL_TEMPLATES.keys()))
        tmpl = _EMAIL_TEMPLATES[tmpl_name]
        try:
            body = tmpl.format(
                name=ent.display_name,
                company=ent.company,
                phone=ent.phone,
                date_orig=(dt + timedelta(days=rng.randint(2, 7))).strftime("%B %d"),
                date_new=(dt + timedelta(days=rng.randint(8, 14))).strftime("%B %d"),
                date=(dt + timedelta(days=rng.randint(2, 5))).strftime("%B %d"),
                invoice_id=f"INV-FILL-{i:04d}",
                amount_charged=round(rng.uniform(500, 5000), 2),
                amount_agreed=round(rng.uniform(500, 4500), 2),
                amount=round(rng.uniform(500, 5000), 2),
                payment_ref=f"PAY-FILL-{i:06d}",
                product=rng.choice(["software license", "consulting hours", "support package"]),
                weekday=rng.choice(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]),
                service_type=rng.choice(["support response", "delivery", "onboarding"]),
                issue=rng.choice(["delayed", "incomplete", "unsatisfactory"]),
                action=rng.choice(["this is prioritized", "you will be refunded", "this is escalated"]),
                owner_name="Owner",
            )
        except KeyError:
            body = f"Hi {ent.display_name}, please see my previous message. Owner"
        thread = f"thread_{ent.entity_id[:8]}_{i:03d}"
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"v3_email_{ent.entity_id}_{i}"),
            source="gmail_owner",
            kind=rng.choice(["email.received", "email.sent"]),
            occurred_at=dt,
            entity_id=ent.entity_id,
            subject=(
                f"{rng.choice(['RE:', 'FWD:', ''])} "
                f"{tmpl_name.replace('_', ' ').title()} — {ent.company}"
            ),
            body_text=body,
            metadata={"from": ent.email, "to": OWNER_EMAIL, "thread_id": thread},
            tags=[f"src:gmail_owner", "kind:email.received",
                  f"ent:{ent.entity_id}", f"thread:{thread}",
                  "dc:gmail_restricted", _ym(dt)],
            thread_id=thread,
        ))

    # ── Old-evidence scenario signals (days 185–318) ─────────────────────────
    for idx, (day, seed_sfx, src, kind, subj_tmpl, body_tmpl, is_llm) in enumerate(
        _OLD_SCENARIOS_V3
    ):
        old_day = start + timedelta(days=day)
        if old_day >= end:
            continue
        old_body = body_tmpl.format(
            company=ent.company,
            name=ent.display_name,
            resolve_date=(old_day + timedelta(days=5)).strftime("%B %d, %Y"),
            amount=round(rng.uniform(500, 5000), 2),
            idx=idx,
            service_type=rng.choice(["infrastructure", "data-migration", "API integration"]),
            product=rng.choice(["annual license", "professional services bundle"]),
            spend_summary="within budget",
        )
        old_subj = subj_tmpl.format(company=ent.company, name=ent.display_name)
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"{seed_sfx}_{ent.entity_id}"),
            source=src,
            kind=kind,
            occurred_at=old_day,
            entity_id=ent.entity_id,
            subject=old_subj,
            body_text=old_body,
            metadata={
                "from": ent.email if "received" in kind else OWNER_EMAIL,
                "to": OWNER_EMAIL if "received" in kind else ent.email,
            },
            tags=[f"src:{src}", f"kind:{kind}", f"ent:{ent.entity_id}",
                  "dc:gmail_restricted", _ym(old_day)],
            is_llm_eligible=is_llm,
        ))


def generate_timeline_v3(
    months: int = 12,
    base_date: Optional[datetime] = None,
) -> list[SyntheticSignal]:
    """Generate ~130 synthetic signals for the Delta Finance holdout entity.

    Uses seed 0xB2E5_D3CC (distinct from main corpus seed 0xA1D3_B1BB).
    A fresh random.Random(HOLDOUT_SEED) is created on every call so the
    output is fully deterministic regardless of import order or prior calls.

    This corpus is FROZEN BEFORE RETRIEVAL.  Do not add entities or change
    signal content after the first benchmark run.

    Returns
    -------
    list[SyntheticSignal]
        Signals sorted by occurred_at.
    """
    rng   = random.Random(HOLDOUT_SEED)
    start = base_date or datetime(2026, 1, 1, tzinfo=UTC)
    end   = start + timedelta(days=months * 30)

    signals: list[SyntheticSignal] = []
    _add_delta_signals(signals, DELTA_ENTITY, start, end, rng)

    signals.sort(key=lambda s: s.occurred_at)
    return signals


# ── Corpus fingerprint ────────────────────────────────────────────────────────

def corpus_fingerprint_v3(signals: list[SyntheticSignal]) -> str:
    """Stable SHA-256 fingerprint of the v3 holdout signal corpus.

    Hashes a canonical, sorted representation of every signal's id, kind,
    entity_id, and body-text sha256.  Human-readable fields (subjects, emails)
    are excluded so the fingerprint is stable across minor prose tweaks that
    don't change signal identity.

    Store this alongside benchmark results.  Refuse to benchmark when the
    fingerprint mismatches the recorded value.
    """
    canon = json.dumps(
        sorted(
            (
                {
                    "id": s.signal_id,
                    "kind": s.kind,
                    "entity_id": s.entity_id or "",
                    "content_sha256": hashlib.sha256(
                        (s.body_text or "").encode()
                    ).hexdigest(),
                }
                for s in signals
            ),
            key=lambda d: d["id"],
        ),
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(canon.encode()).hexdigest()
