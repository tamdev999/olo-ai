#!/usr/bin/env bash
# Rebuild /root/gate_olo.db after CM-01 corpus change
set -euo pipefail
source /root/olo_m3_secrets/deepseek.env
export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
cd /mnt/c/oloai
source /root/olo_linux_venv/bin/activate
export OLO_DB_PATH=/root/gate_olo.db
python3 -c "
from pathlib import Path
from olo.m3.synthetic.generator import generate_timeline
from olo.m3.feeder import populate_olo_db
import os

db = Path(os.environ['OLO_DB_PATH'])
print(f'Rebuilding {db}...')
if db.exists():
    db.unlink()
    print('  Removed old DB')
tl = generate_timeline()
populate_olo_db(tl, db)
print(f'  Built {db} ({db.stat().st_size:,} bytes, {len(tl)} signals)')
"
echo "DB rebuild complete: $(date -u)"
