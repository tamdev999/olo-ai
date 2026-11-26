"""`hermes olo hindsight …` and `hermes olo retry` — Hindsight lifecycle and recovery.

``hindsight configure|start|stop|status|supervise|reconcile`` drive
olo.hindsight_service and olo.hindsight_reconcile for this profile's
business.  ``supervise`` runs one pass (for cron, every minute) or
``--loop``.  ``retry`` re-queues work that stopped retrying (olo.requeue).
"""
from __future__ import annotations

import argparse
import json

__all__ = ["register"]


def _conn():
    from olo.hermes_plugin.cli_operator import _conn as conn
    return conn()


def _now_dt():
    from datetime import UTC, datetime
    return datetime.now(UTC)


def _now() -> str:
    from olo.signals import format_utc
    return format_utc(_now_dt())


def _spec(conn):
    from hermes_constants import get_hermes_home
    from olo.hindsight_service import load_spec
    try:
        return load_spec(conn, get_hermes_home())
    except ValueError as exc:
        raise SystemExit(f"olo: {exc}") from None


def _bound(conn, name: str) -> str:
    row = conn.execute("SELECT value FROM olo_bindings WHERE name=?", (name,)).fetchone()
    if row is None:
        raise SystemExit(f"olo: this database has no {name} (run `hermes olo setup` with "
                         "--enable-projection first)")
    return row[0]


def _cmd_configure(args: argparse.Namespace) -> None:
    from hermes_constants import get_hermes_home
    from olo.hindsight_service import configure
    conn = _conn()
    venv = args.venv or str(get_hermes_home() / "olo" / "hindsight-venv")
    configure(conn, args.business_id or _bound(conn, "business_id"), venv, _now(),
              key_file=args.llm_key_file, hf_home=args.hf_home)
    print(f"Hindsight service configured (venv {venv}"
          + (", LLM key from file" if args.llm_key_file else ", no LLM key") + ")")


def _cmd_start(args: argparse.Namespace) -> None:
    from olo.hindsight_service import start
    conn = _conn()
    pid = start(conn, _spec(conn), _now())
    print(f"Hindsight started (pid {pid}); `hermes olo hindsight status` shows when it is healthy")


def _cmd_stop(args: argparse.Namespace) -> None:
    from olo.hindsight_service import stop
    conn = _conn()
    print("stopped" if stop(conn, _now(), reason=args.reason) else "not running")


def _cmd_status(args: argparse.Namespace) -> None:
    from olo.hindsight_reconcile import last_reconciliation
    from olo.hindsight_service import probe, service_status
    conn = _conn()
    out = service_status(conn)
    spec = _spec(conn)
    out["probe"] = probe(spec).status
    out["last_reconciliation"] = last_reconciliation(conn, spec.bank_id)
    print(json.dumps(out, indent=2, sort_keys=True, default=str))


def _cmd_supervise(args: argparse.Namespace) -> None:
    import threading
    from olo.hindsight_service import supervise_once
    conn = _conn()
    spec = _spec(conn)
    stop = threading.Event()
    while True:
        print(f"{_now()} hindsight {spec.business_id}: "
              f"{supervise_once(conn, spec, _now_dt())}", flush=True)
        if not args.loop or stop.wait(args.interval):
            return


def _cmd_reconcile(args: argparse.Namespace) -> None:
    from hermes_constants import get_hermes_home
    from olo.hindsight_reconcile import reconcile_bank
    from olo.m3.hindsight.client import HindsightClient, HindsightConfig
    conn = _conn()
    business = _bound(conn, "business_id")
    cfg = HindsightConfig.for_business(
        business, registry_path=str(get_hermes_home() / "hindsight_port_registry.json"))
    report = reconcile_bank(conn, HindsightClient(cfg), _bound(conn, "hindsight_bank_id"),
                            _now(), trigger="operator")
    print(json.dumps(report.summary(), indent=2, sort_keys=True))
    if report.missing or report.drifted:
        print("Unconfirmed documents are resubmitted by the next projection run.")


def _cmd_retry(args: argparse.Namespace) -> None:
    from olo.requeue import requeue_extractions, requeue_projection
    if not (args.extractions or args.projection):
        raise SystemExit("olo: choose --extractions and/or --projection")
    conn = _conn()
    now = _now()
    if args.extractions:
        print(f"re-queued {requeue_extractions(conn, now)} exhausted extraction(s)"
              " (they may make paid model calls on the next run)")
    if args.projection:
        n = requeue_projection(conn, _bound(conn, "hindsight_bank_id"), now)
        print(f"re-queued {n} document(s) out of retain attempts")


def register(subs) -> None:
    """Add `hindsight` and `retry` to `hermes olo`."""
    hs = subs.add_parser("hindsight", help="Run and recover this business's Hindsight")
    hs_subs = hs.add_subparsers(dest="hindsight_cmd")
    cf = hs_subs.add_parser("configure", help="Record how to start Hindsight")
    cf.add_argument("--business-id", help="defaults to the business this database is bound to")
    cf.add_argument("--venv", help="Hindsight venv (default: $HERMES_HOME/olo/hindsight-venv)")
    cf.add_argument("--llm-key-file",
                    help="owner-only env file with HINDSIGHT_API_LLM_API_KEY (provider deepseek)")
    cf.add_argument("--hf-home", help="local model cache; the server then runs offline")
    cf.set_defaults(func=_cmd_configure)
    hs_subs.add_parser("start", help="Start the server").set_defaults(func=_cmd_start)
    sp = hs_subs.add_parser("stop", help="Stop the server (the supervisor leaves it stopped)")
    sp.add_argument("--reason", default="operator")
    sp.set_defaults(func=_cmd_stop)
    hs_subs.add_parser("status", help="Supervisor state, probe and last reconciliation"
                       ).set_defaults(func=_cmd_status)
    sv = hs_subs.add_parser("supervise", help="One supervision pass (or --loop)")
    sv.add_argument("--loop", action="store_true")
    sv.add_argument("--interval", type=float, default=30.0)
    sv.set_defaults(func=_cmd_supervise)
    hs_subs.add_parser("reconcile", help="Compare the bank with olo.db now"
                       ).set_defaults(func=_cmd_reconcile)

    rt = subs.add_parser("retry", help="Re-queue work that stopped retrying")
    rt.add_argument("--extractions", action="store_true", help="exhausted commitment extractions")
    rt.add_argument("--projection", action="store_true", help="documents out of retain attempts")
    rt.set_defaults(func=_cmd_retry)

