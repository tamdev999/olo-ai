#!/usr/bin/env bash
# Run ONLY the V10 embedding model comparison benchmark.
# This is separated from the main gate because it creates three isolated
# Hindsight instances and takes 20–25 minutes to complete.
# Expected result: XFAIL (embedding alone achieves ~68%, hs-rerank-1 needed).
set -euo pipefail

BANK_ID="${HINDSIGHT_BANK_ID:-main-v1-granite}"
PORT="${HINDSIGHT_PORT:-18800}"

echo "=== V10 Embedding Benchmark ==="
echo "  Bank:  ${BANK_ID}"
echo "  Port:  ${PORT}"
echo "  Date:  $(date -u)"
echo ""

source /root/olo_linux_venv/bin/activate

export PYTHONPATH=/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT="${PORT}"
export HINDSIGHT_BANK_ID="${BANK_ID}"
export HINDSIGHT_BIN=/root/olo_m3_hindsight_venv/bin/hindsight-api
export OLO_DB_PATH=/root/gate_olo.db
export OLO_GATE_STRICT=1

cd /mnt/c/oloai

python3 -m pytest tests/olo/m3/test_m3_gate.py \
    -m integration \
    -k "TestV10Footprint" \
    -v --tb=short \
    --junit-xml=/tmp/gate_v10.xml \
    2>&1
