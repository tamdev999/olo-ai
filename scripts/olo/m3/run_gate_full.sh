#!/usr/bin/env bash
# Run the FULL strict M3 gate — no deselection of test_embedding_model_comparison.
# Writes JUnit XML to /tmp/gate_full.xml and tee-logs to /tmp/gate_full.log.
set -euo pipefail

BANK_ID="${HINDSIGHT_BANK_ID:-main-v1-granite}"
PORT="${HINDSIGHT_PORT:-18800}"
echo "=== M3.0 FULL Gate — Granite + DeepSeek ==="
echo "  Bank:  ${BANK_ID}"
echo "  Port:  ${PORT}"
echo "  Date:  $(date -u)"
echo ""

# Load API key
API_KEY_FILE="/root/olo_m3_secrets/deepseek.env"
if [ -f "${API_KEY_FILE}" ]; then
    . "${API_KEY_FILE}" 2>/dev/null || true
fi
if [ -z "${HINDSIGHT_API_LLM_API_KEY:-}" ]; then
    echo "ERROR: HINDSIGHT_API_LLM_API_KEY not set" >&2
    exit 1
fi

source /root/olo_linux_venv/bin/activate
export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT="${PORT}"
export HINDSIGHT_BANK_ID="${BANK_ID}"
export HINDSIGHT_BIN=/root/olo_m3_hindsight_venv/bin/hindsight-api
export OLO_DB_PATH=/root/gate_olo.db
export OLO_GATE_STRICT=1
export HINDSIGHT_API_LLM_API_KEY

cd /mnt/c/oloai

# Full gate — includes test_embedding_model_comparison (V10)
python3 -m pytest tests/olo/m3/test_m3_gate.py \
    --override-ini='addopts=' \
    -v --tb=short \
    --junit-xml=/tmp/gate_full.xml \
    2>&1 | tee /tmp/gate_full.log

echo ""
echo "=== Gate complete: $(date -u) ==="
echo "JUnit XML: /tmp/gate_full.xml"
echo "Full log:  /tmp/gate_full.log"
