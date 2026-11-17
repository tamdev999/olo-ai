#!/usr/bin/env bash
set -euo pipefail
export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export HERMES_HOME=/root/olo_live_home
PYTHON=/root/olo_linux_venv/bin/python3

echo "=== Failure/recovery: bad Gmail cursor ==="
$PYTHON '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/inject_bad_cursor.py'

echo "--- Running cron job with bad cursor ---"
hermes cron run f4b3575429c4 || true
echo "cron run exit: $?"

echo ""
echo "--- DB state after bad cursor run ---"
$PYTHON '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/check_last_run.py'

echo ""
echo "--- Reset and re-bootstrap ---"
hermes olo checkpoint reset --source gmail_livetest --confirm
hermes cron run f4b3575429c4
echo ""
echo "--- DB state after re-bootstrap ---"
$PYTHON '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/check_last_run.py'

echo ""
echo "=== Failure/recovery: bad Calendar sync token ==="
$PYTHON - <<'PYEOF'
import os, sys, json
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
bad = json.dumps({"phase":"incr","sync_token":"INVALID_TOKEN_TEST","page_token":None})
conn.execute("UPDATE sync_checkpoints SET cursor=? WHERE source='gcal_livetest'", (bad,))
conn.commit(); conn.close()
print(f"Injected bad Calendar sync token")
PYEOF

echo "--- Running gcal cron job with bad token ---"
hermes cron run 0a82cc7553ee || true
echo "cron run exit: $?"

echo ""
$PYTHON '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/check_last_run.py'

echo ""
echo "--- Gcal reset and re-bootstrap ---"
hermes olo checkpoint reset --source gcal_livetest --confirm
hermes cron run 0a82cc7553ee
$PYTHON '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/check_last_run.py'

echo ""
echo "=== Failure/recovery tests complete ==="
