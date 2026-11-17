import os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
conn.execute("UPDATE sync_checkpoints SET cursor='1' WHERE source='gmail_livetest'")
conn.commit(); conn.close()
print("Injected bad cursor '1' for gmail_livetest")
