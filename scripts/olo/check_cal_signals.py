import os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai")
os.environ["HERMES_HOME"] = "/root/olo_live_home"
from olo.store import connect, migrate
from olo.paths import db_path
conn = connect(db_path()); migrate(conn)
rows = conn.execute(
    "SELECT kind, COUNT(*) n FROM signals WHERE source='gcal_livetest' GROUP BY kind"
).fetchall()
print("By kind:")
for r in rows:
    print(f"  {r['kind']}: {r['n']}")
sigs = conn.execute(
    "SELECT kind, subject FROM signals WHERE source='gcal_livetest' ORDER BY kind, occurred_at"
).fetchall()
print("\nAll signals:")
for s in sigs:
    print(f"  {s['kind']}  {(s['subject'] or '')[:60]!r}")
conn.close()
