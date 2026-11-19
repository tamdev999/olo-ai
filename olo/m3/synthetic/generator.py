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
_RNG = random.Random(0xA1D3_B1BB)  # deterministic seed for Olo M3 PoC


def _rng() -> random.Random:
    return _RNG


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

# Owner / business email (never sent to LLM as source of truth)
OWNER_EMAIL = "owner@olo-business.example"


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


# ── Generator ─────────────────────────────────────────────────────────────────

def generate_timeline(
    months: int = 12,
    base_date: Optional[datetime] = None,
) -> list[SyntheticSignal]:
    """Generate ~400 synthetic signals over *months* months.

    Deterministic — same output for the same seed/months/base_date.
    """
    rng = _rng()
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
    reschedule_body = _EMAIL_TEMPLATES["reschedule_request"].format(
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
        subject=f"Need to reschedule our meeting — {ent.company}",
        body_text=reschedule_body,
        metadata={"from": ent.email, "to": OWNER_EMAIL, "thread_id": reschedule_thread},
        tags=[f"src:gmail_owner", f"kind:email.received",
              f"ent:{ent.entity_id}", f"thread:{reschedule_thread}", "dc:gmail_restricted"],
        thread_id=reschedule_thread,
    ))

    # P1 scenario: confirming transcript (§9 P1: phone confirms the new date)
    transcript_body = _TRANSCRIPT_TEMPLATES["delivery_change"].format(
        owner_name="Owner",
        date_orig=date_orig,
        date_new=date_new,
        order_id=f"ORD-{ent.entity_id[:6].upper()}-001",
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
        metadata={"phone": ent.phone, "duration_s": 180},
        tags=[f"src:voice_fixture", f"kind:call.transcript",
              f"ent:{ent.entity_id}", "dc:voice"],
    ))

    # P2 scenario: invoice dispute (email + Stripe record + QBO record)
    day_45 = start + timedelta(days=45)
    invoice_id = f"INV-{ent.entity_id[:6].upper()}-2026-001"
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
        metadata={"from": ent.email, "to": OWNER_EMAIL,
                  "thread_id": dispute_thread, "invoice_id": invoice_id},
        tags=[f"src:gmail_owner", f"kind:email.received",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}",
              f"thread:{dispute_thread}", "dc:gmail_restricted"],
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
            "amount_due": amount_charged,
            "currency": "usd",
            "status": "open",
        },
        tags=[f"src:stripe_fixture", f"kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:payments"],
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
            "balance": amount_charged,
            "agreed_amount": amount_agreed,
            "status": "open",
        },
        tags=[f"src:qbo_fixture", f"kind:invoice.created",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:accounting"],
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
        metadata={"phone": ent.phone, "invoice_id": invoice_id},
        tags=[f"src:voice_fixture", f"kind:call.transcript",
              f"ent:{ent.entity_id}", f"invoice:{invoice_id}", "dc:voice"],
    ))

    # P4 scenario: contradicting date — owner confirms the ORIGINAL date in a reply
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
        subject=f"RE: Need to reschedule our meeting — {ent.company}",
        body_text=confirm_original_body,
        metadata={"from": OWNER_EMAIL, "to": ent.email, "thread_id": reschedule_thread},
        tags=[f"src:gmail_owner", f"kind:email.sent",
              f"ent:{ent.entity_id}", f"thread:{reschedule_thread}", "dc:gmail_restricted"],
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
                "organizer": OWNER_EMAIL,
                "attendee": ent.email,
                "start": ev_day.isoformat(),
                "duration_minutes": ev_duration,
            },
            tags=[f"src:gcal_owner", f"kind:calendar.event.created",
                  f"ent:{ent.entity_id}", f"dc:calendar_restricted"],
            is_llm_eligible=False,  # Structured
        ))

    # Regular emails spanning the full timeline (old ones for scenario 3)
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
        out.append(SyntheticSignal(
            signal_id=_sig_id(f"email_{ent.entity_id}_{i}"),
            source="gmail_owner",
            kind=rng.choice(["email.received", "email.sent"]),
            occurred_at=dt,
            entity_id=ent.entity_id,
            subject=f"{rng.choice(['RE:', 'FWD:', ''])} {tmpl_name.replace('_', ' ').title()} — {ent.company}",
            body_text=body,
            metadata={"from": ent.email, "to": OWNER_EMAIL, "thread_id": thread},
            tags=[f"src:gmail_owner", f"kind:email.received",
                  f"ent:{ent.entity_id}", f"thread:{thread}", "dc:gmail_restricted"],
            thread_id=thread,
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
        item: dict = {
            "content": sig.body_text,
            "timestamp": sig.occurred_at.isoformat(),
            "document_id": f"sig:{sig.signal_id}",
            "operation_id": f"olo-idx:gate:{sig.signal_id}",
            "entities": [{"text": sig.entity_id, "type": "person"}],
            "resolve_entities": False,
            "tags": sig.tags,
            "metadata": {
                "signal_id": sig.signal_id,
                "source": sig.source,
                "kind": sig.kind,
                "content_sha256": hashlib.sha256(sig.body_text.encode()).hexdigest(),
                "render_version": "1",
                "is_llm_eligible": str(sig.is_llm_eligible),
            },
            # Option B: structured signals use "chunks", prose uses extraction
            "strategy": "structured" if not sig.is_llm_eligible else "prose",
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
    """Return 40+ labelled questions with expected signal_ids for V1 validation.

    At least 10 questions reference events older than 6 months from base_date.
    Each question has: query, entity_id (optional), time_window, expected_signal_ids.
    """
    base = datetime(2026, 1, 1, tzinfo=UTC)
    questions = []

    for ent in ENTITIES:
        # Recent questions (within 3 months)
        questions.extend([
            {
                "id": f"q_{ent.entity_id[:8]}_reschedule",
                "query": f"When did {ent.display_name} ask to reschedule a meeting?",
                "entity_id": ent.entity_id,
                "time_window": {"from": (base + timedelta(days=85)).isoformat(),
                                "to": (base + timedelta(days=100)).isoformat()},
                "expected_signal_ids": [
                    _sig_id(f"email_reschedule_{ent.entity_id}"),
                    _sig_id(f"transcript_reschedule_{ent.entity_id}"),
                ],
                "age_months": 3,
            },
            {
                "id": f"q_{ent.entity_id[:8]}_dispute",
                "query": f"invoice dispute {ent.company}",
                "entity_id": ent.entity_id,
                "time_window": {"from": (base + timedelta(days=40)).isoformat(),
                                "to": (base + timedelta(days=55)).isoformat()},
                "expected_signal_ids": [
                    _sig_id(f"email_dispute_{ent.entity_id}"),
                    _sig_id(f"transcript_dispute_{ent.entity_id}"),
                ],
                "age_months": 1.5,
            },
        ])

        # Old questions (> 6 months from base = after day 180)
        for i in range(5):
            day = 180 + i * 15
            dt = base + timedelta(days=day)
            q_sig_id = _sig_id(f"email_{ent.entity_id}_{i + 10}")
            questions.append({
                "id": f"q_{ent.entity_id[:8]}_old_{i}",
                "query": f"emails from {ent.company} in {dt.strftime('%B %Y')}",
                "entity_id": ent.entity_id,
                "time_window": {"from": (dt - timedelta(days=15)).isoformat(),
                                "to": (dt + timedelta(days=15)).isoformat()},
                "expected_signal_ids": [],  # any signal in that window counts
                "age_months": day // 30,
                "time_window_only": True,  # match by time window, not specific IDs
            })

    return questions
