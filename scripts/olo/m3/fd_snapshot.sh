#!/usr/bin/env bash
# fd_snapshot.sh — capture open-FD inventory for the Hindsight server.
# Usage: bash scripts/olo/m3/fd_snapshot.sh [label]
# Records: total count, soft/hard limit, breakdown by type, CLOSE_WAIT sockets.
set -euo pipefail
LABEL="${1:-snapshot}"
PID=$(pgrep -u olo_hs -f hindsight-api 2>/dev/null | head -1 || true)

if [ -z "$PID" ]; then
    echo "[$LABEL] NO SERVER (pgrep found nothing)"
    exit 0
fi

FD_COUNT=$(ls /proc/"$PID"/fd 2>/dev/null | wc -l)
SOFT_LIMIT=$(awk '/Max open files/{print $4}' /proc/"$PID"/limits 2>/dev/null)
HARD_LIMIT=$(awk '/Max open files/{print $5}' /proc/"$PID"/limits 2>/dev/null)
RSS_MB=$(awk '/VmRSS/{printf "%d", $2/1024}' /proc/"$PID"/status 2>/dev/null)
THREADS=$(awk '/^Threads/{print $2}' /proc/"$PID"/status 2>/dev/null)

SOCKETS=$(ls -la /proc/"$PID"/fd 2>/dev/null | grep -c 'socket' || true)
PIPES=$(ls -la /proc/"$PID"/fd 2>/dev/null | grep -c 'pipe' || true)
ONNX=$(ls -la /proc/"$PID"/fd 2>/dev/null | grep -c '\.onnx' || true)

CLOSE_WAIT=$(ss -tn 2>/dev/null | grep -c CLOSE-WAIT || true)
HEALTH=$(curl -s --max-time 2 http://127.0.0.1:18800/health/live 2>/dev/null | grep -o '"status":"[^"]*"' | head -1 || echo "UNREACHABLE")

echo "[$LABEL] pid=$PID fds=$FD_COUNT (soft=$SOFT_LIMIT hard=$HARD_LIMIT) rss=${RSS_MB}MB threads=$THREADS | sockets=$SOCKETS pipes=$PIPES onnx=$ONNX close_wait=$CLOSE_WAIT | health=$HEALTH"
