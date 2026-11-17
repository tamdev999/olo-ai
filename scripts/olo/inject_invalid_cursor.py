"""Inject a non-numeric cursor to trigger Gmail 400."""
import os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
conn.execute("UPDATE sync_checkpoints SET cursor='NOTANUMBER' WHERE source='gmail_livetest'")
conn.commit(); conn.close()
print("Injected invalid (non-numeric) cursor for gmail_livetest")
