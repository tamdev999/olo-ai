#!/usr/bin/env bash
# wait_server.sh — wait for Hindsight to become healthy on port 18800.
set -euo pipefail
MAX="${1:-15}"
for i in $(seq 1 "$MAX"); do
    sleep 5
    result=$(curl -s --max-time 3 http://127.0.0.1:18800/health/live 2>/dev/null || true)
    if echo "$result" | grep -q alive; then
        echo "Server ready at attempt $i ($((i*5))s)"
        exit 0
    fi
    echo "Waiting... $i/$MAX ($((i*5))s)"
done
echo "Server not ready after $((MAX*5))s" >&2
exit 1
