import sqlite3
import os

for db_path in ['/root/gate_olo.db', '/root/olo.db', '/home/olo_hs/hs_gate_data/olo.db']:
    if os.path.exists(db_path):
        try:
            c = sqlite3.connect(db_path)
            sig_count = c.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
            print(f"{db_path}: {sig_count} signals")
            c.close()
        except Exception as e:
            print(f"{db_path}: error - {e}")
    else:
        print(f"{db_path}: NOT FOUND")
