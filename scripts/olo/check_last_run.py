"""Check the last ingest run status and checkpoint."""
import os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"

from olo.store import connect, get_checkpoint, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)

# Last 3 ingest runs
rows = conn.execute(
    "SELECT id, source, status, started_at, finished_at, error "
    "FROM ingest_runs ORDER BY started_at DESC LIMIT 3"
).fetchall()
for r in rows:
    print(f"{r['source']:20}  status={r['status']:10}  error={r['error']!r}")
print()
# Checkpoint
for src in ("gmail_livetest", "gcal_livetest"):
    chk = get_checkpoint(src, conn)
    print(f"{src}: cursor={chk!r}")
conn.close()
