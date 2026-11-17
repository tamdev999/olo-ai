#!/usr/bin/env bash
# Phase B Stage 0: setup monitoring jobs and bootstrap both adapters.
set -euo pipefail

export HERMES_HOME=/root/olo_live_home
export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export PYTHONPATH=/mnt/c/Users/twMatt/Music/OLO\ MVP/olo-ai

REPO="/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
cd "$REPO"

echo "=== Stage 0: Setup ==="
echo "HERMES_HOME: $HERMES_HOME"
echo ""

# ── Setup sources ─────────────────────────────────────────────────────────────
echo "--- olo setup gmail ---"
hermes olo setup --source gmail --option account=livetest
echo ""

echo "--- olo setup gcal ---"
hermes olo setup --source gcal --option account=livetest --option lookback_days=7
echo ""

# ── Create cron jobs (idempotent via state file) ───────────────────────────────
STATE_FILE="$HERMES_HOME/phase_b_state.json"
python3 - <<'PYEOF'
import json, os, subprocess, sys
state_file = os.environ["HERMES_HOME"] + "/phase_b_state.json"
state = {}
if os.path.exists(state_file):
    with open(state_file) as f:
        state = json.load(f)

hermes_exe = "/root/olo_linux_venv/bin/hermes"
env = {**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"]}

def run(*args):
    r = subprocess.run([hermes_exe, *args], capture_output=True, text=True, env=env, check=False)
    print(f"$ hermes {' '.join(args)}")
    if r.stdout: print(r.stdout.rstrip())
    if r.stderr: print(r.stderr.rstrip(), file=sys.stderr)
    return r

def parse_job_id(output):
    for line in output.splitlines():
        if "Created job:" in line:
            return line.split(":", 1)[1].strip()
    return None

# Gmail job
if not state.get("gmail_job_id"):
    r = run("cron", "create", "every 5m",
            "--name", "olo-gmail-livetest",
            "--script", "olo_monitor_gmail_livetest.py",
            "--no-agent", "--deliver", "local")
    assert r.returncode == 0, f"gmail cron create failed: {r.stderr}"
    jid = parse_job_id(r.stdout)
    assert jid, f"could not parse gmail job id from: {r.stdout}"
    state["gmail_job_id"] = jid
    print(f"  → gmail_job_id = {jid}")
else:
    print(f"gmail job already created: {state['gmail_job_id']}")

# Gcal job
if not state.get("gcal_job_id"):
    r = run("cron", "create", "every 15m",
            "--name", "olo-gcal-livetest",
            "--script", "olo_monitor_gcal_livetest.py",
            "--no-agent", "--deliver", "local")
    assert r.returncode == 0, f"gcal cron create failed: {r.stderr}"
    jid = parse_job_id(r.stdout)
    assert jid, f"could not parse gcal job id from: {r.stdout}"
    state["gcal_job_id"] = jid
    print(f"  → gcal_job_id = {jid}")
else:
    print(f"gcal job already created: {state['gcal_job_id']}")

with open(state_file, "w") as f:
    json.dump(state, f, indent=2)
print(f"\nState saved: {state_file}")
PYEOF

echo ""
echo "=== Running Stage 0 bootstrap ==="

# ── Run both jobs ─────────────────────────────────────────────────────────────
STATE_FILE="$HERMES_HOME/phase_b_state.json"
GMAIL_JOB=$(python3 -c "import json; d=json.load(open('$STATE_FILE')); print(d['gmail_job_id'])")
GCAL_JOB=$(python3  -c "import json; d=json.load(open('$STATE_FILE')); print(d['gcal_job_id'])")
echo "Gmail job: $GMAIL_JOB"
echo "Gcal job:  $GCAL_JOB"
echo ""

echo "--- cron run gmail (bootstrap) ---"
hermes cron run "$GMAIL_JOB"
echo "Gmail exit: $?"
echo ""

echo "--- cron run gcal (bootstrap) ---"
hermes cron run "$GCAL_JOB"
echo "Gcal exit: $?"
echo ""

# ── Check results ─────────────────────────────────────────────────────────────
echo "=== Checking DB results ==="
python3 - <<'PYEOF'
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = os.environ.get("HERMES_HOME", "/root/olo_live_home")

from olo.store import connect, count_signals, get_checkpoint, migrate
from olo.paths import db_path

db = db_path()
print(f"DB path: {db}")
conn = connect(db)
migrate(conn)

for source in ("gmail_livetest", "gcal_livetest"):
    n = count_signals(source, conn)
    chk = get_checkpoint(source, conn)
    print(f"\n{source}:")
    print(f"  signals: {n}")
    print(f"  checkpoint: {chk!r}")
    if source == "gmail_livetest":
        assert chk is not None, f"STOP: Gmail bootstrap must set a checkpoint, got None"
        assert n == 0,          f"STOP: Gmail bootstrap must produce 0 signals, got {n}"
        print("  Gmail bootstrap: OK")
    else:
        assert chk is not None, f"STOP: Calendar bootstrap must set a checkpoint, got None"
        # Parse cursor phase
        try:
            cd = json.loads(chk)
            phase = cd.get("phase")
            print(f"  cursor phase: {phase}")
        except Exception:
            print(f"  cursor (raw): {chk!r}")
        print(f"  Calendar bootstrap: {n} event(s) ingested (may be non-zero for existing account)")

conn.close()
print("\nStage 0 complete.")
PYEOF
