"""olo/current_state.py — current commitment state for customer-facing retrieval.

olo.db's commitment ledger is the only authority for a commitment's current
confirmed state.  Hindsight is a derived projection: its consolidation LLM
can keep a superseded date in an observation ("The ORD-501 delivery is
confirmed for 2026-06-10. (… rescheduled … to 2026-06-15)") and its fact
extraction records what each message said at the time.  Neither is corrected
here by asking the model; instead every customer-facing answer is assembled
deterministically:

* ``customer_state`` reads the customer's commitments from the ledger: the
  latest confirmed state, its version, every earlier confirmed date, and any
  pending customer proposal, which is never confirmed until the business
  authorises it.
* ``label_memories`` labels each recalled memory against that state:
  ledger-state documents of the current version vs superseded versions (by
  version number, from the ledger — not the document's own wording), and any
  other memory that mentions a superseded or cancelled date (``historical``)
  or a pending proposal's date (``unconfirmed_request``).  Memories of
  another customer are excluded.
* ``recall_for_customer`` runs a recall restricted to the customer's entity
  tag and returns the ledger state first, then the labelled memories.

No state is stored here: everything is recomputed from olo.db on each call.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

__all__ = [
    "CommitmentState",
    "customer_state",
    "dates_mentioned",
    "label_memories",
    "recall_for_customer",
]

#: Labels a recalled memory can carry.
CURRENT_LEDGER = "current_ledger"            # ledger document of the current version
SUPERSEDED_LEDGER = "superseded_ledger"      # ledger document of an earlier version
HISTORICAL = "historical"                    # mentions a superseded / cancelled date
UNCONFIRMED_REQUEST = "unconfirmed_request"  # mentions a pending proposal's date
MEMORY = "memory"                            # nothing to qualify

_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTHS = {m: i for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_MONTH_RE = "|".join(sorted({*_MONTHS, *(m[:3] for m in _MONTHS), "sept"}, key=len, reverse=True))
_MONTH_DAY = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\b", re.IGNORECASE)


@dataclass(frozen=True)
class CommitmentState:
    """One commitment as the ledger holds it now."""
    commitment_id: str
    event_type: str
    business_key: Optional[str]
    status: Optional[str]                 # 'scheduled' | 'cancelled' | None
    current_date: Optional[str]           # confirmed date now in force (None if cancelled)
    current_version: Optional[int]
    superseded_dates: tuple[str, ...] = ()   # earlier confirmed / cancelled dates
    pending_action: Optional[str] = None
    pending_date: Optional[str] = None       # customer proposal, NOT confirmed
    history: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["history"] = [dict(h) for h in self.history]
        return d


def customer_state(conn: sqlite3.Connection, contact_id: str) -> list[CommitmentState]:
    """The customer's commitments, current state first-class, from olo.db."""
    from olo.ledger import get_all_versions, get_current_state

    out: list[CommitmentState] = []
    for (cid,) in conn.execute(
            "SELECT id FROM commitments WHERE contact_id=? ORDER BY created_at, id", (contact_id,)):
        st = get_current_state(conn, cid) or {}
        versions = get_all_versions(conn, cid)
        current = st.get("scheduled_date")
        earlier = set()
        for v in versions:
            if v["is_confirmed"] and v["version"] != st.get("version"):
                earlier.update(d for d in (v["confirmed_date"], v["prior_confirmed_date"]) if d)
        if st.get("status") == "cancelled" and st.get("prior_confirmed_date"):
            earlier.add(st["prior_confirmed_date"])
        earlier.discard(current)
        out.append(CommitmentState(
            commitment_id=cid, event_type=st.get("event_type") or "",
            business_key=st.get("business_key"), status=st.get("status"),
            current_date=current, current_version=st.get("version"),
            superseded_dates=tuple(sorted(earlier)),
            pending_action=st.get("pending_action"),
            pending_date=st.get("pending_proposed_date"),
            history=tuple({k: v[k] for k in ("version", "actor_role", "action", "confirmed_date",
                                              "proposed_date", "is_confirmed", "occurred_at")}
                          for v in versions)))
    return out


def dates_mentioned(text: str, years: set[int]) -> set[str]:
    """ISO dates in *text*, plus month-day forms ("June 15", "15th of June")
    resolved in each of *years* (a text rarely restates the year)."""
    text = text or ""
    found = {f"{y}-{m}-{d}" for y, m, d in _ISO.findall(text)}
    pairs = [(m, d) for m, d in _MONTH_DAY.findall(text)] + [(m, d) for d, m in _DAY_MONTH.findall(text)]
    for month, day in pairs:
        mon = _MONTHS.get(month.lower()) or next(
            (i for name, i in _MONTHS.items() if name.startswith(month.lower()[:3])), None)
        for y in years:
            try:
                found.add(date(y, mon, int(day)).isoformat())
            except (TypeError, ValueError):
                continue
    return found


def _ledger_ref(document_id: Optional[str]) -> Optional[tuple[str, int]]:
    m = re.fullmatch(r"ledger:(.+):v(\d+)", document_id or "")
    return (m.group(1), int(m.group(2))) if m else None


def label_memories(results: list[dict], states: list[CommitmentState],
                   ent_tag: str) -> tuple[list[dict], list[dict]]:
    """(labelled, excluded): every result labelled against the ledger.

    A result tagged with another customer's entity is excluded, never shown.
    """
    by_id = {s.commitment_id: s for s in states}
    years = {int(d[:4]) for s in states
             for d in (s.current_date, s.pending_date, *s.superseded_dates) if d}
    labelled, excluded = [], []
    for r in results:
        tags = r.get("tags") or []
        ents = {t for t in tags if t.startswith("ent:")}
        if ents and ents != {ent_tag}:
            excluded.append({**r, "excluded": "another customer's memory"})
            continue
        item = {k: r.get(k) for k in ("type", "text", "document_id")}
        notes: list[str] = []
        ref = _ledger_ref(r.get("document_id"))
        if ref and ref[0] in by_id:
            s = by_id[ref[0]]
            if ref[1] == s.current_version:
                item["label"] = CURRENT_LEDGER
            else:
                item["label"] = SUPERSEDED_LEDGER
                notes.append(f"ledger version {ref[1]} is superseded by version "
                             f"{s.current_version} ({s.status}, {s.current_date or 'no date'})")
        else:
            said = dates_mentioned(r.get("text") or "", years)
            label = MEMORY
            for s in states:
                for d in sorted(said & set(s.superseded_dates)):
                    label = HISTORICAL
                    notes.append(f"{d} is no longer in force for {s.event_type} "
                                 f"{s.business_key or s.commitment_id}: current is "
                                 f"{s.status}, {s.current_date or 'no date'} (ledger v{s.current_version})")
                if s.pending_date and s.pending_date in said and s.pending_date != s.current_date:
                    if label == MEMORY:
                        label = UNCONFIRMED_REQUEST
                    notes.append(f"{s.pending_date} is a customer request ({s.pending_action}), "
                                 "not confirmed by the business")
            item["label"] = label
        item["notes"] = notes
        labelled.append(item)
    return labelled, excluded


def recall_for_customer(conn: sqlite3.Connection, client: Any, bank_id: str, contact_id: str,
                        query: str, *, types: Optional[list[str]] = None) -> dict[str, Any]:
    """Customer-facing recall: the ledger's current state, then labelled memories.

    The recall is restricted to the customer's entity tag; anything else that
    comes back is excluded and reported.
    """
    ent_tag = f"ent:{contact_id}"
    states = customer_state(conn, contact_id)
    reply = client.recall(query, bank_id=bank_id, types=types, tags=[ent_tag],
                          tags_match="all_strict")
    results = [{"type": x.get("type") or x.get("fact_type"), "text": str(x.get("text") or ""),
                "document_id": x.get("document_id"), "tags": x.get("tags") or []}
               for x in (reply.get("results") or [])]
    labelled, excluded = label_memories(results, states, ent_tag)
    return {"current": [s.as_dict() for s in states], "memories": labelled, "excluded": excluded}
