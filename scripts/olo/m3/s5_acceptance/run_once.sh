#!/usr/bin/env bash
# S5 live acceptance in ONE WSL invocation: start.sh, then driver.py (only if
# the server came up), then down.sh whatever happened.
#
# One invocation because WSL shuts an idle distro down — and the Hindsight
# server with it — about a minute after the last wsl command returns
# (observed 2026-11-26: a background process did not keep it running across a
# 90 s gap).  Separate start/driver commands would race that shutdown.
#
# Usage (WSL, root):  bash run_once.sh [evidence.json] [redacted-server.log]
# Exit status: start.sh's when it failed, else the driver's (0 = every check
# passed, 1 = a recorded failure, 2/3/4 = stopped before any paid step).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
EVIDENCE="${1:-/root/s5_acceptance_evidence.json}"
LOG="${2:-/root/s5_acceptance_server.redacted.log}"

bash "$HERE/start.sh"
rc=$?
if [[ $rc == 0 ]]; then
  env -u DEEPSEEK_API_KEY -u HINDSIGHT_API_LLM_API_KEY /root/olo_linux_venv/bin/python "$HERE/driver.py" "$EVIDENCE"
  rc=$?
else
  echo "start.sh failed (exit $rc): driver not run, nothing paid beyond the server's startup check" >&2
fi
bash "$HERE/down.sh" "$LOG"
echo "run_once exit: $rc"
exit $rc
