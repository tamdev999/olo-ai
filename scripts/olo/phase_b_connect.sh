#!/usr/bin/env bash
# Phase B: connect the livetest account using --manual mode.
# Usage: bash scripts/olo/phase_b_connect.sh
# Then: paste the redirect URL when prompted.
set -euo pipefail

REPO="/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
VENV="/root/olo_venv"
LIVE_HOME="/root/olo_live_home"
SECRET="/root/olo_client_secret.json"

export HERMES_HOME="$LIVE_HOME"
export PATH="$VENV/bin:$PATH"

echo "=== Phase B: Connect Google account 'livetest' ==="
echo "HERMES_HOME: $LIVE_HOME"
echo ""
echo "This will print a URL. BBC must:"
echo "  1. Open that URL in a browser"
echo "  2. Sign in as the TEST account (not personal/business)"
echo "  3. Accept the unverified-app warning"
echo "  4. Tick BOTH scope checkboxes"
echo "  5. After redirect fails (can't reach WSL2), copy the full URL from browser"
echo "  6. Paste it here and press Enter"
echo ""

hermes olo google connect \
    --account livetest \
    --client-secret-file "$SECRET" \
    --manual

echo ""
echo "=== Verifying connection ==="
hermes olo google status --check --account livetest

echo ""
echo "=== Checking credential file permissions ==="
CRED_DIR="$LIVE_HOME/credentials/google"
if [ -f "$CRED_DIR/livetest.json" ]; then
    MODE=$(stat -c '%a' "$CRED_DIR/livetest.json")
    DIR_MODE=$(stat -c '%a' "$CRED_DIR")
    echo "Credential file: mode $MODE (expected 600)"
    echo "Credential dir: mode $DIR_MODE (expected 700)"
    [ "$MODE" = "600" ] && echo "File permissions OK" || echo "WARNING: file mode $MODE != 600"
    [ "$DIR_MODE" = "700" ] && echo "Dir permissions OK" || echo "WARNING: dir mode $DIR_MODE != 700"
else
    echo "ERROR: credential file not found at $CRED_DIR/livetest.json"
    exit 1
fi

echo ""
echo "=== Checking granted scopes ==="
python3 - <<'EOF'
import json, os, hashlib
home = os.environ['HERMES_HOME']
path = f"{home}/credentials/google/livetest.json"
with open(path) as f:
    d = json.load(f)
scopes = d.get('granted_scopes', [])
email = d.get('email_address', '')
email_hash = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
print(f"granted_scopes: {sorted(scopes)}")
print(f"email_hash: {email_hash}")
expected = {
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/calendar.events.owned.readonly",
}
if set(scopes) == expected:
    print("Scopes: EXACT MATCH - OK")
else:
    extra = set(scopes) - expected
    missing = expected - set(scopes)
    if extra:
        print(f"ERROR: extra scopes: {extra}")
    if missing:
        print(f"ERROR: missing scopes: {missing}")
EOF
