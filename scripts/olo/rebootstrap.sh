#!/usr/bin/env bash
set -euo pipefail
export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export HERMES_HOME=/root/olo_live_home
PYTHON=/root/olo_linux_venv/bin/python3
STATE_FILE=/root/olo_live_home/phase_b_state.json

GMAIL_JOB=$($PYTHON -c "import json; d=json.load(open('$STATE_FILE')); print(d['gmail_job_id'])")
GCAL_JOB=$($PYTHON -c "import json; d=json.load(open('$STATE_FILE')); print(d['gcal_job_id'])")

echo "Gmail job: $GMAIL_JOB"
echo "Gcal job: $GCAL_JOB"

echo ""
echo "--- Re-bootstrapping Gmail ---"
hermes cron run "$GMAIL_JOB"
echo "Gmail exit: $?"

echo ""
echo "--- Re-bootstrapping Calendar ---"
hermes cron run "$GCAL_JOB"
echo "Gcal exit: $?"

echo ""
echo "--- DB state after re-bootstrap ---"
$PYTHON - <<'PYEOF'
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, get_checkpoint, count_signals, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
for src in ("gmail_livetest", "gcal_livetest"):
    chk = get_checkpoint(src, conn)
    n   = count_signals(src, conn)
    print(f"{src}: signals={n} checkpoint={chk!r}")
conn.close()
PYEOF
