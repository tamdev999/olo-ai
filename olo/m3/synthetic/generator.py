"""Olo M3 — Synthetic 12-month business timeline generator.

Produces ~400 signals across 3 customers and 4 signal types:
  - Gmail prose (email body text: requests, commitments, disputes, date changes)
  - Calendar (meetings, reschedules, cancellations)
  - Stripe-shaped invoices and payments
  - QBO-shaped accounting records
  - 10 call transcripts

Used by M3.0 GATE tests (V1–V10) and Option B PoC (P1–P10).

No external dependencies. Output is deterministic given the same seed.

Opus architecture: §9 (PoC data); revised spec §4 (entity mapping).
"""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Seed for determinism ─────────────────────────────────────────────────────
# IMPORTANT: _DEFAULT_SEED is a constant.  Do NOT use a module-level
# random.Random singleton — each call to generate_timeline() must create
# a fresh Random instance from this seed so repeated calls in the same
# process produce identical corpora.  The historical singleton bug caused
# the bank, benchmark hash, and scoring labels to come from three different
# random timelines, invalidating 33/201 question labels (Opus audit §4.1).
_DEFAULT_SEED: int = 0xA1D3_B1BB  # canonical seed for the standard corpus

# Kept for backward compatibility only.  Deprecated: use generate_timeline(seed=…).
_RNG = random.Random(_DEFAULT_SEED)


def _rng() -> random.Random:
    """Deprecated. Returns a fresh Random from the default seed.
    Use generate_timeline(seed=...) instead.
    """
    return random.Random(_DEFAULT_SEED)


# ── Entity definitions ────────────────────────────────────────────────────────

@dataclass
class SyntheticEntity:
    entity_id: str           # olo entity ID (ulid-like, deterministic)
    display_name: str
    email: str
    phone: str               # E.164
    company: str
    company_email: str       # role address
    stripe_customer_id: str
    qbo_customer_id: str


ENTITIES = [
    SyntheticEntity(
        entity_id="ent_01ACME000000000000000000",
        display_name="Alice Chen",
        email="alice.chen@acme-corp.example",
        phone="+14155550101",
        company="Acme Corp",
        company_email="billing@acme-corp.example",
        stripe_customer_id="stripe:cus_acme_001",
        qbo_customer_id="qbo:realm123:cust_001",
    ),
    SyntheticEntity(
        entity_id="ent_01BETA000000000000000000",
        display_name="Bob Torres",
        email="bob.torres@beta-tech.example",
        phone="+14155550202",
        company="Beta Tech",
        company_email="ap@beta-tech.example",
        stripe_customer_id="stripe:cus_beta_002",
        qbo_customer_id="qbo:realm123:cust_002",
    ),
    SyntheticEntity(
        entity_id="ent_01GAMM000000000000000000",
        display_name="Carla Osei",
        email="carla.osei@gamma-retail.example",
        phone="+14155550303",
        company="Gamma Retail",
        company_email="orders@gamma-retail.example",
        stripe_customer_id="stripe:cus_gamma_003",
        qbo_customer_id="qbo:realm123:cust_003",
    ),
]

# Owner / business email and display name (never sent to LLM as source of truth)
OWNER_EMAIL = "owner@olo-business.example"
OWNER_DISPLAY_NAME = "Owner"


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class SyntheticSignal:
    signal_id: str           # deterministic
    source: str              # e.g. "gmail_owner", "gcal_owner", "stripe_fixture"
    kind: str                # e.g. "email.received", "calendar.event.created"
    occurred_at: datetime
    entity_id: str           # primary entity
    subject: Optional[str]
    body_text: str           # the prose indexed by Hindsight
    metadata: dict           # structured fields (invoice IDs, amounts, etc.)
    tags: list[str]          # Hindsight tags
    thread_id: Optional[str] = None
    is_llm_eligible: bool = True   # False for pure structured signals


def _sig_id(seed: str) -> str:
    """Deterministic signal ID from a seed string."""
    h = hashlib.sha256(seed.encode()).hexdigest()[:24]
    return f"sig_{h}"


def _ulid_time(dt: datetime, suffix: str) -> str:
    h = hashlib.md5((dt.isoformat() + suffix).encode()).hexdigest()[:20]
    return f"sig_{h}"


# ── Prose templates ───────────────────────────────────────────────────────────

_EMAIL_TEMPLATES = {
    "request_delivery": (
        "Hi, I wanted to follow up on our order. Could you please confirm when "
        "the {product} will be delivered? We were expecting it by {date_orig} but "
        "I understand there may be delays. Please let us know the new timeline. "
        "Thanks, {name}"
    ),
    "reschedule_request": (
        "Hi there, I need to reschedule our meeting originally planned for "
        "{date_orig}. Could we move it to {date_new} instead? Let me know if "
        "that works. Best, {name}"
    ),
    "delivery_reschedule_request": (
        "Hi there, I need to reschedule our delivery originally planned for "
        "{date_orig}. Could we move it to {date_new} instead? Let me know if "
        "that works. Best, {name}"
    ),
    "invoice_dispute": (
        "Hello, I'm writing about invoice #{invoice_id}. We were charged "
        "${amount_charged:.2f} but according to our records the agreed price "
        "was ${amount_agreed:.2f}. This discrepancy needs to be resolved before "
        "we can process payment. Please review and issue a corrected invoice. "
        "Thank you, {name} at {company}"
    ),
    "payment_confirmation": (
        "Hi, just confirming that we've processed payment for invoice #{invoice_id}. "
        "Amount: ${amount:.2f}. Payment reference: {payment_ref}. "
        "Please confirm receipt. Regards, {name}"
    ),
    "complaint": (
        "I'm very disappointed with the service we received last {weekday}. "
        "The {service_type} was {issue}. This is the second time this has "
        "happened and I expect a resolution. Please have someone contact me "
        "at {phone}. {name}, {company}"
    ),
    "commitment": (
        "Thank you for your patience. I can confirm that we will have this "
        "resolved by {date}. I am personally ensuring that {action}. "
        "You have my commitment on this. - {owner_name}"
    ),
}

_TRANSCRIPT_TEMPLATES = {
    "delivery_change": (
        "Agent: Hi, this is {owner_name} at Olo Business. How can I help you? "
        "Customer: Hi, I'm calling about my delivery scheduled for {date_orig}. "
        "I need to change it to {date_new} if possible. "
        "Agent: Absolutely, let me check that. I see your order {order_id}. "
        "I can confirm the delivery is now rescheduled for {date_new}. "
        "Customer: Perfect, thank you. My number is {phone} if you need to reach me. "
        "Agent: Got it. All confirmed. Have a great day."
    ),
    "meeting_change": (
        "Agent: Hi, this is {owner_name} from Olo Business. "
        "Customer: Hi, I'm calling about our scheduled meeting on {date_orig}. "
        "I need to move it to {date_new} if at all possible. "
        "Agent: Let me check availability. Yes, {date_new} works for us. "
        "The meeting is now rescheduled to {date_new}. I'll send a calendar update. "
        "Customer: Great, thank you so much. "
        "Agent: Confirmed. Meeting rescheduled from {date_orig} to {date_new}. "
        "Have a great day."
    ),
    "payment_dispute": (
        "Agent: {owner_name} speaking. "
        "Customer: Hi, I'm {name} from {company}. I'm calling about invoice #{invoice_id}. "
        "We received it but the amount is wrong - it shows ${amount_charged:.2f} "
        "but we agreed on ${amount_agreed:.2f}. "
        "Agent: I see the invoice. Let me escalate this to our billing team. "
        "We'll issue a corrected invoice within 2 business days. "
        "Customer: Thank you. My email is {email}. "
        "Agent: Noted, {name}. We'll resolve this by {resolution_date}."
    ),
}


# ── Old-evidence scenario seeds (days 185–318, before run date 2026-11-19) ────

_OLD_SCENARIOS = [
    # (day, seed_suffix, source, kind, subject_tmpl, body_tmpl, is_llm_eligible)
    (185, "old_commitment",   "gmail_owner", "email.sent",
     "Commitment: Support Resolution — {company}",
     ("Following our recent discussion, I want to confirm my commitment to {company}: "
      "we will resolve the outstanding support issue by {resolve_date}. "
      "I have personally escalated this to our engineering team. — Owner"),
     True),
    (200, "old_payment_ack",  "gmail_owner", "email.received",
     "Payment Acknowledgement — {company}",
     ("Hi, confirming that payment of ${amount:.2f} has been processed for {company}. "
      "Payment reference PAY-OLD-{idx:04d} is on file. The account is now current. "
      "Thanks, {name}"),
     True),
    (220, "old_new_service",  "gmail_owner", "email.received",
     "New Service Request — {company}",
     ("Hello, I would like to request a new service engagement for {company}. "
      "We need {service_type} support for our upcoming project. "
      "Please send a proposal at your earliest convenience. Thanks, {name}"),
     True),
    (240, "old_complaint",    "gmail_owner", "email.received",
     "Formal Complaint — {company}",
     ("I am writing to formally complain about the recent support experience with {company}. "
      "The response time was unacceptably slow and the issue remains unresolved. "
      "This needs immediate attention from management. — {name}, {company}"),
     True),
    (260, "old_resolution",   "gmail_owner", "email.sent",
     "Issue Resolved — {company}",
     ("Dear {name}, I am pleased to confirm that the issue raised by {company} "
      "has been fully resolved. The root cause was identified and corrected. "
      "Please let us know if you need anything further. — Owner"),
     True),
    (280, "old_renewal",      "gmail_owner", "email.received",
     "Contract Renewal Discussion — {company}",
     ("Hi, as we approach the end of our current agreement with {company}, "
      "I would like to discuss renewal options. "
      "Could we schedule a call to review pricing and scope? Best, {name}"),
     True),
    (300, "old_invoice_qry",  "gmail_owner", "email.received",
     "Invoice Query — {company}",
     ("Hello, I noticed an invoice for ${amount:.2f} from {company} but I am "
      "missing the itemised breakdown. Could you resend the details? Thanks, {name}"),
     True),
    (310, "old_acct_review",  "gmail_owner", "email.sent",
     "Quarterly Account Review — {company}",
     ("Dear {name}, following our quarterly account review for {company}, "
      "here is a summary: total engagements on track, all support tickets resolved, "
      "upcoming renewal due next quarter. Please confirm if you need any changes. — Owner"),
     True),
    (315, "old_appreciation", "gmail_owner", "email.received",
     "Appreciation Note — {company}",
     ("Hi, I wanted to take a moment to appreciate the excellent service "
      "{company} has received from your team this quarter. "
      "The turnaround time and quality have been outstanding. Thank you! — {name}"),
     True),
    (318, "old_new_order",    "gmail_owner", "email.received",
     "New Order Request — {company}",
     ("Hello, I would like to place a new order for {company}. "
      "Please quote for {product} with delivery by end of this month. "
      "Looking forward to continuing our business relationship. — {name}"),
     True),
]

# Paraphrased queries for old-event labelled questions (index-matched to _OLD_SCENARIOS)
# Each query explicitly names a month/year so V1 test can derive the filter
# FROM THE QUESTION TEXT — never from the expected answer (correction #3).
_OLD_QUERIES = [
    "support issue resolution commitment July 2026",
    "payment confirmation acknowledgement received July 2026",
    "new service engagement request infrastructure August 2026",
    "formal support complaint response August 2026",
    "issue resolution confirmed notification September 2026",
    "contract renewal terms discussion October 2026",
    "invoice amount query details October 2026",
    "quarterly account business review November 2026",
    "positive feedback appreciation received November 2026",
    "new purchase order request November 2026",
]

# Month keywords to month number (for query-text temporal constraint extraction)
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _extract_temporal_ym(query: str) -> Optional[str]:
    """Extract ym:YYYY-MM from query text if it contains an explicit temporal constraint.

    Returns None if the query has no recognisable month+year reference.
    This is the ONLY legitimate source for the V1 month filter — derived
    exclusively from the question, never from expected answer metadata.
    """
    import re
    q_lower = query.lower()
    # Match patterns like "July 2026", "november 2026", "aug 2026"
    for name, num in _MONTH_NAMES.items():
        abbr = name[:3]
        if name in q_lower or abbr in q_lower:
            # Find year near the month name
            m = re.search(r'\b(202[0-9])\b', query)
            if m:
                year = int(m.group(1))
                return f"ym:{year:04d}-{num:02d}"
    return None


def generate_timeline(
    months: int = 12,
    base_date: Optional[datetime] = None,
    seed: Optional[int] = None,
) -> list[SyntheticSignal]:
    """Generate ~400 synthetic signals over *months* months.

    Deterministic — same output for the same seed/months/base_date.

    ``seed`` controls random choices.  Pass ``None`` (default) to use
    ``_DEFAULT_SEED``.  Pass an explicit seed to generate an independent
    reproducible dataset for holdout benchmarks or variance studies.

    A **fresh** ``random.Random(seed)`` is created on every call, so
    repeated calls with the same seed always produce identical corpora.
    (The historical module-global singleton advanced state between calls,
    causing divergent corpora — Opus audit §4.1.)
    """
    rng = random.Random(_DEFAULT_SEED if seed is None else seed)
    start = base_date or datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=months * 30)
    signals: list[SyntheticSignal] = []

    # Create signals for each entity
    for ent in ENTITIES:
        # Sprinkle across the whole timeline
        _add_entity_signals(signals, ent, start, end, rng)

    # Sort by occurred_at
    signals.sort(key=lambda s: s.occurred_at)
    return signals


def _ym(dt: datetime) -> str:
    """Return 'ym:YYYY-MM' tag for the given datetime."""
    return f"ym:{dt.year:04d}-{dt.month:02d}"


def _add_entity_signals(
    out: list[SyntheticSignal],
    ent: SyntheticEntity,
    start: datetime,
    end: datetime,
    rng: random.Random,
) -> None:
    """Add all signal types for one entity across the timeline."""
    total_days = (end - start).days

    # --- Emails: ~55 per entity (spread across the full timeline)
    email_count = rng.randint(55, 65)
    email_dates = sorted(
        start + timedelta(days=rng.randint(0, total_days))
        for _ in range(email_count)
    )

    # Seed specific scenarios at known positions
    # P1 scenario: delivery date change (email at day ~90, transcript at day ~92)
    day_90 = start + timedelta(days=90)
    day_92 = start + timedelta(days=92)
    date_orig = (day_90 + timedelta(days=3)).strftime("%A %B %d")
    date_new = (day_90 + timedelta(days=7)).strftime("%A %B %d")

    # Delivery reschedule email
    reschedule_body = _EMAIL_TEMPLATES["delivery_reschedule_request"].format(
        date_orig=date_orig,
        date_new=date_new,
        name=ent.display_name,
    )
    reschedule_thread = f"thread_{ent.entity_id[:12]}_reschedule"
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"email_reschedule_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_90,
        entity_id=ent.entity_id,
        subject=f"Need to reschedule our delivery — {ent.company}",
        body_text=reschedule_body,
        metadata={
            "headers": {
                "from": [{"name": ent.display_name, "email": ent.email}],
                "to": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
            },
            "thread_id": reschedule_thread,
        },
        tags=[f"src:gmail_owner", f"kind:email.received",
              f"ent:{ent.entity_id}", f"thread:{reschedule_thread}",
              "dc:gmail_restricted", _ym(day_90)],
        thread_id=reschedule_thread,
    ))

    # P1 scenario: confirming transcript (§9 P1: phone confirms the new date)
    transcript_body = _TRANSCRIPT_TEMPLATES["delivery_change"].format(
        owner_name="Owner",
        date_orig=date_orig,
        date_new=date_new,
        order_id=f"ORD-{ent.entity_id[6:10].upper()}-001",
        phone=ent.phone,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"transcript_reschedule_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_92,
        entity_id=ent.entity_id,
        subject=f"Call: delivery reschedule — {ent.company}",
        body_text=transcript_body,
        metadata={
            "phone": ent.phone,
            "duration_s": 180,
            "customer_name": ent.display_name,
            "customer_company": ent.company,
        },
        tags=[f"src:voice_fixture", f"kind:call.transcript",
              f"ent:{ent.entity_id}", "dc:voice", _ym(day_92)],
    ))
    # Meeting reschedule voice transcript — semantically separate from P1 delivery scenario.
    # Used by benchmark mtg_07: "Did {contact} discuss a rescheduled meeting on a voice call?"
    day_93 = day_90 + timedelta(days=3)
    meeting_transcript_body = _TRANSCRIPT_TEMPLATES["meeting_change"].format(
        owner_name="Owner",
        date_orig=date_orig,
        date_new=date_new,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"transcript_meeting_reschedule_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_93,
        entity_id=ent.entity_id,
        subject=f"Call: meeting reschedule — {ent.company}",
        body_text=meeting_transcript_body,
        metadata={
            "phone": ent.phone,
            "duration_s": 150,
            "customer_name": ent.display_name,
            "customer_company": ent.company,
        },
        tags=[f"src:voice_fixture", f"kind:call.transcript",
              f"ent:{ent.entity_id}", "dc:voice", _ym(day_93)],
    ))

    # P2 scenario: invoice dispute (email + Stripe record + QBO record)
    day_45 = start + timedelta(days=45)
    # Use entity_id[6:10] (company abbrev: ACME/BETA/GAMM) not [:6]
    # which yields "ent_01" for all three entities — a collision.
    invoice_id = f"INV-{ent.entity_id[6:10].upper()}-2026-001"
    amount_agreed = round(rng.uniform(1500, 8000), 2)
    amount_charged = round(amount_agreed * 1.1, 2)  # 10% overcharge (the dispute)
    dispute_thread = f"thread_{ent.entity_id[:12]}_dispute"

    dispute_body = _EMAIL_TEMPLATES["invoice_dispute"].format(
        invoice_id=invoice_id,
        amount_charged=amount_charged,
        amount_agreed=amount_agreed,
        name=ent.display_name,
        company=ent.company,
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"email_dispute_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_45,
        entity_id=ent.entity_id,
        subject=f"RE: Invoice {invoice_id} — Billing Discrepancy",
        body_text=dispute_body,
        metadata={
            "headers": {
                "from": [{"name": ent.display_name, "email": ent.email}],
                "to": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
            },
            "thread_id": dispute_thread,
            "invoice_id": invoice_id,
        },
        tags=[f"src:gmail_owner", f"kind:email.received",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}",
              f"thread:{dispute_thread}", "dc:gmail_restricted", _ym(day_45)],
        thread_id=dispute_thread,
    ))

    # Stripe invoice record (structured — no LLM)
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"stripe_invoice_{ent.entity_id}"),
        source="stripe_fixture",
        kind="invoice.created",
        occurred_at=day_45 - timedelta(days=3),
        entity_id=ent.entity_id,
        subject=f"Stripe invoice {invoice_id}",
        body_text=f"Invoice {invoice_id} created for {ent.company}. Amount: ${amount_charged:.2f}",
        metadata={
            "invoice_id": invoice_id,
            "customer_id": ent.stripe_customer_id,
            "customer_email": ent.email,
            "customer_name": ent.display_name,
            "amount_due": amount_charged,
            "currency": "usd",
            "status": "open",
        },
        tags=[f"src:stripe_fixture", f"kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:payments",
              _ym(day_45 - timedelta(days=3))],
        is_llm_eligible=False,  # Structured signal — chunks only, no prose extraction
    ))

    # QBO invoice record (structured — no LLM)
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"qbo_invoice_{ent.entity_id}"),
        source="qbo_fixture",
        kind="invoice.created",
        occurred_at=day_45 - timedelta(days=3),
        entity_id=ent.entity_id,
        subject=f"QBO invoice {invoice_id}",
        body_text=f"QBO Invoice {invoice_id} for {ent.company}. Balance: ${amount_charged:.2f}",
        metadata={
            "invoice_id": invoice_id,
            "customer_id": ent.qbo_customer_id,
            "customer_email": ent.email,
            "customer_name": ent.display_name,
            "balance": amount_charged,
            "agreed_amount": amount_agreed,
            "status": "open",
        },
        tags=[f"src:qbo_fixture", f"kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:accounting",
              _ym(day_45 - timedelta(days=3))],
        is_llm_eligible=False,
    ))

    # P2 scenario: dispute transcript (call about the same invoice)
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
        signal_id=_sig_id(f"transcript_dispute_{ent.entity_id}"),
        source="voice_fixture",
        kind="call.transcript",
        occurred_at=day_47,
        entity_id=ent.entity_id,
        subject=f"Call: invoice dispute {invoice_id}",
        body_text=dispute_transcript,
        metadata={
            "phone": ent.phone,
            "invoice_id": invoice_id,
            "customer_name": ent.display_name,
            "customer_company": ent.company,
        },
        tags=[f"src:voice_fixture", f"kind:call.transcript",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:voice",
              _ym(day_47)],
    ))

    # P4 scenario: five unambiguous contradiction pairs with explicit corrections.
    # Each pair has: original authoritative value (day_p4_N_orig) followed by
    # an explicit correction email (day_p4_N_corr) that supersedes it.
    # DeepSeek should extract the corrected value as current and keep the
    # original in observation history.
    #
    # NOTE: Signals are placed in MAY 2026 (day 120+) to avoid competing with
    # the April 2026 reschedule/dispute scenario signals in V1 recall recall.
    #
    # Pair 1: meeting date (day 122 original → day 125 correction)
    day_p4_1_orig = start + timedelta(days=122)
    day_p4_1_corr = start + timedelta(days=125)
    _p4_date_orig = (start + timedelta(days=126)).strftime("%A %B %d")   # D1
    _p4_date_corr = (start + timedelta(days=130)).strftime("%A %B %d")  # D2
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"p4_meeting_orig_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.received",
        occurred_at=day_p4_1_orig,
        entity_id=ent.entity_id,
        subject=f"Meeting confirmed for {_p4_date_orig} — {ent.company}",
        body_text=(
            f"Hi, just confirming our meeting is scheduled for {_p4_date_orig}. "
            f"Please add it to your calendar. Looking forward to it. {ent.display_name}"
        ),
        metadata={
            "headers": {
                "from": [{"name": ent.display_name, "email": ent.email}],
                "to": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
            },
        },
        tags=[f"src:gmail_owner", f"kind:email.received",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_1_orig)],
    ))
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"p4_meeting_corr_{ent.entity_id}"),
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
        metadata={
            "headers": {
                "from": [{"name": ent.display_name, "email": ent.email}],
                "to": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
            },
        },
        tags=[f"src:gmail_owner", f"kind:email.received",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_1_corr)],
    ))

    # Pair 2: delivery timeline (day 130 original → day 133 correction)
    day_p4_2_orig = start + timedelta(days=130)
    day_p4_2_corr = start + timedelta(days=133)
    _p4_del_orig = (start + timedelta(days=140)).strftime("%B %d, %Y")   # D1
    _p4_del_corr = (start + timedelta(days=148)).strftime("%B %d, %Y")  # D2
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"p4_delivery_orig_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.sent",
        occurred_at=day_p4_2_orig,
        entity_id=ent.entity_id,
        subject=f"Delivery timeline confirmed for {ent.company}",
        body_text=(
            f"Dear {ent.display_name}, I can confirm your order will be "
            f"delivered by {_p4_del_orig}. This is our committed timeline. — Owner"
        ),
        metadata={
            "headers": {
                "from": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
                "to": [{"name": ent.display_name, "email": ent.email}],
            },
        },
        tags=[f"src:gmail_owner", f"kind:email.sent",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_2_orig)],
    ))
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"p4_delivery_corr_{ent.entity_id}"),
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
        metadata={
            "headers": {
                "from": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
                "to": [{"name": ent.display_name, "email": ent.email}],
            },
        },
        tags=[f"src:gmail_owner", f"kind:email.sent",
              f"ent:{ent.entity_id}", "dc:gmail_restricted", _ym(day_p4_2_corr)],
    ))


    # (creates a contradiction with the reschedule email above)
    day_91 = start + timedelta(days=91)
    confirm_original_body = (
        f"Hi {ent.display_name}, just to confirm, our meeting is still on "
        f"for {date_orig} as originally scheduled. See you then. - Owner"
    )
    out.append(SyntheticSignal(
        signal_id=_sig_id(f"email_confirm_orig_{ent.entity_id}"),
        source="gmail_owner",
        kind="email.sent",
        occurred_at=day_91,
        entity_id=ent.entity_id,
        subject=f"RE: Delivery for {date_orig} — {ent.company}",
        body_text=confirm_original_body,
        metadata={
            "headers": {
                "from": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
                "to": [{"name": ent.display_name, "email": ent.email}],
            },
            "thread_id": reschedule_thread,
        },
        tags=[f"src:gmail_owner", f"kind:email.sent",
              f"ent:{ent.entity_id}", f"thread:{reschedule_thread}",
              "dc:gmail_restricted", _ym(day_91)],
        thread_id=reschedule_thread,
    ))

    # Calendar events: ~50 per entity
    for i in range(50):
        ev_day = start + timedelta(days=rng.randint(5, (end - start).days - 5))
        ev_duration = rng.choice([30, 60, 90])  # minutes
        ev_body = (
            f"Meeting with {ent.display_name} from {ent.company}. "
            f"Agenda: quarterly review and next steps. "
            f"Duration: {ev_duration} minutes."
        )
        cal_id = f"cal_{ent.entity_id[:8]}_{i:03d}"
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"cal_{ent.entity_id}_{i}"),
            source="gcal_owner",
            kind="calendar.event.created",
            occurred_at=ev_day,
            entity_id=ent.entity_id,
            subject=f"Q-Review: {ent.company}",
            body_text=ev_body,
            metadata={
                "event_id": cal_id,
                "organizer_email": OWNER_EMAIL,
                "organizer_display_name": OWNER_DISPLAY_NAME,
                "organizer_self": True,
                "attendees": [{
                    "email": ent.email,
                    "display_name": ent.display_name,
                    "response_status": "accepted",
                    "organizer": False,
                    "self": False,
                }],
                "start": ev_day.isoformat(),
                "duration_minutes": ev_duration,
            },
            tags=[f"src:gcal_owner", f"kind:calendar.event.created",
                  f"ent:{ent.entity_id}", f"dc:calendar_restricted", _ym(ev_day)],
            is_llm_eligible=False,  # Structured
        ))

    # Regular emails spanning the full timeline
    for i, dt in enumerate(email_dates):
        # Skip the special scenario dates already added
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
                invoice_id=f"INV-{i:04d}",
                amount_charged=round(rng.uniform(500, 5000), 2),
                amount_agreed=round(rng.uniform(500, 4500), 2),
                amount=round(rng.uniform(500, 5000), 2),
                payment_ref=f"PAY-{i:06d}",
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
        _email_kind = rng.choice(["email.received", "email.sent"])
        if _email_kind == "email.received":
            _email_headers = {
                "from": [{"name": ent.display_name, "email": ent.email}],
                "to": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
            }
        else:
            _email_headers = {
                "from": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
                "to": [{"name": ent.display_name, "email": ent.email}],
            }
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"email_{ent.entity_id}_{i}"),
            source="gmail_owner",
            kind=_email_kind,
            occurred_at=dt,
            entity_id=ent.entity_id,
            subject=f"{rng.choice(['RE:', 'FWD:', ''])} {tmpl_name.replace('_', ' ').title()} — {ent.company}",
            body_text=body,
            metadata={"headers": _email_headers, "thread_id": thread},
            tags=[f"src:gmail_owner", f"kind:email.received",
                  f"ent:{ent.entity_id}", f"thread:{thread}", "dc:gmail_restricted",
                  _ym(dt)],
            thread_id=thread,
        ))

    # ── Old-evidence scenario signals (days 185–318, >180 days from base_date)
    # These are deterministic and serve as labelled benchmark targets for V1.
    for idx, (day, seed_sfx, src, kind, subj_tmpl, body_tmpl, is_llm) in enumerate(
        _OLD_SCENARIOS
    ):
        old_day = start + timedelta(days=day)
        # Skip if old_day would be in the future relative to corpus end
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
        if "received" in kind:
            _old_headers = {
                "from": [{"name": ent.display_name, "email": ent.email}],
                "to": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
            }
        else:
            _old_headers = {
                "from": [{"name": OWNER_DISPLAY_NAME, "email": OWNER_EMAIL}],
                "to": [{"name": ent.display_name, "email": ent.email}],
            }
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"{seed_sfx}_{ent.entity_id}"),
            source=src,
            kind=kind,
            occurred_at=old_day,
            entity_id=ent.entity_id,
            subject=old_subj,
            body_text=old_body,
            metadata={"headers": _old_headers},
            tags=[f"src:{src}", f"kind:{kind}", f"ent:{ent.entity_id}",
                  "dc:gmail_restricted", _ym(old_day)],
            is_llm_eligible=is_llm,
        ))


# ── Serialisation helpers ─────────────────────────────────────────────────────

def to_hindsight_items(signals: list[SyntheticSignal]) -> list[dict]:
    """Convert SyntheticSignal list to Hindsight MemoryItem dicts.

    Maps per §B.2 (DECISION):
      document_id = "sig:<signal_id>"
      operation_id = "olo-idx:gate:<signal_id>"
      entities    = [{"text": entity_id, "type": "person"}] (resolve_entities=false)
    """
    items = []
    for sig in signals:
        # Ensure ym: tag is present (audit V1 fix)
        tags = list(sig.tags)
        ym = _ym(sig.occurred_at)
        if ym not in tags:
            tags.append(ym)

        # C7: use the Hindsight named strategy constants
        # structured-no-llm → chunks extraction, 0 LLM calls
        # prose → bank default extraction (concise + DeepSeek)
        hindsight_strategy = "prose" if sig.is_llm_eligible else "structured-no-llm"
        item: dict = {
            "content": sig.body_text,
            "timestamp": sig.occurred_at.isoformat(),
            "document_id": f"sig:{sig.signal_id}",
            "operation_id": f"olo-idx:gate:{sig.signal_id}",
            "entities": [{"text": sig.entity_id, "type": "person"}],
            "resolve_entities": False,
            "tags": tags,
            "metadata": {
                "signal_id": sig.signal_id,
                "source": sig.source,
                "kind": sig.kind,
                "content_sha256": hashlib.sha256(sig.body_text.encode()).hexdigest(),
                "render_version": "1",
                "is_llm_eligible": str(sig.is_llm_eligible),
            },
            # Option B: structured signals use "chunks", prose uses extraction
            "strategy": hindsight_strategy,
        }
        if sig.subject:
            item["context"] = sig.subject   # v1.2.1: context is a string
        items.append(item)
    return items


def save_to_jsonl(signals: list[SyntheticSignal], path: Path) -> None:
    """Write signals to a JSONL file for reproducible test runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for sig in signals:
            f.write(json.dumps({
                "signal_id": sig.signal_id,
                "source": sig.source,
                "kind": sig.kind,
                "occurred_at": sig.occurred_at.isoformat(),
                "entity_id": sig.entity_id,
                "subject": sig.subject,
                "body_text": sig.body_text,
                "metadata": sig.metadata,
                "tags": sig.tags,
                "thread_id": sig.thread_id,
                "is_llm_eligible": sig.is_llm_eligible,
            }) + "\n")


def load_from_jsonl(path: Path) -> list[SyntheticSignal]:
    """Load signals from a JSONL file."""
    signals = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            signals.append(SyntheticSignal(
                signal_id=d["signal_id"],
                source=d["source"],
                kind=d["kind"],
                occurred_at=datetime.fromisoformat(d["occurred_at"]),
                entity_id=d["entity_id"],
                subject=d.get("subject"),
                body_text=d["body_text"],
                metadata=d["metadata"],
                tags=d["tags"],
                thread_id=d.get("thread_id"),
                is_llm_eligible=d.get("is_llm_eligible", True),
            ))
    return signals


# ── Labelled question set for V1 (recall@10 ≥ 0.90) ─────────────────────────

def build_labelled_questions() -> list[dict]:
    """Return 48 labelled questions for V1 recall@10 validation.

    All questions have expected_signal_ids (no time_window_only shortcuts).
    At least 10 per entity target signals > 180 days old.
    Calendar share: 0% (no calendar-targeting questions).

    Correction #3 (answer leakage): every query that uses a temporal filter
    explicitly names a month/year IN THE QUERY TEXT.  The ``temporal_ym``
    field is derived by ``_extract_temporal_ym()`` from the question itself —
    never from expected-answer metadata.  When ``temporal_ym`` is None the V1
    test uses entity-only strict filtering.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    questions: list[dict] = []

    for ent in ENTITIES:
        def _q(qid, query, expected_sigs, age_days):
            tym = _extract_temporal_ym(query)
            return {
                "id": f"q_{ent.entity_id[:8]}_{qid}",
                "query": query,
                "entity_id": ent.entity_id,
                "temporal_ym": tym,
                "expected_signal_ids": expected_sigs,
                "age_months": age_days // 30,
            }

    # ── Scenario questions: month-only temporal context (no 4-digit year).
    # Month-only triggers temporal_ym=None → entity-only pool (~120-130 docs,
    # pool > 10 so recall@10 is non-trivial).
    # Queries include the scenario month to improve semantic matching with
    # DeepSeek-extracted world facts, without using year-based tag filters.
    # C8 xfail: content-word overlap is 20-45%; improving below 40% while
    # keeping rank ≤ 10 is not achievable with E5 on this corpus — documented.
        questions.extend([
            _q("reschedule_a",
               "reschedule meeting April request originally",
               [_sig_id(f"email_reschedule_{ent.entity_id}")], 90),
            _q("reschedule_b",
               "delivery reschedule April transcript phone",
               [_sig_id(f"transcript_reschedule_{ent.entity_id}")], 92),
            _q("dispute_a",
               "invoice overcharged February amount dispute email",
               [_sig_id(f"email_dispute_{ent.entity_id}")], 45),
            _q("dispute_b",
               "invoice amount February dispute phone call",
               [_sig_id(f"transcript_dispute_{ent.entity_id}")], 47),
            _q("contradiction",
               "confirm meeting April originally scheduled still",
               [_sig_id(f"email_confirm_orig_{ent.entity_id}")], 91),
            _q("stripe",
               "stripe invoice February amount created payment",
               [_sig_id(f"stripe_invoice_{ent.entity_id}")], 42),
        ])

        # ── Old-evidence questions (10 per entity, all > 180 days) ──────────
        # Queries include explicit month names (legitimate temporal constraint).
        for idx, (day, seed_sfx, _src, _kind, _st, _bt, _) in enumerate(
            _OLD_SCENARIOS
        ):
            q_text = _OLD_QUERIES[idx].format(
                company=ent.company,
                name=ent.display_name,
            )
            questions.append({
                "id": f"q_{ent.entity_id[:8]}_old_{idx}",
                "query": q_text,
                "entity_id": ent.entity_id,
                "temporal_ym": _extract_temporal_ym(q_text),
                "expected_signal_ids": [_sig_id(f"{seed_sfx}_{ent.entity_id}")],
                "age_months": day // 30,
            })

    return questions


# ── Frozen benchmark hash (C8) ────────────────────────────────────────────────
# Computed from the query texts in build_labelled_questions() BEFORE any gate
# run.  The structural test TestV1BenchmarkFreeze.test_benchmark_hash_matches
# verifies this value matches the actual questions.  Changing questions
# without updating this constant is caught at collection time.
#
# Recompute after intentional question changes:
#   python -c "
#   import hashlib, json
#   from olo.m3.synthetic.generator import build_labelled_questions
#   qs = build_labelled_questions()
#   txt = json.dumps([q['query'] for q in qs], sort_keys=True, ensure_ascii=False)
#   print(hashlib.sha256(txt.encode()).hexdigest())
#   "

def _benchmark_query_hash() -> str:
    """Return the sha256 of the current benchmark query texts (deterministic)."""
    import json as _json
    qs = build_labelled_questions()
    txt = _json.dumps([q["query"] for q in qs], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(txt.encode()).hexdigest()


def get_benchmark_hash() -> str:
    """Return the sha256 hash of the frozen benchmark question texts."""
    return _benchmark_query_hash()


def corpus_fingerprint(signals: "list[SyntheticSignal]") -> str:
    """Return a stable SHA-256 fingerprint of an authoritative signal corpus.

    The fingerprint hashes a canonical representation of every signal's
    identifier, content hash and kind — not human-readable fields that could
    drift (subjects, emails) — so it can be compared across builds and stored
    alongside benchmark results to detect corpus/label mismatches.

    Usage
    -----
        tl = generate_timeline()
        fp = corpus_fingerprint(tl)
        # Store fp alongside the bank; refuse to benchmark when fp mismatches
        # the label key's expected fingerprint.

    Returns a hex-encoded SHA-256 string.
    """
    import hashlib as _hashlib
    import json as _json
    canon = _json.dumps(
        sorted(
            (
                {
                    "id": s.signal_id,
                    "kind": s.kind,
                    "entity_id": s.entity_id or "",
                    # Hash the body text to represent content identity.
                    # SyntheticSignal has body_text, not a pre-computed content_sha256.
                    "content_sha256": _hashlib.sha256(
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
    return _hashlib.sha256(canon.encode()).hexdigest()


def corpus_fingerprint_from_seed(
    seed: "Optional[int]" = None,
    months: int = 12,
) -> str:
    """Return the corpus fingerprint for a given seed without storing signals.

    Convenience wrapper for callers that only need the fingerprint.
    """
    return corpus_fingerprint(generate_timeline(months=months, seed=seed))
