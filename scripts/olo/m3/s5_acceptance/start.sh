#!/usr/bin/env bash
# S5 live acceptance — disposable business set up by the real setup.sh
# (provider deepseek, tenant authentication on), started the way setup.sh
# documents (cwd = business home; Hindsight reads ./.env).
#
# Run as root in WSL.  The DeepSeek key is read from KEY_FILE (a dotenv file
# defining HINDSIGHT_API_LLM_API_KEY) into the server's environment only; it
# is never written to the business .env and never printed.  The Hindsight
# model is pinned to deepseek-chat (the model the gate used and the usage
# guard's prices are based on); Hindsight's own default would be
# deepseek-v4-flash.  Gate server (18800) and production data are untouched.
set -euo pipefail
KEY_FILE="${KEY_FILE:-/root/olo_m3_secrets/deepseek.env}"
HH=/home/olo_hs/s5accept_hermes
PORT=18899
VENV=/root/olo_m3_hindsight_venv
PG0SRC=/home/olo_hs/mig_accept_home/.pg0/installation
HERE="$(cd "$(dirname "$0")" && pwd)"
SETUP="$HERE/../../../../olo/m3/hindsight/setup.sh"

[[ -r "$KEY_FILE" ]] || { echo "key file not readable: $KEY_FILE" >&2; exit 1; }
pkill -u olo_hs -f "hindsight-api --host 127.0.0.1 --port ${PORT}" 2>/dev/null || true
rm -rf "$HH" /root/olo_s5accept_db
install -d -o olo_hs -g olo_hs -m 700 "$HH" "$HH/olo"
ln -s "$VENV" "$HH/olo/hindsight-venv"
cp "$SETUP" "$HH/setup.sh"; chown olo_hs:olo_hs "$HH/setup.sh"

sudo -u olo_hs env -i PATH=/usr/bin:/bin HOME=/home/olo_hs HERMES_HOME="$HH" \
  HF_HOME=/var/cache/huggingface HINDSIGHT_PORT="$PORT" HINDSIGHT_PYTHON="$VENV/bin/python3" \
  bash "$HH/setup.sh" s5accept --provider=deepseek | grep -E "Provenance|Tenant|Bank ID" || true
BH="$HH/businesses/s5accept"
install -d -o olo_hs -g olo_hs "$BH/.pg0"
cp -a "$PG0SRC" "$BH/.pg0/"; chown -R olo_hs:olo_hs "$BH/.pg0"
: > "$HH/server.log"; chown olo_hs:olo_hs "$HH/server.log"

LLM_KEY=$(sed -n 's/^HINDSIGHT_API_LLM_API_KEY=//p' "$KEY_FILE" | head -1)
[[ -n "$LLM_KEY" ]] || { echo "HINDSIGHT_API_LLM_API_KEY missing in $KEY_FILE" >&2; exit 1; }
sudo -u olo_hs env -i PATH=/usr/bin:/bin HOME="$BH" HF_HOME=/var/cache/huggingface HF_HUB_OFFLINE=1 \
  HINDSIGHT_API_LLM_API_KEY="$LLM_KEY" HINDSIGHT_API_LLM_MODEL=deepseek-chat \
  bash -c "cd '$BH' && exec nohup $VENV/bin/hindsight-api --host 127.0.0.1 --port $PORT >> '$HH/server.log' 2>&1" &
unset LLM_KEY
for i in $(seq 1 60); do
  sleep 3
  [[ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$PORT/health || true)" == 200 ]] \
    && { echo "server up after $((i*3))s"; break; }
done
grep -E "LLM: provider|Loaded tenant extension|Connection verified|ERROR" "$HH/server.log" \
  | sed -E 's/(sk-)[A-Za-z0-9-]+/\1<redacted>/g; s/[0-9a-f]{64}/<redacted>/g' | cut -c1-160 | head -6
