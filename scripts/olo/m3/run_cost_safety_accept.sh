#!/usr/bin/env bash
set -euo pipefail
source /root/olo_m3_secrets/deepseek.env
export HINDSIGHT_API_LLM_API_KEY
export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
cd /mnt/c/oloai
source /root/olo_linux_venv/bin/activate
exec python scripts/olo/m3/live_cost_safety_accept.py
