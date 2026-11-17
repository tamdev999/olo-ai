#!/usr/bin/env bash
# Recreate the gcal cron job with lookback_days=0 to avoid picking up
# stale cancelled events from previous seeder rounds.
set -euo pipefail
export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export HERMES_HOME=/root/olo_live_home
PYTHON=/root/olo_linux_venv/bin/python3
STATE_FILE=/root/olo_live_home/phase_b_state.json

echo "=== Reconfigure Calendar job with lookback_days=0 ==="

# Update setup (lookback_days=0)
hermes olo setup --source gcal --option account=livetest --option lookback_days=0 --force

# Delete existing gcal job and create new one
GCAL_JOB=$($PYTHON -c "import json; print(json.load(open('$STATE_FILE'))['gcal_job_id'])")
echo "Deleting old gcal job: $GCAL_JOB"
hermes cron delete "$GCAL_JOB" 2>&1 || echo "delete failed (ok if already gone)"

# Create new job
NEW_OUTPUT=$(hermes cron create "every 15m" \
    --name "olo-gcal-livetest" \
    --script "olo_monitor_gcal_livetest.py" \
    --no-agent --deliver local 2>&1)
echo "$NEW_OUTPUT"

# Parse new job ID
NEW_GCAL_JOB=$(echo "$NEW_OUTPUT" | grep "Created job:" | awk '{print $NF}')
echo "New gcal job: $NEW_GCAL_JOB"

# Update state file
$PYTHON - <<PYEOF
import json
f = "$STATE_FILE"
with open(f) as fh:
    s = json.load(fh)
s["gcal_job_id"] = "$NEW_GCAL_JOB"
with open(f, "w") as fh:
    json.dump(s, fh, indent=2)
print(f"State updated: gcal_job_id = $NEW_GCAL_JOB")
PYEOF

# Reset gcal checkpoint + signals
$PYTHON - <<'PYEOF'
import os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
n = conn.execute("SELECT COUNT(*) n FROM signals WHERE source='gcal_livetest'").fetchone()["n"]
conn.execute("DELETE FROM signals WHERE source='gcal_livetest'")
conn.execute("DELETE FROM sync_checkpoints WHERE source='gcal_livetest'")
conn.commit(); conn.close()
print(f"Reset: {n} signals + checkpoint cleared")
PYEOF

echo ""
echo "Re-bootstrapping with lookback_days=0..."
NEW_GCAL_JOB=$($PYTHON -c "import json; print(json.load(open('$STATE_FILE'))['gcal_job_id'])")
hermes cron run "$NEW_GCAL_JOB"

$PYTHON - <<'PYEOF'
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, get_checkpoint, count_signals, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
n = count_signals("gcal_livetest", conn)
chk = get_checkpoint("gcal_livetest", conn)
conn.close()
print(f"gcal_livetest: signals={n}  checkpoint={chk!r}")
if n == 0:
    print("Calendar bootstrap (lookback_days=0): 0 signals ✓")
elif n <= 4:
    print(f"Calendar bootstrap: {n} signals (new events in lookback window)")
else:
    print(f"WARNING: {n} signals — stale events still in window")
PYEOF
