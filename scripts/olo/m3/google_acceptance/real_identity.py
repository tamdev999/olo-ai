"""Genuine second-identity acceptance: provenance and the operator's plan.

A ``second_identity`` scenario (or a ``simulated`` one) passes for real only
when its action is recorded ``--by real-identity`` *and* its data shows it
came from a genuine second party, never from the seeder:

* the seeder's messages carry ``Message-ID: <olo-acc.<run>.<key>@olo-seed.example.com>``
  and its events ``id = oloacc…``; neither may appear;
* the customer is a real address (no reserved test domain), and the
  received mail is from that address;
* the composing client left its own mark: Gmail web's Message-IDs end in
  ``@mail.gmail.com``, Outlook.com's in ``outlook.com``; Google Calendar's
  invitation emails' in ``@google.com``;
* a calendar invitation is organized by the customer, not by the business
  (``organizer.self`` is false).

A real-identity action whose data fails provenance is ``failed`` (the
details name what was wrong), so seeded data recorded as real can never pass.

Identities: one second Google account (Gmail and Calendar web) covers G1,
G4, C4, L1 and L3; one Outlook.com account covers G2.  Neither is connected
to Olo: the operator acts in each account's own web UI, and Olo reads only
the business's mailbox and calendar through its existing read-only grant.
"""
from __future__ import annotations

from typing import Callable

SEED_MESSAGE_ID = ("<olo-acc.", "@olo-seed.example.com>")
SEED_EVENT_PREFIX = "oloacc"
SYNTHETIC_SUFFIXES = (".example.com", ".example", ".invalid", ".test")
GMAIL_WEB_ID = "@mail.gmail.com>"
OUTLOOK_ID = "outlook.com>"
OUTLOOK_DOMAINS = ("outlook.com", "hotmail.com", "live.com", "msn.com")
GOOGLE_INVITE_ID = "@google.com>"


def synthetic(address: str) -> bool:
    domain = (address or "").rsplit("@", 1)[-1].lower()
    return not domain or domain.endswith(SYNTHETIC_SUFFIXES) or "@" not in (address or "")


def _seeded_message(mid: str) -> bool:
    m = (mid or "").lower()
    return m.startswith(SEED_MESSAGE_ID[0]) or m.endswith(SEED_MESSAGE_ID[1])


def _received(ctx, tag: str) -> list[dict]:
    from google_acceptance.scenarios import _content
    return [s for s in _content(ctx, tag) if s["payload"].get("direction") == "received"]


def _senders(sig: dict) -> set[str]:
    return {(a.get("email") if isinstance(a, dict) else str(a)).lower()
            for a in sig["payload"].get("from") or []}


def _mail(ctx, tag: str, *, sender: str, client_mark: str) -> tuple[bool, dict]:
    """Every received tagged message: from *sender*, not seeded, composed by the client."""
    sigs = _received(ctx, tag)
    rows = [{"message": s["payload"].get("message_id"), "from_sender": sender in _senders(s),
             "seeded": _seeded_message(s["payload"].get("rfc822_message_id")),
             "client": (s["payload"].get("rfc822_message_id") or "").lower().endswith(client_mark.lower())}
            for s in sigs]
    ok = (bool(rows) and not synthetic(sender)
          and all(r["from_sender"] and not r["seeded"] and r["client"] for r in rows))
    return ok, {"sender_synthetic": synthetic(sender), "received": rows, "client_mark": client_mark}


def _invitation(ctx, tag: str) -> tuple[bool, dict]:
    """The tagged event is organized by the customer (not the business, not the seeder)."""
    from google_acceptance.scenarios import _cal
    events = _cal(ctx, tag)
    rows = [{"event": (s["payload"].get("event_id") or "")[:12],
             "seeded": (s["payload"].get("event_id") or "").startswith(SEED_EVENT_PREFIX),
             "organizer_is_customer": (s["payload"].get("organizer_email") or "").lower() == ctx.customer,
             "organizer_self": bool(s["payload"].get("organizer_self"))} for s in events]
    ok = (bool(rows) and not synthetic(ctx.customer)
          and all(not r["seeded"] and r["organizer_is_customer"] and not r["organizer_self"] for r in rows))
    return ok, {"customer_synthetic": synthetic(ctx.customer), "events": rows}


def g1(ctx):
    return _mail(ctx, "G1", sender=ctx.customer, client_mark=GMAIL_WEB_ID)


def g2(ctx):
    sender = ctx.outlook or ""
    ok, details = _mail(ctx, "G2", sender=sender, client_mark=OUTLOOK_ID)
    outlook = sender.rsplit("@", 1)[-1].lower() in OUTLOOK_DOMAINS
    return ok and outlook, {**details, "outlook_domain": outlook}


def g4(ctx):
    return _mail(ctx, "G4", sender=ctx.customer, client_mark=GMAIL_WEB_ID)


def c4(ctx):
    return _invitation(ctx, "C4")


def l1(ctx):
    ok_event, events = _invitation(ctx, "L1")
    invites = [s for s in _received(ctx, "L1")]
    rows = [{"message": s["payload"].get("message_id"),
             "google_invitation": (s["payload"].get("rfc822_message_id") or "").lower().endswith(GOOGLE_INVITE_ID),
             "seeded": _seeded_message(s["payload"].get("rfc822_message_id"))} for s in invites]
    ok = ok_event and bool(rows) and all(r["google_invitation"] and not r["seeded"] for r in rows)
    return ok, {**events, "invitation_emails": rows}


def l3(ctx):
    return _invitation(ctx, "L1")


#: Scenario → its provenance check (run for a ``--by real-identity`` action).
PROVENANCE: dict[str, Callable] = {"G1": g1, "G2": g2, "G4": g4, "C4": c4, "L1": l1, "L3": l3}


def plan(run_id: str) -> str:
    """The operator's steps for *run_id*: exact subjects and titles, in order.

    Every subject and title ends in ``acc <run id>`` so the checks see only
    this run's data.  BUSINESS is the dedicated test account, CUSTOMER the
    second Google account, OUTLOOK the Outlook.com account."""
    t = f"acc {run_id}"
    return f"""Real second-identity acceptance, run {run_id}
(BUSINESS = the dedicated test account; CUSTOMER = the second Google account;
OUTLOOK = the Outlook.com account.  Use each account's own web UI; nothing is
connected to Olo.  After each step: run.py sync, then run.py action <ID> --by real-identity.)

G1  BUSINESS (Gmail web) emails CUSTOMER:  "[OLO-G1] order question - {t}"
      "Your order 4471 ships on Friday."
    CUSTOMER replies from Gmail web, keeping Gmail's quoted text:
      "Could you confirm it ships on Friday?"
    BUSINESS replies from Gmail web, keeping the quote: "Yes, it ships on Friday."
G4  CUSTOMER (Gmail web) emails BUSINESS "[OLO-G4] attachment - {t}" with a .txt
    attachment containing OLO-ATTACHMENT-TEXT, plus the G1 email attached
    with Gmail's "Forward as attachment".
G2  BUSINESS emails OUTLOOK "[OLO-G2] quote - {t}"; OUTLOOK replies from
    Outlook.com (HTML, its default), keeping Outlook's From:/Sent: header block.
C4  CUSTOMER (Calendar web) creates "[OLO-C4] site visit - {t}" next week and
    invites BUSINESS; sync.  BUSINESS accepts in Calendar; sync.  BUSINESS
    declines; sync.
L1  CUSTOMER (Calendar web) creates "[OLO-L1] consultation - {t}" in two weeks
    and invites BUSINESS.  Google's own invitation email reaches BUSINESS's
    Gmail; sync (both sources).
L3  CUSTOMER deletes the [OLO-L1] event and sends the cancellation; sync.

Then: run.py check G1 G2 G4 C4 L1 L3.  Provenance fails (and so the scenario
fails) if any message or event came from the seeder or the wrong sender.
Cleanup: delete the messages and events by hand in each account (the seeder
never touches data it did not create).
"""
