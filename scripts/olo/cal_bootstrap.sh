#!/usr/bin/env bash
set -euo pipefail
export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export HERMES_HOME=/root/olo_live_home
PYTHON=/root/olo_linux_venv/bin/python3
STATE_FILE=/root/olo_live_home/phase_b_state.json

GCAL_JOB=$($PYTHON -c "import json; print(json.load(open('$STATE_FILE'))['gcal_job_id'])")
echo "Calendar job: $GCAL_JOB"
hermes cron run "$GCAL_JOB"

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
if n == 4:
    print("Calendar bootstrap: 4 signals - OK")
elif n > 4:
    print(f"Calendar: {n} signals (more than 4 - stale events still present)")
else:
    print(f"Calendar: {n} signals (expected 4)")
PYEOF
