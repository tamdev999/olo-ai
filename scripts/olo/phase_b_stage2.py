"""Phase B Stage 2: verify Round 2 mutation signals."""
from __future__ import annotations

import json
import os
import subprocess
import sys

REPO = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
sys.path.insert(0, REPO)
os.environ["HERMES_HOME"] = "/root/olo_live_home"

STATE_FILE  = "/root/olo_seeder_state.json"
PHASE_STATE = "/root/olo_live_home/phase_b_state.json"
OBS_PATH    = "/root/olo_live_home/phase_b_observations.json"
HERMES_EXE  = "/root/olo_linux_venv/bin/hermes"
PYTHON      = "/root/olo_linux_venv/bin/python3"

from olo.runner import run_source
from olo.store import connect, count_signals, get_checkpoint, migrate
from olo.paths import db_path
from olo.google.credentials import load_credentials
from olo.google.http import GoogleClient
from olo.adapters.gmail import GmailAdapter
from olo.adapters.gcal import GcalAdapter


def _db():
    conn = connect(db_path())
    migrate(conn)
    return conn


def _counts(conn, source: str) -> dict:
    rows = conn.execute(
        "SELECT kind, COUNT(*) n FROM signals WHERE source=? GROUP BY kind", (source,)
    ).fetchall()
    return {r["kind"]: r["n"] for r in rows}


def _obs(key, value):
    obs = {}
    if os.path.exists(OBS_PATH):
        with open(OBS_PATH) as f:
            obs = json.load(f)
    obs[key] = value
    with open(OBS_PATH, "w") as f:
        json.dump(obs, f, indent=2)


with open(STATE_FILE) as f:
    state = json.load(f)
with open(PHASE_STATE) as f:
    phase = json.load(f)

print("=" * 60)
print("Phase B Stage 2: Round 2 mutations verification")
print("=" * 60)

stage1_gmail_n = 8
stage1_gcal_n  = 20  # 4 changed + 16 cancelled from bootstrap

# ── Gmail ─────────────────────────────────────────────────────────────────────
print("\n--- Gmail ---")
creds = load_credentials("livetest")
client = GoogleClient(creds)
gmail_adapter = GmailAdapter({"account": "livetest"}, client=client)
result = run_source(gmail_adapter, limit=100, max_batches=50)
print(f"run_source: {result.status}  inserted={result.inserted}")
assert result.status in ("succeeded", "partial"), f"STOP: {result.status} {result.error}"

conn = _db()
gmail_n2    = count_signals("gmail_livetest", conn)
gmail_kinds = _counts(conn, "gmail_livetest")
gmail_new   = gmail_n2 - stage1_gmail_n
conn.close()

print(f"Total signals: {gmail_n2} (was {stage1_gmail_n}, added {gmail_new})")
print(f"By kind: {gmail_kinds}")

# Expected new signals:
# - P1: email.labels_removed INBOX → 1
# - P2: email.labels_removed UNREAD → 1
# - P3: email.labels_added STARRED → 1
# - P4: email.labels_added TRASH → 1 (from trash operation)
# Total new: ≥4 (some IMPORTANT/UNREAD toggles may also appear)
removed_total = gmail_kinds.get("email.labels_removed", 0)
added_total   = gmail_kinds.get("email.labels_added", 0)
print(f"\nLabel signals: {removed_total} removed + {added_total} added")

# Check exact expected signals
conn2 = _db()
new_signals = conn2.execute(
    "SELECT external_id, kind, payload, occurred_at FROM signals "
    "WHERE source='gmail_livetest' AND kind LIKE 'email.labels%' "
    "ORDER BY occurred_at"
).fetchall()
conn2.close()

print(f"\nLabel change signals ({len(new_signals)} total):")
for s in new_signals:
    p = json.loads(s["payload"])
    labels = p.get("labels", [])
    print(f"  {s['kind']:<30} labels={labels}  ext={s['external_id'][:20]}")

# Assert minimum: INBOX removed, UNREAD removed, STARRED added, TRASH added
label_removed_labels = set()
label_added_labels   = set()
for s in new_signals:
    p = json.loads(s["payload"])
    if s["kind"] == "email.labels_removed":
        label_removed_labels.update(p.get("labels", []))
    elif s["kind"] == "email.labels_added":
        label_added_labels.update(p.get("labels", []))

print(f"\nLabels removed: {label_removed_labels}")
print(f"Labels added:   {label_added_labels}")

assert "INBOX" in label_removed_labels, "STOP: P1 archive (INBOX removed) not seen"
assert "UNREAD" in label_removed_labels, "STOP: P2 mark-read (UNREAD removed) not seen"
assert "STARRED" in label_added_labels, "STOP: P3 star (STARRED added) not seen"
assert "TRASH" in label_added_labels, "STOP: P4 trash (TRASH added) not seen"
print("Gmail Round 2 signals: OK ✓")

# Check V8: P4 trash → email.labels_added TRASH
# (also check for potential email.deleted if P4 was permanently deleted)
p4_id = state["p4_id"]
p4_sigs = [s for s in new_signals if p4_id in s["external_id"]]
print(f"\nP4 signals: {len(p4_sigs)}")
for s in p4_sigs:
    p = json.loads(s["payload"])
    print(f"  {s['kind']}  labels={p.get('labels')}")
_obs("V8_p4_round2_signals", [
    {"kind": s["kind"], "labels": json.loads(s["payload"]).get("labels")}
    for s in p4_sigs
])

_obs("stage2_gmail_n", gmail_n2)
_obs("stage2_gmail_kinds", gmail_kinds)

# ── Calendar ──────────────────────────────────────────────────────────────────
print("\n--- Calendar ---")
gcal_adapter = GcalAdapter({"account": "livetest", "lookback_days": "0"}, client=GoogleClient(creds))
result_c = run_source(gcal_adapter, limit=100, max_batches=20)
print(f"run_source: {result_c.status}  inserted={result_c.inserted}")

conn = _db()
gcal_n2    = count_signals("gcal_livetest", conn)
gcal_kinds = _counts(conn, "gcal_livetest")
gcal_new   = gcal_n2 - stage1_gcal_n
conn.close()

print(f"Total signals: {gcal_n2} (was {stage1_gcal_n}, added {gcal_new})")
print(f"By kind: {gcal_kinds}")

# Expected: E1 rename → 1 changed; E3 cancel → 1 cancelled
new_changed   = gcal_kinds.get("calendar.event.changed", 0) - 4  # 4 from Round 1
new_cancelled = gcal_kinds.get("calendar.event.cancelled", 0) - 16  # 16 from cleanup

print(f"\nNew since Round 1: changed={new_changed}  cancelled={new_cancelled}")

# Check E1 rename produced a NEW changed signal
conn3 = _db()
e1_sigs = conn3.execute(
    "SELECT external_id, kind, subject FROM signals "
    "WHERE source='gcal_livetest' AND kind='calendar.event.changed' "
    "AND subject LIKE '%RENAMED%'"
).fetchall()
e3_sigs = conn3.execute(
    "SELECT external_id, kind, subject FROM signals "
    "WHERE source='gcal_livetest' AND kind='calendar.event.cancelled' "
    "AND subject LIKE '%OLO-E3%'"
).fetchall()
conn3.close()

print(f"E1 renamed signals: {len(e1_sigs)}")
for s in e1_sigs:
    print(f"  {s['kind']}  {s['subject']!r}  ext={s['external_id'][:20]}")
print(f"E3 cancelled signals: {len(e3_sigs)}")
for s in e3_sigs:
    print(f"  {s['kind']}  {s['subject']!r}  ext={s['external_id'][:20]}")

assert len(e1_sigs) >= 1, "STOP: E1 rename did not produce a new calendar.event.changed"
assert len(e3_sigs) >= 1, "STOP: E3 cancel did not produce a calendar.event.cancelled"
print("Calendar Round 2 signals: OK ✓")

# V3: E1 rename produces new external_id (different updated timestamp)
if e1_sigs:
    _obs("V3_e1_renamed_ext_id", e1_sigs[0]["external_id"])

_obs("stage2_gcal_n", gcal_n2)
_obs("stage2_gcal_kinds", gcal_kinds)

# ── Idempotence ───────────────────────────────────────────────────────────────
print("\n--- Idempotence ---")
env = {**os.environ, "HERMES_HOME": "/root/olo_live_home",
       "PATH": "/root/olo_linux_venv/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")}
for name, jid in [("gmail", phase["gmail_job_id"]), ("gcal", phase["gcal_job_id"])]:
    r = subprocess.run([HERMES_EXE, "cron", "run", jid],
                       capture_output=True, text=True, env=env)
    print(f"  {name}: rc={r.returncode}  {r.stdout.strip()!r}")
    assert r.returncode == 0
    assert "Ran now: succeeded." in r.stdout

conn = _db()
gn3 = count_signals("gmail_livetest", conn)
cn3 = count_signals("gcal_livetest", conn)
conn.close()
assert gn3 == gmail_n2, f"gmail idempotence: {gn3} != {gmail_n2}"
assert cn3 == gcal_n2,  f"gcal idempotence:  {cn3} != {gcal_n2}"
print("Idempotence: 0 duplicates ✓")

print("\n" + "=" * 60)
print("Stage 2 Summary")
print("=" * 60)
print(f"Gmail new signals: {gmail_new} label-change signals")
print(f"  INBOX removed (P1 archive): ✓")
print(f"  UNREAD removed (P2 read): ✓")
print(f"  STARRED added (P3 star): ✓")
print(f"  TRASH added (P4 trash): ✓")
print(f"Calendar new signals: {gcal_new}")
print(f"  E1 renamed (new changed): ✓")
print(f"  E3 cancelled: ✓")
print(f"Observations: {OBS_PATH}")
