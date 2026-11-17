#!/usr/bin/env bash
set -euo pipefail
export PATH=/root/olo_linux_venv/bin:/usr/bin:/bin:$PATH
export HERMES_HOME=/root/olo_live_home
python3 '/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/phase_b_final_checks.py'
