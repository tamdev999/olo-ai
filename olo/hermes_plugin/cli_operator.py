"""`hermes olo operator|review|resolve` — the operator workflow (olo.operator).

``review`` lists what Olo left for a person.  ``resolve`` records a decision
and ``operator`` manages who may decide.  Every command that decides
something asks for the operator's passphrase on the terminal; it is never
read from arguments, the environment or a pipe.
"""
from __future__ import annotations

import argparse
import json
import sys

__all__ = ["register"]


def _passphrase(prompt: str) -> str:
    """Read a passphrase from the controlling terminal only."""
    import getpass
    if not sys.stdin.isatty():
        raise SystemExit("olo: operator passphrases are read from an interactive terminal only")
    return getpass.getpass(prompt)


def _conn():
    from olo.operator import migrate
    from olo.paths import db_path
    from olo.store import connect
    from olo.store import migrate as migrate_store
    db = db_path()
    if not db.exists():
        raise SystemExit("olo: database not initialised (run `hermes olo setup` first)")
    conn = connect(db)
    migrate_store(conn)
    migrate(conn)
    return conn


def _now() -> str:
    from datetime import UTC, datetime
    from olo.signals import format_utc
    return format_utc(datetime.now(UTC))


def _grant(conn, name: str):
    from olo.ledger import AuthorizationError
    from olo.operator import authorize
    try:
        return authorize(conn, name, _passphrase(f"Passphrase for operator {name}: "), now=_now())
    except AuthorizationError as exc:
        raise SystemExit(f"olo: {exc}") from None


def _cmd_enroll(args: argparse.Namespace) -> None:
    from olo.operator import enroll, operators
    conn = _conn()
    grant = _grant(conn, args.by) if operators(conn) else None
    first = _passphrase(f"New passphrase for {args.name}: ")
    if first != _passphrase("Repeat it: "):
        raise SystemExit("olo: the passphrases differ")
    enroll(conn, args.name, first, now=_now(), grant=grant)
    print(f"Operator {args.name} enrolled" + ("" if grant else " (first operator of this business)"))


def _cmd_revoke(args: argparse.Namespace) -> None:
    from olo.operator import revoke
    conn = _conn()
    revoke(conn, _grant(conn, args.by), args.name, now=_now(), reason=args.reason)
    print(f"Operator {args.name} revoked")


def _cmd_review(args: argparse.Namespace) -> None:
    from olo.ledger import open_unresolved
    conn = _conn()
    events = open_unresolved(conn)
    print(f"Unresolved ledger events: {len(events)}")
    for u in events:
        e = u["event"]
        dates = {k: e[k] for k in ("confirmed_date", "prior_confirmed_date", "proposed_date") if e.get(k)}
        print(f"  {u['source_doc_id']}  customer={u['contact_id']} {e['event_type']}"
              f" {e['actor_role']}:{e['action']} {json.dumps(dates)}"
              f" key={e.get('business_key') or '-'} thread={e.get('thread_id') or '-'}")
        print(f"    why: {u['reason'] or '-'}")
        if u["candidate_ids"]:
            print(f"    candidates: {', '.join(u['candidate_ids'])}")
        if u["blocked_by"]:
            print(f"    waits for: {', '.join(u['blocked_by'])}")
    ambiguous = conn.execute(
        "SELECT source, signal_id, identity_reason FROM signal_resolution"
        " WHERE identity_status='ambiguous' ORDER BY source, signal_id").fetchall()
    print(f"Ambiguous signal identities: {len(ambiguous)}")
    for r in ambiguous:
        print(f"  {r[0]}/{r[1]}: {r[2] or '-'}")
    try:
        conflicts = conn.execute(
            "SELECT identifier_type, identifier_value, GROUP_CONCAT(DISTINCT existing_contact_id),"
            " GROUP_CONCAT(DISTINCT new_contact_id) FROM identifier_conflicts"
            " WHERE resolved_contact_id IS NULL GROUP BY identifier_type, identifier_value").fetchall()
    except Exception:
        conflicts = []
    print(f"Identifier conflicts: {len(conflicts)}")
    for r in conflicts:
        print(f"  {r[0]}:{r[1]} claimed by {r[2]}, {r[3]}")


def _cmd_resolve_commitment(args: argparse.Namespace) -> None:
    from olo.operator import resolve_commitment
    decision = "link" if args.link else ("distinct" if args.distinct else "dismiss")
    conn = _conn()
    res = resolve_commitment(conn, _grant(conn, args.operator), args.source_doc_id, decision,
                             target=args.link, reason=args.reason, now=_now())
    print(f"Decision {res.decision_id}: {res.outcome} — {res.message}")
    if res.replayed:
        print(f"  applied {len(res.replayed)} event(s) that waited: {', '.join(res.replayed)}")
    if res.outcome == "refused":
        raise SystemExit(1)


def _cmd_resolve_identity(args: argparse.Namespace) -> None:
    from olo.operator import assign_signal_identity
    conn = _conn()
    did = assign_signal_identity(conn, _grant(conn, args.operator), args.source, args.signal,
                                 args.contact, reason=args.reason, now=_now())
    print(f"Decision {did}: {args.source}/{args.signal} → {args.contact}")


def _cmd_resolve_identifier(args: argparse.Namespace) -> None:
    from olo.operator import resolve_identifier
    conn = _conn()
    did = resolve_identifier(conn, _grant(conn, args.operator), args.type, args.value,
                             args.contact, reason=args.reason, now=_now())
    print(f"Decision {did}: {args.type}:{args.value} → {args.contact}")


def register(subs) -> None:
    """Add the operator, review and resolve sub-commands to `hermes olo`."""
    op = subs.add_parser("operator", help="Manage who may decide unresolved items")
    op_subs = op.add_subparsers(dest="operator_cmd")
    en = op_subs.add_parser("enroll", help="Enrol an operator (passphrase prompted)")
    en.add_argument("--name", required=True)
    en.add_argument("--by", help="An existing operator authorizing the enrolment")
    en.set_defaults(func=_cmd_enroll)
    rv = op_subs.add_parser("revoke", help="Revoke an operator")
    rv.add_argument("--name", required=True)
    rv.add_argument("--by", required=True)
    rv.add_argument("--reason", required=True)
    rv.set_defaults(func=_cmd_revoke)

    review = subs.add_parser("review", help="List what is waiting for an operator")
    review.set_defaults(func=_cmd_review)

    res = subs.add_parser("resolve", help="Record an operator decision")
    res_subs = res.add_subparsers(dest="resolve_cmd")
    cm = res_subs.add_parser("commitment", help="Decide an unresolved ledger event")
    cm.add_argument("source_doc_id")
    which = cm.add_mutually_exclusive_group(required=True)
    which.add_argument("--link", metavar="COMMITMENT_ID", help="apply it to this commitment")
    which.add_argument("--distinct", action="store_true", help="record it as a new commitment")
    which.add_argument("--dismiss", action="store_true", help="settle it with no ledger change")
    cm.add_argument("--reason", required=True)
    cm.add_argument("--operator", required=True)
    cm.set_defaults(func=_cmd_resolve_commitment)
    idn = res_subs.add_parser("identity", help="Settle an ambiguous signal's customer")
    idn.add_argument("--source", required=True)
    idn.add_argument("--signal", required=True)
    idn.add_argument("--contact", required=True)
    idn.add_argument("--reason", required=True)
    idn.add_argument("--operator", required=True)
    idn.set_defaults(func=_cmd_resolve_identity)
    idf = res_subs.add_parser("identifier", help="Assign a conflicted identifier")
    idf.add_argument("--type", required=True, choices=("email", "phone"))
    idf.add_argument("--value", required=True)
    idf.add_argument("--contact", required=True)
    idf.add_argument("--reason", required=True)
    idf.add_argument("--operator", required=True)
    idf.set_defaults(func=_cmd_resolve_identifier)
