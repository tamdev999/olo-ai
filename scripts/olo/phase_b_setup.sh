#!/usr/bin/env bash
# Phase B setup script — runs in WSL2
# Usage: bash /mnt/c/Users/twMatt/Music/OLO\ MVP/olo-ai/scripts/olo/phase_b_setup.sh
set -euo pipefail

REPO="/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
VENV="/root/olo_venv"
LIVE_HOME="/root/olo_live_home"
PYTHON="python3.12"

echo "=== Phase B Setup ==="
echo "Repo: $REPO"
echo "Venv: $VENV"
echo "Live home: $LIVE_HOME"
echo "Python: $($PYTHON --version)"

# 1. Check git
cd "$REPO"
git log --oneline -2
echo "Branch: $(git branch --show-current)"

# 2. Build venv if needed
if [ ! -f "$VENV/bin/python" ]; then
    echo "=== Building venv ==="
    cd "$REPO"
    $PYTHON -m pm.build_env --source . --out "$VENV" --group dev --group test
else
    echo "Venv exists: $VENV"
fi
"$VENV/bin/python" --version

# 3. Create isolated HERMES_HOME
mkdir -p "$LIVE_HOME"
chmod 700 "$LIVE_HOME"
echo "Live home created: $LIVE_HOME"

# 4. Copy client secret to mode-600 location in WSL2
SECRET_SRC="/mnt/c/Users/twMatt/Music/OLO MVP/client_secret_796790314811-h71j05cmkoeo0qn561hk1m4v6mrqhpn8.apps.googleusercontent.com.json"
SECRET_DST="/root/olo_client_secret.json"
cp "$SECRET_SRC" "$SECRET_DST"
chmod 600 "$SECRET_DST"
echo "Client secret copied to $SECRET_DST (mode 600)"
# Verify type without printing contents
$PYTHON -c "
import json, sys
with open('$SECRET_DST') as f:
    d = json.load(f)
keys = list(d.keys())
print('Client JSON top-level keys:', keys)
if 'installed' in keys:
    print('Type: installed (Desktop app) OK')
else:
    print('ERROR: not a Desktop app client')
    sys.exit(1)
"

echo "=== Setup complete ==="
echo "Run: HERMES_HOME=$LIVE_HOME HERMES_PYTHON=$VENV/bin/python"
