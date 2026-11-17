#!/usr/bin/env bash
# Correct Phase B sequence:
# 1. Bootstrap both adapters (establish cursors at current historyId)
# 2. Seed all test data (messages/events arrive AFTER cursor)
# 3. Run Stage 1 (incremental from cursor picks up all seeded data)
set -euo pipefail

export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export HERMES_HOME=/root/olo_live_home
PYTHON=/root/olo_linux_venv/bin/python3
STATE_FILE=/root/olo_live_home/phase_b_state.json
SEEDER_DIR="/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder"

GMAIL_JOB=$($PYTHON -c "import json; print(json.load(open('$STATE_FILE'))['gmail_job_id'])")
GCAL_JOB=$($PYTHON  -c "import json; print(json.load(open('$STATE_FILE'))['gcal_job_id'])")
echo "Gmail job: $GMAIL_JOB"
echo "Gcal  job: $GCAL_JOB"

# ── Step 1: Bootstrap ─────────────────────────────────────────────────────────
echo ""
echo "=== Step 1: Bootstrap (cursor = current historyId) ==="
hermes cron run "$GMAIL_JOB"
hermes cron run "$GCAL_JOB"

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
    print(f"  {src}: signals={n} checkpoint={chk!r}")
conn.close()
PYEOF

# Record bootstrap cursors
GMAIL_CURSOR=$($PYTHON -c "
import json, os, sys
sys.path.insert(0, '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai')
os.environ['HERMES_HOME'] = '/root/olo_live_home'
from olo.store import connect, get_checkpoint, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
print(get_checkpoint('gmail_livetest', conn))
conn.close()
")
echo "Gmail bootstrap cursor: $GMAIL_CURSOR"

# ── Step 2: Seed test data ────────────────────────────────────────────────────
echo ""
echo "=== Step 2: Seed test data (after cursor $GMAIL_CURSOR) ==="
cd "$SEEDER_DIR"
$PYTHON run_seeder.py

# Verify historyId advanced past bootstrap cursor
GMAIL_CURSOR_INT=$(echo $GMAIL_CURSOR | tr -d "'" | tr -d '"')
$PYTHON - <<PYEOF2
import json, os, sys
sys.path.insert(0, "$SEEDER_DIR")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from auth import get_credentials
from googleapiclient.discovery import build
creds = get_credentials()
gmail = build("gmail", "v1", credentials=creds)
profile = gmail.users().getProfile(userId="me").execute()
hid = profile["historyId"]
cursor = "$GMAIL_CURSOR_INT"
print(f"Current historyId: {hid}  Bootstrap cursor: {cursor}")
if int(hid) > int(cursor):
    print("historyId > cursor: seeded messages are in history window ✓")
else:
    print("STOP: historyId did not advance past cursor")
    sys.exit(1)
PYEOF2

echo ""
echo "=== Step 2 complete: data seeded after cursor ==="
