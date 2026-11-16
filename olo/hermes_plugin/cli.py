"""Olo AI — `hermes olo status` and `hermes olo setup` subcommands.

§6.1: CLI-only plugin surface for M1.
No side effects at import time.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# ── Validation patterns (must stay in sync with olo.signals) ─────────────────
_SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_OPTION_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")


# ── Status ────────────────────────────────────────────────────────────────────

def _cmd_status(args: argparse.Namespace) -> None:
    """Print DB path, schema version, checkpoint, lease, signal counts, last 5 runs."""
    from hermes_constants import get_hermes_home
    from olo.paths import db_path
    from olo.store import connect, migrate

    home = get_hermes_home()
    db = db_path()
    print("Olo AI status")
    print(f"  Home:    {home}")
    print(f"  DB:      {db}")

    if not db.exists():
        print("  (database not yet initialised — run `hermes olo setup --source <name>`)")
        return

    conn = connect(db)
    migrate(conn)

    # Schema version
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    version = row["version"] if row else "?"
    print(f"  Schema:  v{version}")

    # Sources
    sources = conn.execute(
        "SELECT DISTINCT source FROM sync_checkpoints"
    ).fetchall()
    if not sources:
        print("  Sources: (none)")
    else:
        for s_row in sources:
            src = s_row["source"]
            cp = conn.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source=?", (src,)
            ).fetchone()
            cursor = cp["cursor"] if cp else "None"
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM signals WHERE source=?", (src,)
            ).fetchone()["n"]
            lease = conn.execute(
                "SELECT run_id, owner, expires_at FROM source_leases WHERE source=?",
                (src,),
            ).fetchone()
            lease_str = (
                f"lease held by {lease['run_id']} ({lease['owner']}) until {lease['expires_at']}"
                if lease else "no lease"
            )
            print(f"  Source:  {src}")
            print(f"    cursor={cursor}  signals={n}  {lease_str}")

    # Last 5 runs
    runs = conn.execute(
        "SELECT id, source, status, started_at, batches, inserted, error"
        " FROM ingest_runs ORDER BY started_at DESC LIMIT 5"
    ).fetchall()
    if runs:
        print("  Last runs:")
        for r in runs:
            err = f"  error={r['error']!r}" if r["error"] else ""
            print(
                f"    [{r['status']:12s}] {r['source']} {r['id'][:8]}…"
                f" batches={r['batches']} inserted={r['inserted']}{err}"
            )

    conn.close()


# ── Setup ─────────────────────────────────────────────────────────────────────

def _cmd_setup(args: argparse.Namespace) -> None:
    """Migrate DB, install shim script, print cron create command."""
    from hermes_constants import get_hermes_home
    from olo.adapters import ADAPTERS, _ensure_registry
    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now

    source: str = args.source

    # ── Validate source before any side effects ───────────────────────────────
    if not _SOURCE_RE.match(source):
        print(
            f"olo setup: invalid --source {source!r}: "
            f"must match ^[a-z0-9_]{{1,32}}$",
            file=sys.stderr,
        )
        sys.exit(1)
    _ensure_registry()
    if source not in ADAPTERS:
        known = ", ".join(sorted(ADAPTERS))
        print(
            f"olo setup: unknown --source {source!r}. Known adapters: {known}",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Parse and validate options ────────────────────────────────────────────
    options: dict[str, str] = {}
    for item in (args.option or []):
        if "=" not in item:
            print(f"olo setup: bad --option {item!r} (expected key=value)", file=sys.stderr)
            sys.exit(1)
        k, _, v = item.partition("=")
        if not _OPTION_KEY_RE.match(k):
            print(
                f"olo setup: invalid option key {k!r}: "
                f"must match ^[a-z0-9_]{{1,64}}$",
                file=sys.stderr,
            )
            sys.exit(1)
        options[k] = v

    # Migrate
    db = db_path()
    conn = connect(db)
    migrate(conn)

    # Install shim
    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    shim_name = f"olo_monitor_{source}.py"
    shim_path = scripts_dir / shim_name

    if shim_path.exists() and not getattr(args, "force", False):
        print(
            f"olo setup: {shim_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Render shim from template.
    # Build the argument list as a safe Python literal using repr() so that
    # source and option values can NEVER inject code regardless of their content.
    import os
    repo_root = str(Path(__file__).parent.parent.parent)

    # Build the argument list as a safe Python literal using repr() so that
    # every element (flags AND values) is a quoted string literal in the
    # generated shim. repr() ensures injected Python characters are inert.
    parts: list[str] = [repr("--source"), repr(source)]
    for k, v in options.items():
        parts += [repr("--option"), repr(f"{k}={v}")]
    argv_literal = "[" + ", ".join(parts) + "]"

    tmpl_path = Path(__file__).parent.parent / "cron_scripts" / "olo_monitor.py.tmpl"
    if tmpl_path.exists():
        tmpl = tmpl_path.read_text(encoding="utf-8")
    else:
        tmpl = _DEFAULT_SHIM_TEMPLATE

    shim_content = tmpl.format(
        repo_root=repr(repo_root),   # safe: repr() wraps the string
        argv_literal=argv_literal,
    )

    shim_path.write_text(shim_content, encoding="utf-8")
    shim_path.chmod(0o700)

    # Audit
    conn.execute("BEGIN IMMEDIATE")
    try:
        _audit(conn, "cli:setup", "setup.completed", {
            "source": source,
            "options": options,
            "shim": str(shim_path),
        }, now=utc_now())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    conn.close()

    print(f"olo setup: DB migrated, shim installed at {shim_path}")
    print()
    print("Add cron job:")
    print(
        f"  hermes cron create \"every 15m\" --name olo-{source}"
        f" --script {shim_name} --no-agent --deliver local"
    )


_DEFAULT_SHIM_TEMPLATE = """\
#!/usr/bin/env python3
# Olo AI monitor shim — installed by `hermes olo setup`.
# Do NOT edit directly; re-run setup to regenerate.
# argv and repo_root are Python literals produced by repr() — injection-safe.
import sys

try:
    import olo.runner as _r
except ImportError:
    # Fallback: add repo root so dev installs work without the package installed.
    _repo_root = {repo_root}
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    import olo.runner as _r  # type: ignore[no-redef]

raise SystemExit(_r.main({argv_literal}))
"""


# ── Argparse setup ────────────────────────────────────────────────────────────

def setup_parser(subparser: argparse.ArgumentParser) -> None:
    """Wire `hermes olo <sub>` subcommands onto *subparser*."""
    subs = subparser.add_subparsers(dest="olo_cmd")

    # status
    status_p = subs.add_parser("status", help="Show Olo pipeline status")
    status_p.set_defaults(func=_cmd_status)

    # setup
    setup_p = subs.add_parser("setup", help="Initialise Olo pipeline for a source")
    setup_p.add_argument("--source", required=True, help="Adapter name, e.g. synthetic")
    setup_p.add_argument(
        "--option", metavar="key=value", action="append",
        help="Adapter option (may be repeated)",
    )
    setup_p.add_argument(
        "--force", action="store_true",
        help="Overwrite existing shim script",
    )
    setup_p.set_defaults(func=_cmd_setup)


def handler(args: argparse.Namespace) -> None:
    """Dispatch to the chosen sub-command."""
    func = getattr(args, "func", None)
    if func is None:
        print("usage: hermes olo {status,setup}", file=sys.stderr)
        sys.exit(1)
    func(args)
