"""Olo AI — `hermes olo` CLI subcommands (M1 + M2).

§6.1, §7.1, §7.2: setup, status, google connect/status/disconnect,
checkpoint reset.

No side effects at import time.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# ── Validation patterns (must stay in sync with olo.signals) ─────────────────
_SOURCE_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_OPTION_KEY_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_ALIAS_RE = re.compile(r"^[a-z0-9]{1,16}$")

# ── Gmail/Calendar schedule strings (§7.1) ────────────────────────────────────
_GMAIL_SCHEDULE = "every 5m"
_GCAL_SCHEDULE = "every 15m"


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

    # Connected Google accounts
    try:
        from olo.google.credentials import list_accounts, load_credentials, email_hash
        accounts = list_accounts()
        if accounts:
            print("  Google accounts:")
            for alias in accounts:
                try:
                    creds = load_credentials(alias)
                    scopes_short = []
                    if creds.has_gmail_scope():
                        scopes_short.append("gmail.metadata")
                    if creds.has_calendar_scope():
                        scopes_short.append("calendar")
                    print(
                        f"    {alias}: email_hash={email_hash(creds.email_address)[:12]}…"
                        f"  scopes={scopes_short}"
                    )
                except Exception as e:
                    print(f"    {alias}: ERROR ({e})")
    except ImportError:
        pass

    if not db.exists():
        print("  (database not yet initialised — run `hermes olo setup --source <name>`)")
        return

    conn = connect(db)
    migrate(conn)

    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    version = row["version"] if row else "?"
    print(f"  Schema:  v{version}")

    sources = conn.execute("SELECT DISTINCT source FROM sync_checkpoints").fetchall()
    if not sources:
        print("  Sources: (none)")
    else:
        for s_row in sources:
            src = s_row["source"]
            cp = conn.execute(
                "SELECT cursor FROM sync_checkpoints WHERE source=?", (src,)
            ).fetchone()
            cursor = cp["cursor"] if cp else "None"
            # Show phase for Google sources
            phase_str = _cursor_phase_str(cursor)
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
            print(f"    cursor={phase_str}  signals={n}  {lease_str}")

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


def _cursor_phase_str(cursor: str) -> str:
    """Human-readable phase description for a cursor string."""
    if cursor is None or cursor == "None":
        return "bootstrap"
    try:
        import json
        d = json.loads(cursor)
        phase = d.get("phase", "?")
        if phase == "full":
            return f"full(time_min={d.get('time_min','?')[:10]})"
        if phase == "incr":
            tok = d.get("sync_token", "?")[:12]
            return f"incr(token={tok}…)"
        return phase
    except Exception:
        # Gmail: plain historyId string
        return f"historyId={cursor}"


# ── Setup ─────────────────────────────────────────────────────────────────────

def _cmd_setup(args: argparse.Namespace) -> None:
    """Migrate DB, install shim script, print cron create command."""
    from hermes_constants import get_hermes_home
    from olo.adapters import ADAPTERS, _ensure_registry
    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now

    source: str = args.source

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

    # For gmail/gcal, verify the account is connected and has the required scope
    alias = options.get("account", "")
    if source in ("gmail", "gcal") and alias:
        try:
            from olo.google.credentials import load_credentials
            creds = load_credentials(alias)
            if source == "gmail" and not creds.has_gmail_scope():
                print(
                    f"olo setup: account {alias!r} has no gmail.metadata scope. "
                    f"Run: hermes olo google connect --account {alias}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if source == "gcal" and not creds.has_calendar_scope():
                print(
                    f"olo setup: account {alias!r} has no calendar scope. "
                    f"Run: hermes olo google connect --account {alias}",
                    file=sys.stderr,
                )
                sys.exit(1)
        except FileNotFoundError:
            print(
                f"olo setup: account {alias!r} is not connected. "
                f"Run: hermes olo google connect --account {alias}",
                file=sys.stderr,
            )
            sys.exit(1)
    elif source in ("gmail", "gcal") and not alias:
        print(
            f"olo setup: --source {source} requires --option account=<alias>",
            file=sys.stderr,
        )
        sys.exit(1)

    db = db_path()
    conn = connect(db)
    migrate(conn)

    scripts_dir = get_hermes_home() / "scripts"
    scripts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    # §7.1: shim name includes account for gmail/gcal
    if source in ("gmail", "gcal") and alias:
        shim_name = f"olo_monitor_{source}_{alias}.py"
        cron_name = f"olo-{source}-{alias}"
    else:
        shim_name = f"olo_monitor_{source}.py"
        cron_name = f"olo-{source}"

    shim_path = scripts_dir / shim_name

    if shim_path.exists() and not getattr(args, "force", False):
        print(
            f"olo setup: {shim_path} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    import os
    repo_root = str(Path(__file__).parent.parent.parent)
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
        repo_root=repr(repo_root),
        argv_literal=argv_literal,
    )

    shim_path.write_text(shim_content, encoding="utf-8")
    shim_path.chmod(0o700)

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

    # §7.1: schedule strings
    schedule_str = _GMAIL_SCHEDULE if source == "gmail" else (
        _GCAL_SCHEDULE if source == "gcal" else "every 15m"
    )

    print(f"olo setup: DB migrated, shim installed at {shim_path}")
    print()
    print("Add cron job:")
    print(
        f"  hermes cron create \"{schedule_str}\" --name {cron_name}"
        f" --script {shim_name} --no-agent --deliver local"
    )


# ── Google connect ─────────────────────────────────────────────────────────────

def _cmd_google_connect(args: argparse.Namespace) -> None:
    """OAuth connect for a Google account (initial connect or --upgrade to M2b scopes)."""
    alias: str = args.account
    client_secret_file: str = args.client_secret_file
    manual: bool = getattr(args, "manual", False)
    prod: bool = getattr(args, "production", False)
    upgrade: bool = getattr(args, "upgrade", False)
    revoke_first: bool = getattr(args, "revoke_first", False)

    if revoke_first and not upgrade:
        print("olo google connect: --revoke-first requires --upgrade", file=sys.stderr)
        sys.exit(1)

    if not _ALIAS_RE.match(alias):
        print(
            f"olo google connect: invalid alias {alias!r}: "
            f"must match ^[a-z0-9]{{1,16}}$",
            file=sys.stderr,
        )
        sys.exit(1)

    # Load client secrets
    import json as _json
    try:
        raw = Path(client_secret_file).read_text(encoding="utf-8")
        secret_data = _json.loads(raw)
    except Exception as e:
        print(f"olo google connect: cannot read client secret file: {e}", file=sys.stderr)
        sys.exit(1)

    # Support both "installed" and "web" key layouts
    app = secret_data.get("installed") or secret_data.get("web") or secret_data
    client_id = app.get("client_id", "")
    client_secret = app.get("client_secret", "")
    if not client_id or not client_secret:
        print(
            "olo google connect: client secret file missing client_id or client_secret",
            file=sys.stderr,
        )
        sys.exit(1)

    oauth_status = "production" if prod else "testing"

    # ── M2b upgrade path ──────────────────────────────────────────────────────
    if upgrade:
        from olo.google.oauth import upgrade_credentials

        def _connect_fn(scopes):
            """Obtain consent for *scopes*; return (code, api_base_override)."""
            from olo.google.oauth import (
                build_auth_url, connect_loopback, await_loopback_code,
                parse_manual_redirect,
            )
            if manual:
                import socket
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                redirect_uri = f"http://127.0.0.1:{port}/"
                auth_url, pending = build_auth_url(client_id, redirect_uri, scopes)
                print(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
                print("After authorising, paste the full redirect URL here:")
                try:
                    pasted = input("> ").strip()
                except (EOFError, KeyboardInterrupt):
                    raise RuntimeError("Upgrade cancelled by user.")
                code = parse_manual_redirect(pasted, pending)
                if code is None:
                    raise RuntimeError("Authorisation failed or state mismatch.")
                return code, None
            else:
                auth_url, pending, port = connect_loopback(
                    client_id, client_secret, alias, scopes,
                )
                print(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
                print(
                    f"Waiting for redirect on http://127.0.0.1:{port}/ (300s timeout)..."
                )
                code = await_loopback_code(pending, port)
                if code is None:
                    raise RuntimeError("Upgrade consent timed out or access denied.")
                return code, None

        result = upgrade_credentials(
            alias,
            revoke_first=revoke_first,
            connect_fn=_connect_fn,
            oauth_publishing_status=oauth_status,
        )

        if result.success:
            print(f"\nUpgrade complete for account {alias!r}.")
            print("  scope_profile = full_v1")
            print("  Existing checkpoints preserved — no reset needed.")
            if oauth_status == "testing":
                print("  Warning: Testing-mode tokens expire 7 days after consent.")
        else:
            if result.scope_union and result.needs_revoke_first:
                print(
                    f"\nolo google connect --upgrade: Google returned both old and new scopes "
                    f"in the same grant (scope_union).\n"
                    f"Rerun with --revoke-first to clear the existing project grant first:\n\n"
                    f"  hermes olo google connect --account {alias} "
                    f"--client-secret-file {client_secret_file} "
                    f"--upgrade --revoke-first\n",
                    file=sys.stderr,
                )
            else:
                print(
                    f"olo google connect --upgrade: failed — {result.reason}",
                    file=sys.stderr,
                )
            sys.exit(1)
        return

    # ── Standard initial connect path ─────────────────────────────────────────
    # P1-C: new accounts connect with M2b scopes (REQUIRED_SCOPES_V2) by default.
    # Warn if the account already has credentials — connecting again will update scopes.
    from olo.google.credentials import REQUIRED_SCOPES_V2, credential_lock, save_credentials
    from olo.google.oauth import (
        connect_loopback, await_loopback_code,
        build_auth_url, parse_manual_redirect,
        exchange_code,
    )

    scopes = REQUIRED_SCOPES_V2

    # Warn if account already connected (connecting again replaces existing creds)
    try:
        from olo.google.credentials import load_credentials
        existing = load_credentials(alias)
        if existing.is_legacy_profile():
            print(
                f"\nNote: account {alias!r} already has legacy (M2) credentials. "
                "Connecting again will upgrade to M2b scopes. "
                "Use --upgrade to preserve checkpoints.",
            )
        else:
            print(
                f"\nNote: account {alias!r} is already connected. Connecting again "
                "will replace the existing credentials.",
            )
    except FileNotFoundError:
        pass  # new account — no warning needed

    if manual:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        redirect_uri = f"http://127.0.0.1:{port}/"
        auth_url, pending = build_auth_url(client_id, redirect_uri, scopes)
        print(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
        print("After authorising, paste the full redirect URL here:")
        try:
            pasted = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.", file=sys.stderr)
            sys.exit(1)
        code = parse_manual_redirect(pasted, pending)
        if code is None:
            print("olo google connect: authorisation failed or state mismatch.", file=sys.stderr)
            sys.exit(1)
    else:
        auth_url, pending, port = connect_loopback(client_id, client_secret, alias, scopes)
        print(f"\nOpen this URL in your browser:\n\n  {auth_url}\n")
        print(f"Waiting for redirect on http://127.0.0.1:{port}/ (300s timeout)...")
        code = await_loopback_code(pending, port)
        if code is None:
            print("olo google connect: timeout or access denied.", file=sys.stderr)
            sys.exit(1)

    try:
        creds = exchange_code(
            code, pending, client_id, client_secret, alias,
            oauth_publishing_status=oauth_status,
        )
    except ValueError as e:
        print(f"olo google connect: {e}", file=sys.stderr)
        sys.exit(1)

    with credential_lock(alias):
        save_credentials(creds)

    # Audit
    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now
    from olo.google.credentials import email_hash
    db = db_path()
    conn = connect(db)
    migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _audit(conn, "cli:google", "google.connected", {
            "alias": alias,
            "email_hash": email_hash(creds.email_address),
            "scopes": sorted(creds.granted_scopes),
        }, now=utc_now())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
    conn.close()

    scopes_short = []
    if creds.has_gmail_scope():
        scopes_short.append("gmail.metadata")
    if creds.has_calendar_scope():
        scopes_short.append("calendar")
    print(f"\nConnected account {alias!r} (scopes: {scopes_short})")
    if oauth_status == "testing":
        print("  Warning: Testing-mode tokens expire 7 days after consent.")
        print("  Run 'hermes olo google connect' again weekly to reconnect.")


def _cmd_google_status(args: argparse.Namespace) -> None:
    """Show connected Google accounts."""
    check: bool = getattr(args, "check", False)

    try:
        from olo.google.credentials import list_accounts, load_credentials, email_hash
        from datetime import datetime, UTC, timedelta
    except ImportError:
        print("olo google status: google module not available", file=sys.stderr)
        sys.exit(1)

    accounts = list_accounts()
    if not accounts:
        print("No connected Google accounts.")
        return

    for alias in accounts:
        try:
            creds = load_credentials(alias)
        except Exception as e:
            print(f"  {alias}: ERROR loading credentials: {e}")
            continue

        scopes_short = []
        if creds.has_gmail_scope():
            scopes_short.append(
                "gmail.readonly" if creds.has_gmail_readonly_scope() else "gmail.metadata"
            )
        if creds.has_calendar_scope():
            scopes_short.append("calendar")

        profile_str = creds.scope_profile or "legacy_v1"
        print(f"  {alias}:")
        print(f"    email_hash:    {email_hash(creds.email_address)}")
        print(f"    scopes:        {scopes_short}")
        print(f"    scope_profile: {profile_str}")
        print(f"    status:        {creds.oauth_publishing_status}")

        # Expiry warning for testing mode
        if creds.oauth_publishing_status == "testing":
            expiry = creds.refresh_token_issued_at + timedelta(days=7)
            now = datetime.now(UTC)
            remaining = (expiry - now).total_seconds()
            if remaining <= 0:
                print("    expiry:     EXPIRED — reconnect now")
            elif remaining < 48 * 3600:
                hrs = remaining / 3600
                print(f"    expiry:     WARNING — expires in {hrs:.1f}h (reconnect soon)")
            else:
                days = remaining / 86400
                print(f"    expiry:     {days:.1f} days remaining")

        if check:
            try:
                from olo.google.oauth import refresh_access_token
                from olo.google.credentials import GoogleAuthRevoked
                refresh_access_token(alias)
                print("    live check: OK (token refreshed)")
            except GoogleAuthRevoked as e:
                print(f"    live check: FAILED — {e}")
            except Exception as e:
                print(f"    live check: ERROR — {e}")


def _cmd_google_disconnect(args: argparse.Namespace) -> None:
    """Revoke and remove credentials for an account."""
    alias: str = args.account

    try:
        from olo.google.credentials import (
            load_credentials, remove_credentials, credential_lock,
        )
        from olo.google.oauth import refresh_access_token, revoke_token
    except ImportError:
        print("olo google disconnect: google module not available", file=sys.stderr)
        sys.exit(1)

    try:
        with credential_lock(alias):
            creds = load_credentials(alias)
            # Revoke (best effort)
            try:
                token = refresh_access_token(alias)
                revoke_token(token, api_base_override=creds.api_base_override)
            except Exception:
                pass
            remove_credentials(alias)
    except FileNotFoundError:
        print(f"olo google disconnect: account {alias!r} not connected", file=sys.stderr)
        sys.exit(1)

    # Audit
    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now
    from olo.google.credentials import email_hash
    db = db_path()
    conn = connect(db)
    migrate(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _audit(conn, "cli:google", "google.disconnected", {"alias": alias}, now=utc_now())
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
    conn.close()

    print(f"Disconnected account {alias!r}.")


# ── Checkpoint reset ───────────────────────────────────────────────────────────

def _cmd_checkpoint_reset(args: argparse.Namespace) -> None:
    """Reset sync checkpoint for a source (§7.2)."""
    source: str = args.source
    confirm: bool = getattr(args, "confirm", False)

    if not confirm:
        print(
            "olo checkpoint reset: requires --confirm flag to proceed.",
            file=sys.stderr,
        )
        sys.exit(1)

    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now
    import hashlib as _hashlib
    import sqlite3

    db = db_path()
    conn = connect(db)
    migrate(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Refuse if a live lease exists
        lease = conn.execute(
            "SELECT run_id, expires_at FROM source_leases WHERE source=?",
            (source,),
        ).fetchone()
        if lease:
            from datetime import datetime, UTC
            expires = datetime.fromisoformat(lease["expires_at"])
            if expires > datetime.now(UTC):
                conn.execute("ROLLBACK")
                print(
                    f"olo checkpoint reset: source {source!r} has an active lease "
                    f"(run_id={lease['run_id']}, expires={lease['expires_at']}). "
                    f"Wait for it to expire or finish.",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Get old cursor for audit
        cp_row = conn.execute(
            "SELECT cursor FROM sync_checkpoints WHERE source=?", (source,)
        ).fetchone()
        old_cursor = cp_row["cursor"] if cp_row else None
        old_cursor_hash = (
            _hashlib.sha256(old_cursor.encode()).hexdigest()
            if old_cursor else None
        )

        # Delete checkpoint
        conn.execute("DELETE FROM sync_checkpoints WHERE source=?", (source,))

        _audit(conn, "cli:checkpoint", "checkpoint.reset", {
            "source": source,
            "old_cursor_hash": old_cursor_hash,
            "reason": "operator",
        }, now=utc_now())
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    conn.close()
    print(f"Checkpoint reset for source {source!r}. Next run will re-sync from scratch.")


# ── Backfill (P1-A) ───────────────────────────────────────────────────────────

def _cmd_backfill(args: argparse.Namespace) -> None:
    """Trigger Gmail full-content backfill (P1-A).

    Injects a backfill cursor so the next Gmail cron run scans all messages
    via messages.list and stores email.content blobs.  The existing historyId
    checkpoint is preserved as resume_history_id so incremental sync resumes
    normally after backfill completes.

    Mode is auto-detected if not supplied:
      - 'resume' if a numeric historyId checkpoint exists
      - 'gap'    if signals exist but no historyId (history expired)
      - 'bootstrap' otherwise (new account)
    """
    source: str = args.source
    mode: str | None = getattr(args, "mode", None)

    if not source.startswith("gmail_"):
        print(
            f"olo backfill: backfill is only supported for gmail_* sources, "
            f"got {source!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now, get_checkpoint
    import json as _json

    db = db_path()
    conn = connect(db)
    migrate(conn)

    current_cursor = get_checkpoint(source, conn)

    # Auto-detect mode
    if mode is None:
        if current_cursor is not None:
            try:
                parsed = _json.loads(current_cursor)
                if isinstance(parsed, dict) and parsed.get("phase") == "backfill":
                    print(
                        f"olo backfill: source {source!r} already has a backfill cursor "
                        f"(mode={parsed.get('mode')!r}). Nothing to do."
                    )
                    conn.close()
                    return
            except (ValueError, _json.JSONDecodeError):
                pass  # plain historyId string — not a JSON cursor
            # Numeric historyId → resume mode
            if current_cursor is not None and current_cursor.lstrip("-").isdigit():
                mode = "resume"
            else:
                mode = "bootstrap"
        else:
            mode = "bootstrap"

    resume_history_id = current_cursor if (current_cursor and current_cursor.lstrip("-").isdigit()) else "0"

    backfill_cursor = _json.dumps({
        "phase": "backfill",
        "mode": mode,
        "after": 0,
        "page_token": None,
        "resume_history_id": resume_history_id,
    })

    conn.execute("BEGIN IMMEDIATE")
    try:
        if current_cursor is None:
            # Insert new checkpoint
            # We need a run_id reference; create a synthetic one
            import secrets as _sec
            fake_run_id = _sec.token_hex(16)
            conn.execute(
                "INSERT OR IGNORE INTO ingest_runs(id,source,owner,started_at,status)"
                " VALUES (?,?,'cli',datetime('now'),'succeeded')",
                (fake_run_id, source),
            )
            conn.execute(
                "INSERT INTO sync_checkpoints(source,cursor,updated_at,run_id)"
                " VALUES (?,?,datetime('now'),?)",
                (source, backfill_cursor, fake_run_id),
            )
        else:
            conn.execute(
                "UPDATE sync_checkpoints SET cursor=?,updated_at=datetime('now') WHERE source=?",
                (backfill_cursor, source),
            )
        _audit(conn, "cli:backfill", "backfill.started", {
            "source": source,
            "mode": mode,
            "resume_history_id": resume_history_id,
        }, now=utc_now())
        conn.execute("COMMIT")
    except Exception:
        try: conn.execute("ROLLBACK")
        except Exception: pass
        raise

    conn.close()
    print(
        f"Backfill cursor set for source {source!r} (mode={mode!r}). "
        f"Run the Gmail cron job to start backfill."
    )


# ── Purge content (P1-A) ──────────────────────────────────────────────────────

def _cmd_purge_content(args: argparse.Namespace) -> None:
    """Remove all stored content blobs for a source (P1-A).

    Deletes rows from content_blobs and clears content_refs from signal payloads.
    Does NOT remove the signal rows themselves.
    """
    source: str = args.source
    confirm: bool = getattr(args, "confirm", False)

    if not confirm:
        print(
            "olo purge-content: requires --confirm flag. "
            "This operation is irreversible.",
            file=sys.stderr,
        )
        sys.exit(1)

    from olo.paths import db_path
    from olo.store import connect, migrate, _audit, utc_now

    db = db_path()
    conn = connect(db)
    migrate(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM content_blobs WHERE source=?", (source,)
        ).fetchone()[0]
        conn.execute("DELETE FROM content_blobs WHERE source=?", (source,))
        _audit(conn, "cli:purge", "content.purged", {
            "source": source,
            "blobs_removed": n,
        }, now=utc_now())
        conn.execute("COMMIT")
    except Exception:
        try: conn.execute("ROLLBACK")
        except Exception: pass
        raise

    conn.close()
    print(f"Purged {n} content blob(s) for source {source!r}.")


# ── Shim template ─────────────────────────────────────────────────────────────

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
    setup_p.add_argument("--source", required=True, help="Adapter name")
    setup_p.add_argument(
        "--option", metavar="key=value", action="append",
        help="Adapter option (may be repeated)",
    )
    setup_p.add_argument("--force", action="store_true", help="Overwrite existing shim")
    setup_p.set_defaults(func=_cmd_setup)

    # google (sub-group)
    google_p = subs.add_parser("google", help="Google account management")
    google_subs = google_p.add_subparsers(dest="google_cmd")

    # google connect
    gconn = google_subs.add_parser("connect", help="Connect a Google account")
    gconn.add_argument("--account", required=True, help="Account alias (a-z0-9, max 16)")
    gconn.add_argument(
        "--client-secret-file", required=True, dest="client_secret_file",
        help="Path to Google OAuth client secret JSON",
    )
    gconn.add_argument(
        "--manual", action="store_true",
        help="Paste redirect URL manually (headless hosts)",
    )
    gconn.add_argument(
        "--production", action="store_true",
        help="Mark as production OAuth status (not testing)",
    )
    gconn.add_argument(
        "--upgrade", action="store_true",
        help=(
            "Upgrade from M2 (gmail.metadata) to M2b (gmail.readonly + "
            "calendar.events.readonly + calendar.calendarlist.readonly) scopes. "
            "Preserves existing checkpoints. Use --revoke-first if Google returns "
            "scope_union (old and new scopes granted together)."
        ),
    )
    gconn.add_argument(
        "--revoke-first", action="store_true", dest="revoke_first",
        help=(
            "Only with --upgrade: revoke the existing token before requesting new "
            "scopes, then poll until invalid_grant is confirmed. Use when a plain "
            "--upgrade attempt returns a scope_union error."
        ),
    )
    gconn.set_defaults(func=_cmd_google_connect)

    # google status
    gstatus = google_subs.add_parser("status", help="Show connected Google accounts")
    gstatus.add_argument(
        "--check", action="store_true",
        help="Perform a live token refresh check",
    )
    gstatus.set_defaults(func=_cmd_google_status)

    # google disconnect
    gdisc = google_subs.add_parser("disconnect", help="Revoke and remove a Google account")
    gdisc.add_argument("--account", required=True, help="Account alias")
    gdisc.set_defaults(func=_cmd_google_disconnect)

    # checkpoint reset
    cpreset = subs.add_parser("checkpoint", help="Sync checkpoint operations")
    cpreset_subs = cpreset.add_subparsers(dest="checkpoint_cmd")
    cpreset_reset = cpreset_subs.add_parser("reset", help="Reset sync checkpoint for a source")
    cpreset_reset.add_argument("--source", required=True, help="Source name, e.g. gmail_acme")
    cpreset_reset.add_argument(
        "--confirm", action="store_true",
        help="Confirm the reset (required)",
    )
    cpreset_reset.set_defaults(func=_cmd_checkpoint_reset)

    # backfill (P1-A)
    backfill_p = subs.add_parser(
        "backfill",
        help="Trigger Gmail full-content backfill for an account",
    )
    backfill_p.add_argument(
        "--source", required=True,
        help="Gmail source name, e.g. gmail_acme",
    )
    backfill_p.add_argument(
        "--mode",
        choices=("bootstrap", "resume", "gap"),
        default=None,
        help=(
            "Backfill mode (default: auto-detected from checkpoint). "
            "bootstrap=scan all messages; resume=continue from existing checkpoint; "
            "gap=acknowledge an unrecoverable history gap then scan forward."
        ),
    )
    backfill_p.set_defaults(func=_cmd_backfill)

    # purge-content (P1-A)
    purge_p = subs.add_parser(
        "purge-content",
        help="Remove all stored content blobs for a source",
    )
    purge_p.add_argument(
        "--source", required=True,
        help="Source name, e.g. gmail_acme",
    )
    purge_p.add_argument(
        "--confirm", action="store_true",
        help="Confirm the purge (required; this operation is irreversible)",
    )
    purge_p.set_defaults(func=_cmd_purge_content)


def handler(args: argparse.Namespace) -> None:
    """Dispatch to the chosen sub-command."""
    # Handle nested sub-parsers
    if getattr(args, "olo_cmd", None) == "google":
        func = getattr(args, "func", None)
        if func is None:
            print("usage: hermes olo google {connect,status,disconnect}", file=sys.stderr)
            sys.exit(1)
        func(args)
        return
    if getattr(args, "olo_cmd", None) == "checkpoint":
        func = getattr(args, "func", None)
        if func is None:
            print("usage: hermes olo checkpoint {reset}", file=sys.stderr)
            sys.exit(1)
        func(args)
        return
    func = getattr(args, "func", None)
    if func is None:
        print("usage: hermes olo {status,setup,google,checkpoint,backfill,purge-content}", file=sys.stderr)
        sys.exit(1)
    func(args)
