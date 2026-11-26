#!/usr/bin/env bash
# Stop the S5 acceptance server and delete every disposable file it used.
# The server log is copied (keys redacted) to $1 when given.
HH=/home/olo_hs/s5accept_hermes
if [[ -n "${1:-}" && -f "$HH/server.log" ]]; then
  sed -E 's/(sk-)[A-Za-z0-9-]+/\1<redacted>/g; s/[0-9a-f]{64}/<redacted>/g' "$HH/server.log" > "$1"
fi
pkill -u olo_hs -f "hindsight-api --host 127.0.0.1 --port 18899" 2>/dev/null; sleep 3
pkill -u olo_hs -f "s5accept_hermes" 2>/dev/null; sleep 1
rm -rf "$HH" /root/olo_s5accept_db
ls -d "$HH" /root/olo_s5accept_db 2>/dev/null && echo "WARNING: not removed" || echo "disposable data removed"
ss -ltn | grep -q ":18899 " && echo "WARNING: port 18899 still bound" || echo "port 18899 free"
