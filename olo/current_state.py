"""olo/current_state.py — the customer context future Hermes tools must consume.

olo.db's commitment ledger is the only authority for a commitment's current
confirmed state.  Hindsight is a derived projection: its consolidation LLM
can keep a superseded date in an observation, and its facts record what each
message said at the time.  Nothing here asks the model to correct itself.

``customer_context`` is the one supported way to answer a question about a
customer:

* ``current`` — the customer's commitments from the ledger (status, current
  confirmed date and version, earlier dates, any pending customer proposal,
  never confirmed until the business authorises it).  This, and only this,
  is current operational truth.
* ``memories`` — recalled Hindsight memories (restricted to the customer's
  entity tag), each a ``LabelledMemory`` with ``authoritative=False``,
  labelled against the ONE commitment its provenance links it to:

  ========================  ==============================================
  link                      how a memory is tied to a commitment
  ========================  ==============================================
  ``ledger_document``       ``ledger:<commitment>:v<n>`` document id
  ``signal_provenance``     ``sig:<signal>`` document whose signal the
                            ledger recorded for that commitment/version
  ``observation_sources``   an observation whose source facts all come from
                            documents of that commitment
  ``business_key``          the text names exactly one of the customer's
                            commitment keys (fallback; no structural link)
  ========================  ==============================================

  A memory with no link, or links to several commitments, is ``uncertain``:
  it is never silently assigned.  Dates are compared with the linked
  commitment only, so order A's valid date is never "historical" because
  order B once used it, and order B's pending request never relabels A.

``current_truth`` accepts only a ``CustomerContext``: a raw Hindsight recall
response is refused (TypeError), so it cannot be mistaken for the ledger.
Everything is recomputed from olo.db on each call; no state is stored here.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Optional

__all__ = [
    "CommitmentState",
    "CustomerContext",
    "LabelledMemory",
    "current_truth",
    "customer_context",
    "customer_state",
    "dates_mentioned",
    "label_memories",
    "recall_for_customer",
]

# Labels (none of them makes a memory authoritative).
CURRENT_LEDGER = "current_ledger"            # projection of the ledger's current version
SUPERSEDED_LEDGER = "superseded_ledger"      # projection of an earlier version
CONSISTENT = "consistent"                    # linked; agrees with the current state
HISTORICAL = "historical"                    # linked; states something no longer in force
UNCONFIRMED_REQUEST = "unconfirmed_request"  # linked; states a pending customer proposal
UNCERTAIN = "uncertain"                      # not linked to exactly one commitment, or
#                                              states dates the ledger does not hold

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

    def known_dates(self) -> set[str]:
        """Every date the ledger holds for this commitment (any version, and
        the days its versions happened on)."""
        out = {d for d in (self.current_date, self.pending_date, *self.superseded_dates) if d}
        for h in self.history:
            out.update(d for d in (h.get("confirmed_date"), h.get("proposed_date")) if d)
            if h.get("occurred_at"):
                out.add(str(h["occurred_at"])[:10])
        return out

    def version(self, n: int) -> dict:
        return next((h for h in self.history if h.get("version") == n), {})


@dataclass(frozen=True)
class LabelledMemory:
    """A recalled Hindsight memory, never authoritative."""
    text: str
    type: Optional[str]
    document_id: Optional[str]
    label: str
    commitment_id: Optional[str]
    link: str
    notes: tuple[str, ...] = ()
    authoritative: bool = False

    def as_dict(self) -> dict:
        d = asdict(self)
        d["notes"] = list(self.notes)
        return d


@dataclass(frozen=True)
class CustomerContext:
    """What a Hermes tool may tell about one customer: ledger state first."""
    contact_id: str
    current: tuple[CommitmentState, ...]
    memories: tuple[LabelledMemory, ...]
    excluded: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {"contact_id": self.contact_id,
                "current": [s.as_dict() for s in self.current],
                "memories": [m.as_dict() for m in self.memories],
                "excluded": [dict(x) for x in self.excluded]}


def current_truth(context: CustomerContext) -> tuple[CommitmentState, ...]:
    """The customer's current confirmed commitments — only from a context
    built by ``customer_context`` (the ledger), never from raw recall."""
    if not isinstance(context, CustomerContext):
        raise TypeError(
            "current operational truth comes only from olo.current_state.customer_context "
            f"(the commitment ledger), not from {type(context).__name__}: raw Hindsight "
            "recall is derived memory and is never authoritative")
    return context.current


def customer_state(conn: sqlite3.Connection, contact_id: str) -> list[CommitmentState]:
    """The customer's commitments, from olo.db."""
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


def signal_links(conn: sqlite3.Connection) -> dict[str, tuple[str, Optional[int]]]:
    """``sig:<signal>`` document id -> (commitment id, version) the ledger
    recorded for that signal (olo.db signal_resolution)."""
    try:
        rows = conn.execute(
            "SELECT signal_id, commitment_id, commitment_version FROM signal_resolution"
            " WHERE commitment_id IS NOT NULL AND commitment_id != ''").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {f"sig:{r[0]}": (r[1], r[2]) for r in rows}


def _ledger_ref(document_id: Optional[str]) -> Optional[tuple[str, int]]:
    m = re.fullmatch(r"ledger:(.+):v(\d+)", document_id or "")
    return (m.group(1), int(m.group(2))) if m else None


def _keys_named(text: str, states: list[CommitmentState]) -> set[str]:
    return {s.commitment_id for s in states
            if s.business_key and re.search(rf"(?<![\w-]){re.escape(s.business_key)}(?![\w-])",
                                            text or "", re.IGNORECASE)}


def _link(r: dict, states_by_id: dict, links: dict, source_docs: dict,
          states: list[CommitmentState]) -> tuple[set[str], str, Optional[int], list[str]]:
    """(commitment ids, link basis, linked version, notes) for one result."""
    doc = r.get("document_id")
    ref = _ledger_ref(doc)
    if ref:
        return ({ref[0]} if ref[0] in states_by_id else set()), "ledger_document", ref[1], []
    if doc and doc.startswith("sig:"):
        if doc in links:
            cid, ver = links[doc]
            return ({cid} if cid in states_by_id else set()), "signal_provenance", ver, []
        return set(), "signal_without_commitment", None, [f"{doc} produced no ledger event"]
    sources = [d for d in (source_docs.get(fid) for fid in r.get("source_fact_ids") or []) if d]
    if sources:
        cids: set[str] = set()
        for d in sources:
            lref = _ledger_ref(d)
            if lref:
                cids.add(lref[0])
            elif d in links:
                cids.add(links[d][0])
            else:
                return set(), "observation_sources", None, [
                    f"source document {d} is not linked to a commitment"]
        return cids & set(states_by_id), "observation_sources", None, []
    named = _keys_named(r.get("text") or "", states)
    if named:
        return named, "business_key", None, []
    return set(), "none", None, []


def _date_label(text: str, s: CommitmentState, years: set[int]) -> tuple[str, list[str]]:
    said = dates_mentioned(text, years)
    notes: list[str] = []
    where = f"{s.event_type} {s.business_key or s.commitment_id}"
    if s.pending_date and s.pending_date != s.current_date and s.pending_date in said:
        notes.append(f"{s.pending_date} is a customer request ({s.pending_action}) for {where}, "
                     "not confirmed by the business")
        return UNCONFIRMED_REQUEST, notes
    old = sorted(said & set(s.superseded_dates))
    if old:
        notes.append(f"{', '.join(old)} no longer in force for {where}: current is "
                     f"{s.status}, {s.current_date or 'no date'} (ledger v{s.current_version})")
        return HISTORICAL, notes
    unknown = sorted(said - s.known_dates())
    if unknown:
        notes.append(f"{', '.join(unknown)} not held by the ledger for {where}")
        return UNCERTAIN, notes
    return CONSISTENT, notes


def label_memories(results: list[dict], states: list[CommitmentState], ent_tag: str, *,
                   links: Optional[dict] = None, source_docs: Optional[dict] = None,
                   ) -> tuple[list[LabelledMemory], list[dict]]:
    """(labelled, excluded): every result labelled against the commitment
    its provenance links it to.  Another customer's memory is excluded."""
    links, source_docs = links or {}, source_docs or {}
    by_id = {s.commitment_id: s for s in states}
    labelled: list[LabelledMemory] = []
    excluded: list[dict] = []
    for r in results:
        ents = {t for t in r.get("tags") or [] if t.startswith("ent:")}
        if ents and ents != {ent_tag}:
            excluded.append({**r, "excluded": "another customer's memory"})
            continue
        text = r.get("text") or ""
        cids, basis, version, notes = _link(r, by_id, links, source_docs, states)
        if len(cids) != 1:
            why = (f"links to {len(cids)} commitments" if cids
                   else "not linked to any commitment of this customer")
            labelled.append(LabelledMemory(text, r.get("type"), r.get("document_id"), UNCERTAIN,
                                           None, basis, tuple(notes + [why])))
            continue
        (cid,) = cids
        s = by_id[cid]
        named = _keys_named(text, states)
        if named and named != {cid}:
            labelled.append(LabelledMemory(text, r.get("type"), r.get("document_id"), UNCERTAIN,
                                           cid, basis, tuple(notes + [
                                               "its text names another commitment's key"])))
            continue
        years = {int(d[:4]) for d in s.known_dates()}
        if basis == "ledger_document":
            if version == s.current_version:
                label = CURRENT_LEDGER
            else:
                label = SUPERSEDED_LEDGER
                notes.append(f"ledger version {version} is superseded by version "
                             f"{s.current_version} ({s.status}, {s.current_date or 'no date'})")
        elif basis == "signal_provenance" and version is not None:
            v = s.version(version)
            if not v.get("is_confirmed"):
                pending = s.pending_date and v.get("proposed_date") == s.pending_date
                label = UNCONFIRMED_REQUEST if pending else HISTORICAL
                notes.append(f"evidence of customer request v{version} for "
                             f"{s.business_key or cid}: "
                             + ("still pending, not confirmed" if pending else "no longer pending"))
            elif version != s.current_version:
                label = HISTORICAL
                notes.append(f"evidence of superseded version {version}; current is "
                             f"v{s.current_version} ({s.status}, {s.current_date or 'no date'})")
            else:
                label, more = _date_label(text, s, years)
                if label == HISTORICAL and s.current_date and s.current_date in dates_mentioned(text, years):
                    label = CONSISTENT          # states the change to the current version
                notes.extend(more)
        else:
            label, more = _date_label(text, s, years)
            notes.extend(more)
        labelled.append(LabelledMemory(text, r.get("type"), r.get("document_id"), label, cid,
                                       basis, tuple(notes)))
    return labelled, excluded


def customer_context(conn: sqlite3.Connection, client: Any, bank_id: str, contact_id: str,
                     query: str, *, types: Optional[list[str]] = None) -> CustomerContext:
    """Ledger state, then the customer's recalled memories, each labelled
    against its own commitment.  Recall is restricted to the customer's
    entity tag; anything else returned is excluded and reported."""
    ent_tag = f"ent:{contact_id}"
    states = customer_state(conn, contact_id)
    reply = client.recall(query, bank_id=bank_id, types=types, tags=[ent_tag],
                          tags_match="all_strict", include_source_facts=True) or {}
    source_docs = {fid: (f or {}).get("document_id")
                   for fid, f in (reply.get("source_facts") or {}).items()}
    results = [{"type": x.get("type") or x.get("fact_type"), "text": str(x.get("text") or ""),
                "document_id": x.get("document_id"), "tags": x.get("tags") or [],
                "source_fact_ids": x.get("source_fact_ids") or []}
               for x in (reply.get("results") or [])]
    labelled, excluded = label_memories(results, states, ent_tag, links=signal_links(conn),
                                        source_docs=source_docs)
    return CustomerContext(contact_id, tuple(states), tuple(labelled), tuple(excluded))


def recall_for_customer(conn: sqlite3.Connection, client: Any, bank_id: str, contact_id: str,
                        query: str, *, types: Optional[list[str]] = None) -> dict[str, Any]:
    """``customer_context`` as a JSON-serialisable dict (evidence, reports)."""
    return customer_context(conn, client, bank_id, contact_id, query, types=types).as_dict()
