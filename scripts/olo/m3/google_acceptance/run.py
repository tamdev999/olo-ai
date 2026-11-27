"""Operator command line for the Google live acceptance run.

  python scripts/olo/m3/google_acceptance/run.py environment
  python scripts/olo/m3/google_acceptance/run.py preflight
  python scripts/olo/m3/google_acceptance/run.py sync [--max-requests 400]
  python scripts/olo/m3/google_acceptance/run.py action <ID> [--by operator|seeder|real-identity] [--note TEXT]
  python scripts/olo/m3/google_acceptance/run.py expire-cursor gmail|gcal
  python scripts/olo/m3/google_acceptance/run.py rerun
  python scripts/olo/m3/google_acceptance/run.py check [ID ...]
  python scripts/olo/m3/google_acceptance/run.py teardown

Test data comes from the seeder project
(``python -m tests.olo.live_seed.acceptance_fixtures``) or by hand; ``action
--by`` records who produced it, and only ``--by real-identity`` lets a
second-identity scenario be evaluated (scenarios.py: evidence classes).
``check`` exits 0 accepted, 1 rejected, 2 incomplete, 4 accepted within scope.

``environment`` checks the host (user, network namespace, imports, loopback)
without reading credentials or contacting Google.  Every other command except
``teardown`` runs the preflight first and stops on a refusal.  ``sync`` runs both sources once through Olo's real worker with a
request budget and records Google's response shapes; the scheduled cron jobs
(E1–E6) run the same code unattended.  Evidence lands in
$OLO_ACCEPTANCE_EVIDENCE; credentials stay in the acceptance home.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_acceptance import guard, scenarios
from google_acceptance.recorder import RecordingClient

BIZ = "accept"
BANK = f"olo-{BIZ}"
TEARDOWN = """\
Teardown (only steps 0 and 1 touch Google):
  0. python -m tests.olo.live_seed.acceptance_fixtures cleanup --run-id <run> --dry-run, then
     without --dry-run (root, seeder credentials): removes only this run's marked fixtures
  1. hermes olo google disconnect --account <alias>      # revokes the OAuth token
  2. hermes cron remove olo-gmail-<alias>; hermes cron remove olo-gcal-<alias>;
     hermes cron remove olo-hindsight-accept
  3. hermes olo hindsight stop
  4. Archive $OLO_ACCEPTANCE_EVIDENCE (no credentials are in it), then delete
     $OLO_ACCEPTANCE_HOME (credentials, olo.db, Hindsight data).
  5. Keep both Google Cloud projects and clients (Olo's and the seeder's) for
     the next run: they are reused, not recreated.
"""


def _now() -> str:
    from olo.signals import format_utc
    return format_utc(datetime.now(UTC))


def _json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _preflight(env=os.environ, client_factory=None) -> guard.Preflight:
    pf = guard.run_preflight(env, client_factory=client_factory)
    if pf.settings is not None:
        _write(pf.settings.evidence / "preflight.json",
               {"at": _now(), "ok": pf.ok, "refusals": pf.refusals, "facts": pf.facts})
    if not pf.ok:
        for r in pf.refusals:
            print(f"REFUSED: {r}", file=sys.stderr)
        raise SystemExit(3)
    return pf


def _conn(pf: guard.Preflight):
    os.environ["HERMES_HOME"] = str(pf.settings.home)
    from olo.paths import db_path
    from olo.store import connect, migrate
    conn = connect(db_path())
    migrate(conn)
    return conn


def sync(pf: guard.Preflight, *, max_requests: int, limit: int, client_factory=None,
         mode: str = "A", key_file: str | None = None) -> dict:
    """Both sources once through the real worker, budgeted and recorded."""
    from olo.activation import ActivationRequest, activate
    from olo.adapters.gcal import GcalAdapter
    from olo.adapters.gmail import GmailAdapter
    from olo.google.credentials import load_credentials
    from olo.google.http import GoogleClient
    from olo.runner import run_source

    s = pf.settings
    conn = _conn(pf)
    registry = s.home / "hindsight_port_registry.json"
    act = activate(ActivationRequest(
        business_id=BIZ, enable_commitments=True, enable_projection=registry.exists(),
        bank_id=BANK if registry.exists() else None,
        extract_max_calls=(10 if mode == "B" else 0), deepseek_key_file=key_file,
        projection_max_docs=50, projection_max_prose_docs=(10 if mode == "B" else 0),
        projection_budget_usd=0.5, own_emails=(s.account,),
        hindsight_registry=str(registry) if registry.exists() else None), conn, _now())
    client = RecordingClient((client_factory or GoogleClient)(load_credentials(s.alias)),
                             max_requests=max_requests)
    results = {}
    for adapter in (GmailAdapter({"account": s.alias}, client=client),
                    GcalAdapter({"account": s.alias, "lookback_days": "30"}, client=client)):
        r = run_source(adapter, db_path=None, limit=limit, max_batches=20,
                       commitment=act.commitment, projection=act.projection,
                       own_identifiers=act.own_identifiers)
        results[adapter.source] = {"status": r.status, "error": r.error, "fetched": r.fetched,
                                   "inserted": r.inserted, "commitment": r.commitment,
                                   "projection": {k: v for k, v in r.projection.items() if v}}
    facts = _json(s.evidence / "facts.json", {})
    facts["history_pages"] = max(facts.get("history_pages", 0), 1 + sum(
        1 for c in client.calls if c["path"] == "/users/me/history" and "pageToken" in c["params"]))
    facts["calendar_pages"] = max(facts.get("calendar_pages", 0), 1 + sum(
        1 for c in client.calls if c["path"].endswith("/events") and "pageToken" in c["params"]))
    _write(s.evidence / "facts.json", facts)
    client.save(s.evidence, _now())
    out = {"at": _now(), "requests": client.requests, "budget": max_requests, "runs": results}
    with (s.evidence / "syncs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(out, default=str) + "\n")
    return out


ACTORS = ("operator", "seeder", "real-identity")


def record_action(pf: guard.Preflight, sid: str, note: str, by: str = "operator") -> None:
    known = {sc.id for sc in scenarios.SCENARIOS}
    if sid not in known:
        raise SystemExit(f"unknown scenario {sid!r}; known: {sorted(known)}")
    if by not in ACTORS:
        raise SystemExit(f"--by must be one of {ACTORS}")
    path = pf.settings.evidence / "actions.json"
    actions = _json(path, {})
    actions[sid] = {"at": _now(), "note": note, "by": by}
    _write(path, actions)


def expire_cursor(pf: guard.Preflight, source: str) -> str:
    """Make the stored cursor stale (local olo.db only; nothing at Google)."""
    from olo.store import get_checkpoint
    conn = _conn(pf)
    name = f"{source}_{pf.settings.alias}"
    cur = json.loads(get_checkpoint(name, conn) or "{}")
    if source == "gmail":
        cur["start_history_id"] = "1"
    else:
        for cal in (cur.get("cals") or {}).values():
            if isinstance(cal, dict) and cal.get("phase") == "incr":
                cal["sync_token"] = "CPDAlvWDx70CEPDAlvWDx70CGAU="   # long-expired form
    # As the M2b failure stage does (test_google_live): the cursor row only.
    conn.execute("UPDATE sync_checkpoints SET cursor=? WHERE source=?", (json.dumps(cur), name))
    conn.commit()
    return name


def check(pf: guard.Preflight, only: set[str] | None, mode: str) -> int:
    s = pf.settings
    conn = _conn(pf)
    facts = _json(s.evidence / "facts.json", {})
    ctx = scenarios.Ctx(conn, s.account, s.customer, _json(s.evidence / "actions.json", {}),
                        mode=mode, facts=facts)
    outcomes = scenarios.evaluate(ctx, only)
    verdict, code = scenarios.verdict(outcomes)
    facts.update(ctx.facts)
    _write(s.evidence / "facts.json", facts)
    _write(s.evidence / "results.json", {"at": _now(), "verdict": verdict, "mode": mode,
                                         "outcomes": [o.__dict__ for o in outcomes]})
    lines = [f"# Google acceptance results ({_now()}, mode {mode}): {verdict}", "",
             "| Scenario | Evidence | Result | Details |", "|---|---|---|---|"]
    evidence = {sc.id: sc.evidence for sc in scenarios.SCENARIOS}
    lines += [f"| {o.id} | {evidence[o.id]} | {o.status} | `{json.dumps(o.details, default=str)[:300]}` |"
              for o in outcomes]
    (s.evidence / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for o in outcomes:
        print(f"{o.id:4s} {evidence[o.id]:15s} {o.status:16s} {json.dumps(o.details, default=str)[:140]}")
    print(f"verdict: {verdict}")
    return code


def environment_check(env=os.environ) -> int:
    """Host checks only; the result goes to the evidence directory when set."""
    from google_acceptance import environment
    refusals, facts = environment.check(REPO)
    if env.get("OLO_ACCEPTANCE_EVIDENCE"):
        _write(Path(env["OLO_ACCEPTANCE_EVIDENCE"]) / "environment.json",
               {"at": _now(), "ok": not refusals, "refusals": refusals, "facts": facts})
    for r in refusals:
        print(f"REFUSED: {r}", file=sys.stderr)
    print(json.dumps({"ok": not refusals, "facts": facts}, indent=2))
    return 3 if refusals else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="google_acceptance")
    ap.add_argument("--mode", choices=("A", "B"), default="A",
                    help="B adds paid inference; it needs its own approval and --key-file")
    ap.add_argument("--key-file", help="mode B: owner-only DeepSeek key file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("environment")
    sub.add_parser("preflight")
    sp = sub.add_parser("sync")
    sp.add_argument("--max-requests", type=int, default=400)
    sp.add_argument("--limit", type=int, default=5)
    ac = sub.add_parser("action")
    ac.add_argument("id")
    ac.add_argument("--note", default="")
    ac.add_argument("--by", choices=ACTORS, default="operator",
                    help="who produced the data: the business by hand, the seeder project, or a"
                         " genuine second identity (the only way a second-identity scenario is checked)")
    ex = sub.add_parser("expire-cursor")
    ex.add_argument("source", choices=("gmail", "gcal"))
    sub.add_parser("rerun")
    ck = sub.add_parser("check")
    ck.add_argument("ids", nargs="*")
    sub.add_parser("teardown")
    args = ap.parse_args(argv)
    if args.cmd == "teardown":
        print(TEARDOWN)
        return 0
    if args.cmd == "environment":
        return environment_check()
    if args.mode == "B" and not args.key_file:
        raise SystemExit("mode B needs --key-file (and its own approval)")
    pf = _preflight()
    if args.cmd == "preflight":
        print(json.dumps({"ok": True, "facts": pf.facts}, indent=2))
        return 0
    if args.cmd == "sync":
        print(json.dumps(sync(pf, max_requests=args.max_requests, limit=args.limit,
                              mode=args.mode, key_file=args.key_file), indent=2, default=str))
        return 0
    if args.cmd == "action":
        record_action(pf, args.id, args.note, args.by)
        return 0
    if args.cmd == "expire-cursor":
        print(f"cursor of {expire_cursor(pf, args.source)} made stale")
        record_action(pf, "G10" if args.source == "gmail" else "C7", "expire-cursor")
        return 0
    if args.cmd == "rerun":
        conn = _conn(pf)
        facts = _json(pf.settings.evidence / "facts.json", {})
        facts["signals_before_rerun"] = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
        _write(pf.settings.evidence / "facts.json", facts)
        for _ in range(2):
            sync(pf, max_requests=200, limit=5, mode=args.mode, key_file=args.key_file)
        record_action(pf, "D1", "rerun twice")
        return 0
    return check(pf, set(args.ids) or None, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
