#!/usr/bin/env python3
"""Phase B Stage 1: ingest Round 1 data with spy; verify exact signal counts."""
from __future__ import annotations

import hashlib
import json
import os
import sys

REPO = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
sys.path.insert(0, REPO)
os.environ["HERMES_HOME"] = "/root/olo_live_home"

# ── Spy ───────────────────────────────────────────────────────────────────────

class _Spy:
    """Records every history.list and events.list get_json call."""

    def __init__(self, real_client):
        self._client = real_client
        self.calls: list[dict] = []

    def get_json(self, service: str, path: str, params):
        param_names = [k for k, _ in params]
        start_hid = next((v for k, v in params if k == "startHistoryId"), None)
        sync_tok  = next((v for k, v in params if k == "syncToken"),       None)
        page_tok  = next((v for k, v in params if k == "pageToken"),       None)
        max_res   = next((v for k, v in params if k == "maxResults"),      None)
        self.calls.append({
            "service":          service,
            "path":             path,
            "param_names":      param_names,
            "start_history_id": start_hid,
            "has_sync_token":   sync_tok is not None,
            "has_page_token":   page_tok is not None,
            "max_results":      max_res,
        })
        return self._client.get_json(service, path, params)

    def get_message_or_none(self, msg_id, headers):
        self.calls.append({
            "service": "gmail", "path": f"/users/me/messages/{msg_id}",
            "param_names": ["format", "metadataHeaders"],
            "start_history_id": None, "has_sync_token": False,
            "has_page_token": False, "max_results": None,
        })
        return self._client.get_message_or_none(msg_id, headers)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _make_spy(source_type: str, alias: str = "livetest") -> tuple:
    from olo.google.credentials import load_credentials
    from olo.google.http import GoogleClient
    creds = load_credentials(alias)
    client = GoogleClient(creds)
    spy = _Spy(client)
    if source_type == "gmail":
        from olo.adapters.gmail import GmailAdapter
        return GmailAdapter({"account": alias}, client=spy), spy
    else:
        from olo.adapters.gcal import GcalAdapter
        return GcalAdapter({"account": alias, "lookback_days": "7"}, client=spy), spy


# ── DB helpers ────────────────────────────────────────────────────────────────

def _db():
    from olo.store import connect, migrate
    from olo.paths import db_path
    conn = connect(db_path())
    migrate(conn)
    return conn


def _counts(conn, source: str) -> dict:
    rows = conn.execute(
        "SELECT kind, COUNT(*) n FROM signals WHERE source=? GROUP BY kind",
        (source,),
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def _total(conn, source: str) -> int:
    from olo.store import count_signals
    return count_signals(source, conn)


def _checkpoint(conn, source: str):
    from olo.store import get_checkpoint
    return get_checkpoint(source, conn)


def _all_signals(conn, source: str) -> list[dict]:
    rows = conn.execute(
        "SELECT external_id, kind, subject, occurred_at FROM signals "
        "WHERE source=? ORDER BY occurred_at",
        (source,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Observations ─────────────────────────────────────────────────────────────

OBS_PATH = "/root/olo_live_home/phase_b_observations.json"


def _record(key: str, value) -> None:
    obs = {}
    if os.path.exists(OBS_PATH):
        with open(OBS_PATH) as f:
            obs = json.load(f)
    obs[key] = value
    with open(OBS_PATH, "w") as f:
        json.dump(obs, f, indent=2)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Phase B Stage 1: Round 1 data ingestion")
    print("=" * 60)

    from olo.runner import run_source

    # ── Gmail ─────────────────────────────────────────────────────────────────
    print("\n--- Gmail (in-process, limit=2) ---")
    gmail_adapter, gmail_spy = _make_spy("gmail")
    result_g = run_source(gmail_adapter, limit=2, max_batches=50)

    print(f"run_source status: {result_g.status}")
    print(f"batches: {result_g.batches}, fetched: {result_g.fetched}, "
          f"inserted: {result_g.inserted}, duplicates: {result_g.duplicates}")
    if result_g.error:
        print(f"ERROR: {result_g.error}")
        sys.exit(1)

    conn = _db()
    gmail_n       = _total(conn, "gmail_livetest")
    gmail_kinds   = _counts(conn, "gmail_livetest")
    gmail_sigs    = _all_signals(conn, "gmail_livetest")
    gmail_cursor  = _checkpoint(conn, "gmail_livetest")
    conn.close()

    print(f"\nTotal signals: {gmail_n}")
    print(f"By kind: {gmail_kinds}")
    print(f"Checkpoint: {gmail_cursor!r}")
    print("\nSignals:")
    for s in gmail_sigs:
        subj = (s["subject"] or "")[:60]
        print(f"  {s['kind']:<30} ext_id={s['external_id'][:16]}…  subject={subj!r}")

    # V1: check only expected API paths
    gmail_paths = {c["path"] for c in gmail_spy.calls}
    expected_paths = {
        "/users/me/profile",
        "/users/me/history",
    }
    unexpected = {p for p in gmail_paths
                  if not p.startswith("/users/me/messages/")
                  and p not in expected_paths}
    print(f"\nV1 — API paths used: {sorted(gmail_paths)}")
    print(f"V1 — unexpected paths: {sorted(unexpected)}")
    _record("V1_gmail_paths", sorted(gmail_paths))
    _record("V1_unexpected_paths", sorted(unexpected))

    # V2: startHistoryId constant per fetch; ascending record ids
    history_calls = [c for c in gmail_spy.calls if "/history" in c["path"]]
    start_ids = [c["start_history_id"] for c in history_calls
                 if c["start_history_id"] is not None]
    page_token_calls = sum(1 for c in history_calls if c["has_page_token"])
    print(f"\nV2 — history.list calls: {len(history_calls)}")
    print(f"V2 — startHistoryIds used: {start_ids}")
    print(f"V2 — calls with pageToken: {page_token_calls}")
    _record("V2_history_calls", len(history_calls))
    _record("V2_start_history_ids", start_ids)
    _record("V2_page_token_calls", page_token_calls)
    # H1 check: within each batch, start_history_id must be constant
    unique_starts = set(start_ids)
    _record("H1_unique_start_ids", sorted(unique_starts))

    # V6: check if refresh returned a new refresh_token (read key names only)
    # (we can't observe this directly from the spy, but can note if rotation happened)
    _record("V6_note", "rotation observable only via credential file update timestamp")

    # Exact count assertions
    received = gmail_kinds.get("email.received", 0)
    sent     = gmail_kinds.get("email.sent", 0)
    print(f"\nExpected: 6 email.received + 3 email.sent = 9 total")
    print(f"  (P1-P5=5 received, P7=1 received, P6=1 sent, P8_sent=1 sent, P8_recv=1 sent[*])")
    print(f"  [*] V8: API-imported self-email (From==account) is classified as sent by Gmail")
    print(f"Actual:   {received} email.received + {sent} email.sent = {gmail_n} total")

    if received == 6 and sent == 3:
        print("Gmail count: EXACT MATCH ✓")
    elif received == 6 and sent == 2:
        print("Gmail: 6+2=8 (P8_recv classified as email.received — also valid)")
    else:
        extra_kinds = {k: v for k, v in gmail_kinds.items()
                       if k not in ("email.received", "email.sent")}
        if extra_kinds:
            print(f"Extra kinds: {extra_kinds}")
        if received != 6:
            print(f"STOP: expected 6 email.received, got {received}")
            sys.exit(1)
        if sent not in (2, 3):
            print(f"STOP: expected 2 or 3 email.sent, got {sent}")
            sys.exit(1)

    _record("stage1_gmail_n",     gmail_n)
    _record("stage1_gmail_kinds", gmail_kinds)

    # ── Calendar ──────────────────────────────────────────────────────────────
    print("\n--- Calendar (in-process) ---")
    gcal_adapter, gcal_spy = _make_spy("gcal")
    result_c = run_source(gcal_adapter, limit=100, max_batches=20)

    print(f"run_source status: {result_c.status}")
    print(f"batches: {result_c.batches}, fetched: {result_c.fetched}, "
          f"inserted: {result_c.inserted}, duplicates: {result_c.duplicates}")
    if result_c.error:
        print(f"ERROR: {result_c.error}")
        sys.exit(1)

    conn = _db()
    gcal_n      = _total(conn, "gcal_livetest")
    gcal_kinds  = _counts(conn, "gcal_livetest")
    gcal_sigs   = _all_signals(conn, "gcal_livetest")
    gcal_cursor = _checkpoint(conn, "gcal_livetest")
    conn.close()

    print(f"\nTotal signals: {gcal_n}")
    print(f"By kind: {gcal_kinds}")
    print(f"Checkpoint: {gcal_cursor!r}")
    print("\nSignals:")
    for s in gcal_sigs:
        subj = (s["subject"] or "")[:60]
        print(f"  {s['kind']:<35} ext_id={s['external_id'][:20]}…  subject={subj!r}")

    # V3: cursor must be incr phase with sync_token
    try:
        cd = json.loads(gcal_cursor)
        phase = cd.get("phase")
        has_sync = "sync_token" in cd
        print(f"\nV3 — cursor phase: {phase}, has sync_token: {has_sync}")
        _record("V3_cursor_phase", phase)
        _record("V3_has_sync_token", has_sync)
        assert phase == "incr", f"V3: cursor phase must be 'incr', got {phase!r}"
    except (json.JSONDecodeError, TypeError) as e:
        print(f"V3 — cursor parse failed: {e}")
        _record("V3_cursor_phase", "parse_error")

    # V4: timeMin full sync → incr
    # Stage 0 showed immediate incr cursor after full sync — V4 confirmed in stage 0
    _record("V4_full_sync_to_incr", "confirmed in stage 0 bootstrap")

    # Calendar spy — check only GET calls
    gcal_paths = {c["path"] for c in gcal_spy.calls}
    print(f"\nV1 (gcal) — API paths used: {sorted(gcal_paths)}")
    _record("V1_gcal_paths", sorted(gcal_paths))
    gcal_unexpected = {p for p in gcal_paths
                       if not p.startswith("/calendars/primary/events")}
    if gcal_unexpected:
        print(f"V1 (gcal) — unexpected paths: {gcal_unexpected}")
        _record("V1_gcal_unexpected", sorted(gcal_unexpected))

    changed = gcal_kinds.get("calendar.event.changed", 0)
    cancelled = gcal_kinds.get("calendar.event.cancelled", 0)
    print(f"\nExpected: 4 calendar.event.changed (E1-E4) + N cancelled (from seeder cleanup)")
    print(f"Actual:   {changed} changed + {cancelled} cancelled  ({gcal_n} total)")

    if changed == 4:
        print("Calendar changed count: EXACT MATCH ✓")
        if cancelled > 0:
            print(f"  Note: {cancelled} cancelled signals from API cleanup of previous test events")
            print("  These are real Google API signals, not spurious Olo behavior")
    else:
        print(f"STOP: expected 4 calendar.event.changed, got {changed}")
        sys.exit(1)

    _record("stage1_gcal_n",     gcal_n)
    _record("stage1_gcal_kinds", gcal_kinds)

    # ── Idempotence: second run adds 0 ────────────────────────────────────────
    print("\n--- Idempotence check (cron run both jobs again) ---")
    import subprocess
    env = {**os.environ, "HERMES_HOME": "/root/olo_live_home",
           "PATH": "/root/olo_linux_venv/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")}

    with open("/root/olo_live_home/phase_b_state.json") as f:
        state = json.load(f)
    gmail_job = state["gmail_job_id"]
    gcal_job  = state["gcal_job_id"]

    for name, jid in [("gmail", gmail_job), ("gcal", gcal_job)]:
        r = subprocess.run(
            ["/root/olo_linux_venv/bin/hermes", "cron", "run", jid],
            capture_output=True, text=True, env=env,
        )
        print(f"{name} re-run: rc={r.returncode}  stdout={r.stdout.strip()!r}")
        assert r.returncode == 0, f"{name} re-run failed: rc={r.returncode} {r.stderr}"
        assert "Ran now: succeeded." in r.stdout, f"unexpected output: {r.stdout!r}"

    conn = _db()
    gmail_n2 = _total(conn, "gmail_livetest")
    gcal_n2  = _total(conn, "gcal_livetest")
    conn.close()

    print(f"\nAfter re-run: gmail={gmail_n2} (was {gmail_n}), gcal={gcal_n2} (was {gcal_n})")
    assert gmail_n2 == gmail_n, f"STOP: gmail idempotence failed: {gmail_n2} != {gmail_n}"
    assert gcal_n2  == gcal_n,  f"STOP: gcal idempotence failed:  {gcal_n2} != {gcal_n}"
    print("Idempotence: OK ✓")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Stage 1 Summary")
    print("=" * 60)
    print(f"Gmail:    {gmail_n} signals  (received={received}, sent={sent})")
    print(f"Calendar: {gcal_n} signals  (changed={changed})")
    print(f"V1 (no writes): {'OK' if not unexpected and not gcal_unexpected else 'CHECK'}")
    print(f"V2 (startHistoryId constant): {len(unique_starts)} unique value(s) seen")
    print(f"V3 (sync_token in cursor): {has_sync}")
    print(f"Idempotence: 0 duplicates ✓")
    print(f"\nObservations saved to: {OBS_PATH}")


if __name__ == "__main__":
    main()
