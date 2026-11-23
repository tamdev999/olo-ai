#!/usr/bin/env bash
# Run the M3.0 integration gate against the Granite + DeepSeek bank.
# V10 benchmark (test_embedding_model_comparison) is excluded here because
# it creates three isolated Hindsight instances and takes 20–25 minutes;
# running it in the same session would time out unrelated tests.
# Run it separately with __run_gate_v10.sh.
#
# Usage: bash scripts/olo/m3/run_gate.sh
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
# Include Hindsight venv site-packages so hindsight_api is importable in the
# test interpreter (olo_linux_venv) without installing the full package there.
# Required by test_none_llm_raises_on_call and test_none_llm_verify_connection_is_noop.
export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT="${PORT}"
export HINDSIGHT_BANK_ID="${BANK_ID}"
export HINDSIGHT_BIN=/root/olo_m3_hindsight_venv/bin/hindsight-api
export OLO_DB_PATH=/root/gate_olo.db
export OLO_GATE_STRICT=1
export HINDSIGHT_API_LLM_API_KEY

cd /mnt/c/oloai

# Run ALL gate tests (integration + structural).  -m integration would exclude
# the non-integration tests (synthetic-data correctness, benchmark freeze, V9
# NoneLLM, V8 provenance structural) that are still required for a complete gate.
# V10 benchmark comparison is excluded separately — it starts three isolated
# Hindsight instances and belongs in its own gate run (__run_gate_v10.sh).
python3 -m pytest tests/olo/m3/test_m3_gate.py \
    -k "not test_embedding_model_comparison" \
    --override-ini='addopts=' \
    -v --tb=short \
    --junit-xml=/tmp/gate_granite.xml \
    2>&1
