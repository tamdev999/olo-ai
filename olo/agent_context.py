"""olo/agent_context.py — the customer brief the Hermes agent reads.

The ``olo_customer_context`` tool (olo/hermes_plugin) returns this, and only
this, about a customer.  It is read-only: no business action is possible
through it.

Authority.  ``current`` comes from the commitment ledger in olo.db through
olo.current_state (``current_truth`` of a ``customer_context``): status,
confirmed date and time, pending customer requests (never confirmed),
superseded dates.  Hindsight memories are supporting narrative only, every
one labelled by olo.current_state and grouped so that:

  supporting      agrees with the ledger's current state
  no_longer_true  states a superseded date or an earlier version (stale)
  unconfirmed     states a customer request the business has not confirmed
  uncertain       not tied to exactly one commitment

``uncertain_ledger`` lists this customer's events the ledger could not
apply (open unresolved matches, waiting for an operator).  A disputed or
unknown customer identity returns no customer data at all.

Scope.  One olo.db holds one business; the brief reads the active Hermes
profile's olo.db, its bound business and bank, and recalls only the
customer's entity tag in that bank.  Hindsight being unreachable never hides
the ledger: ``memory.status`` says why memories are missing.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Optional

from olo.current_state import (
    CONSISTENT,
    CURRENT_LEDGER,
    HISTORICAL,
    SUPERSEDED_LEDGER,
    UNCERTAIN,
    UNCONFIRMED_REQUEST,
    CommitmentState,
    current_truth,
    customer_context,
    customer_state,
)

__all__ = ["customer_brief", "resolve_customer"]

_log = logging.getLogger(__name__)

_GROUP = {CURRENT_LEDGER: "supporting", CONSISTENT: "supporting",
          HISTORICAL: "no_longer_true", SUPERSEDED_LEDGER: "no_longer_true",
          UNCONFIRMED_REQUEST: "unconfirmed", UNCERTAIN: "uncertain"}

_GUIDANCE = (
    "Answer questions about commitments from 'current' only: it is the commitment ledger. "
    "A 'pending_request' is the customer's proposal and is NOT confirmed. Memories are "
    "narrative background: 'no_longer_true' memories describe superseded states and must not "
    "be repeated as current; 'uncertain' items are not facts. Business actions are disabled.")


def _binding(conn: sqlite3.Connection, name: str) -> Optional[str]:
    try:
        row = conn.execute("SELECT value FROM olo_bindings WHERE name=?", (name,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def resolve_customer(conn: sqlite3.Connection, customer: str) -> dict:
    """``{"status": verified|unknown|disputed|invalid, "contact_id", "reason"}``.

    Accepts a contact id (``cid_...``), an email address or a phone number,
    matched exactly against verified identifiers; never a name.  An
    identifier with an open ownership dispute resolves to nobody.
    """
    from olo.adapters.contacts import PhoneNormalizationError, normalize_phone
    raw = (customer or "").strip()
    if raw.startswith("cid_"):
        found = conn.execute("SELECT id FROM contacts WHERE id=?", (raw,)).fetchone()
        return ({"status": "verified", "contact_id": raw} if found else
                {"status": "unknown", "reason": "no such contact"})
    if "@" in raw:
        kind, value = "email", raw.lower()
    else:
        try:
            kind, value = "phone", normalize_phone(raw)
        except PhoneNormalizationError:
            return {"status": "invalid", "reason": "give a contact id, an email address or a "
                                                   "phone number in international format"}
    disputed = conn.execute(
        "SELECT 1 FROM identifier_conflicts WHERE identifier_type=? AND identifier_value=?"
        " AND resolved_contact_id IS NULL", (kind, value)).fetchone()
    if disputed:
        return {"status": "disputed",
                "reason": f"{kind} is claimed by more than one contact; an operator must resolve it"}
    owners = sorted({r[0] for r in conn.execute(
        "SELECT contact_id FROM contact_identifiers WHERE identifier_type=? AND identifier_value=?"
        " AND revoked_at IS NULL", (kind, value))})
    if not owners:
        return {"status": "unknown", "reason": f"no verified contact has this {kind}"}
    if len(owners) > 1:
        return {"status": "disputed", "reason": f"{kind} belongs to {len(owners)} contacts"}
    return {"status": "verified", "contact_id": owners[0]}


def _commitment(s: CommitmentState) -> dict:
    sources = sorted({h["source_doc_id"] for h in s.history if h.get("source_doc_id")})
    out = {
        "commitment_id": s.commitment_id,
        "type": s.event_type,
        "key": s.business_key or None,
        "status": s.status,
        "date": s.current_date,
        "start": s.current_start,
        "version": s.current_version,
        "superseded_dates": list(s.superseded_dates),
        "sources": sources,
        "evidence_not_active": sorted({h["evidence_state"] for h in s.history
                                       if h.get("evidence_state") not in (None, "active")}),
    }
    if s.pending_action:
        out["pending_request"] = {"action": s.pending_action, "date": s.pending_date,
                                  "confirmed": False}
    return out


def _uncertain_ledger(conn: sqlite3.Connection, contact_id: str) -> list[dict]:
    from olo.ledger import open_unresolved
    return [{"source": u["source_doc_id"], "event": f"{u['event']['actor_role']} "
             f"{u['event']['action']}", "type": u["event_type"],
             "dates": {k: u["event"][k] for k in ("confirmed_date", "prior_confirmed_date",
                                                   "proposed_date") if u["event"].get(k)},
             "why": u["reason"], "waits_for": u["blocked_by"]}
            for u in open_unresolved(conn) if u["contact_id"] == contact_id]


def _warnings(current: list[dict]) -> list[str]:
    by_date: dict[tuple, list[str]] = defaultdict(list)
    for c in current:
        if c["status"] == "scheduled" and c["date"]:
            by_date[(c["type"], c["date"])].append(c["key"] or c["commitment_id"])
    return [f"{len(ids)} {kind} commitments are on {day} ({', '.join(ids)}): refer to them "
            "by key or commitment id, never by date alone"
            for (kind, day), ids in sorted(by_date.items()) if len(ids) > 1]


def customer_brief(conn: sqlite3.Connection, customer: str, question: str = "", *,
                   client_factory: Optional[Callable[[str], Any]] = None) -> dict:
    """The agent-facing context for one customer (see the module docstring).

    ``client_factory(business_id)`` returns this business's Hindsight client;
    None (projection not set up) omits memories.
    """
    who = resolve_customer(conn, customer)
    base = {"authority": "olo commitment ledger (olo.db)", "actions": "disabled",
            "guidance": _GUIDANCE, "identity": who}
    if who["status"] != "verified":
        return base
    contact_id = who["contact_id"]
    business = _binding(conn, "business_id")
    bank = _binding(conn, "hindsight_bank_id")
    memory: dict[str, Any] = {"authoritative": False, "status": "not_configured",
                              **{g: [] for g in ("supporting", "no_longer_true",
                                                 "unconfirmed", "uncertain")}}
    states = None
    if client_factory is not None and business and bank:
        try:
            ctx = customer_context(conn, client_factory(business), bank, contact_id,
                                   question or "commitments, meetings, deliveries and requests")
        except Exception as exc:  # health: allow BLE001 -- memory is optional, the ledger is not; logged with traceback
            _log.warning("customer memory unavailable; ledger-only context", exc_info=True)
            memory["status"] = f"unavailable: {type(exc).__name__}"
        else:
            states = current_truth(ctx)
            memory["status"] = "ok"
            for m in ctx.memories:
                memory[_GROUP[m.label]].append({
                    "text": m.text, "commitment_id": m.commitment_id, "label": m.label,
                    **({"source_state": m.source_state} if m.source_state else {}),
                    **({"notes": list(m.notes)} if m.notes else {})})
            memory["excluded_other_scope"] = len(ctx.excluded)
    if states is None:
        states = tuple(customer_state(conn, contact_id))
    current = [_commitment(s) for s in states]
    name = conn.execute("SELECT display_name FROM contacts WHERE id=?", (contact_id,)).fetchone()
    return {**base,
            "business": business,
            "customer": {"contact_id": contact_id, "name": name[0] if name else ""},
            "current": current,
            "uncertain_ledger": _uncertain_ledger(conn, contact_id),
            "warnings": _warnings(current),
            "memory": memory}
