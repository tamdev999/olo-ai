"""Wipe signals for livetest sources so Stage 1 counts start from zero."""
import os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"

from olo.store import connect, migrate
from olo.paths import db_path

conn = connect(db_path())
migrate(conn)

for source in ("gmail_livetest", "gcal_livetest"):
    n = conn.execute("SELECT COUNT(*) n FROM signals WHERE source=?", (source,)).fetchone()["n"]
    conn.execute("DELETE FROM signals WHERE source=?", (source,))
    print(f"Deleted {n} signals for {source}")

conn.commit()
conn.close()
print("DB reset complete.")
