#!/usr/bin/env bash
# Run the M3.0 integration gate against the Granite + DeepSeek bank.
# V10 benchmark (test_embedding_model_comparison) is excluded here because
# it creates three isolated Hindsight instances and takes 20–25 minutes;
# running it in the same session would time out unrelated tests.
# Run it separately with __run_gate_v10.sh.
#
# Usage:
#   HINDSIGHT_BANK_ID=main-v1-granite bash __run_gate_granite.sh
#
set -euo pipefail

BANK_ID="${HINDSIGHT_BANK_ID:-main-v1-granite}"
PORT="${HINDSIGHT_PORT:-18800}"

echo "=== M3.0 Gate — Granite + DeepSeek ==="
echo "  Bank:  ${BANK_ID}"
echo "  Port:  ${PORT}"
echo "  Date:  $(date -u)"
echo ""

# Load API key
API_KEY_FILE="/root/olo_m3_secrets/deepseek.env"
if [ -f "${API_KEY_FILE}" ]; then
    # shellcheck disable=SC1090
    . "${API_KEY_FILE}" 2>/dev/null || true
fi
if [ -z "${HINDSIGHT_API_LLM_API_KEY:-}" ]; then
    echo "ERROR: HINDSIGHT_API_LLM_API_KEY not set" >&2
    exit 1
fi

source /root/olo_linux_venv/bin/activate

export PYTHONPATH=/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT="${PORT}"
export HINDSIGHT_BANK_ID="${BANK_ID}"
export HINDSIGHT_BIN=/root/olo_m3_hindsight_venv/bin/hindsight-api
export OLO_DB_PATH=/root/gate_olo.db
export OLO_GATE_STRICT=1
export HINDSIGHT_API_LLM_API_KEY

cd /mnt/c/oloai

# Exclude the slow V10 benchmark comparison to prevent gate timeout.
# V10 is run separately by __run_gate_v10.sh.
python3 -m pytest tests/olo/m3/test_m3_gate.py \
    -m integration \
    -k "not test_embedding_model_comparison" \
    -v --tb=short \
    --junit-xml=/tmp/gate_granite.xml \
    2>&1
