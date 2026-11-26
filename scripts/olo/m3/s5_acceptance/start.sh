#!/usr/bin/env bash
# S5 live acceptance — disposable business set up by the real setup.sh
# (provider deepseek, tenant authentication on), started the way setup.sh
# documents (cwd = business home; Hindsight reads ./.env).
#
# Run as root in WSL.  The DeepSeek key is read from KEY_FILE (a dotenv file
# defining HINDSIGHT_API_LLM_API_KEY) into the server's environment only: it
# is never written to the business .env, never printed and never placed on a
# command line.  Shell builtins copy it into a one-time file (mode 600, owner
# olo_hs) inside the private (700) disposable home; the server's own shell
# sources and deletes that file before exec, so process arguments carry only
# its path.  The model (deepseek-flash, thinking disabled) comes from the .env
# written by the real setup.sh.  Gate server (18800) and production data are
# untouched.
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
grep -E "^HINDSIGHT_API_LLM_(PROVIDER|MODEL|EXTRA_BODY)=" "$BH/.env"
install -d -o olo_hs -g olo_hs "$BH/.pg0"
cp -a "$PG0SRC" "$BH/.pg0/"; chown -R olo_hs:olo_hs "$BH/.pg0"
: > "$HH/server.log"; chown olo_hs:olo_hs "$HH/server.log"

# The key never leaves shell builtins (read, printf): no process sees it in argv.
LLM_KEY=""
while IFS= read -r line || [[ -n "$line" ]]; do
  if [[ "$line" == HINDSIGHT_API_LLM_API_KEY=* ]]; then LLM_KEY="${line#*=}"; break; fi
done < "$KEY_FILE"
unset line
LLM_KEY="${LLM_KEY%$'\r'}"; LLM_KEY="${LLM_KEY#\"}"; LLM_KEY="${LLM_KEY%\"}"
[[ -n "$LLM_KEY" ]] || { echo "HINDSIGHT_API_LLM_API_KEY missing in $KEY_FILE" >&2; exit 1; }
[[ "$LLM_KEY" =~ ^[A-Za-z0-9._-]+$ ]] || { unset LLM_KEY; echo "unexpected characters in the key" >&2; exit 1; }
KEY_ONCE="$HH/.llm_key.once"
( umask 077; printf 'export HINDSIGHT_API_LLM_API_KEY=%s\n' "$LLM_KEY" > "$KEY_ONCE" )
unset LLM_KEY
chown olo_hs:olo_hs "$KEY_ONCE"
sudo -u olo_hs env -i PATH=/usr/bin:/bin HOME="$BH" HF_HOME=/var/cache/huggingface HF_HUB_OFFLINE=1 \
  bash -c ". '$KEY_ONCE' && rm -f '$KEY_ONCE' && cd '$BH' && exec nohup $VENV/bin/hindsight-api --host 127.0.0.1 --port $PORT >> '$HH/server.log' 2>&1" &
UP=0
for i in $(seq 1 60); do
  sleep 3
  if [[ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$PORT/health || true)" == 200 ]]; then
    echo "server up after $((i*3))s"; UP=1; break
  fi
done
if [[ -e "$KEY_ONCE" ]]; then
  rm -f "$KEY_ONCE"; echo "one-time key file was not consumed; stopping" >&2; exit 1
fi
grep -E "LLM: provider|Loaded tenant extension|Connection verified|ERROR" "$HH/server.log" \
  | sed -E 's/(sk-)[A-Za-z0-9-]+/\1<redacted>/g; s/[0-9a-f]{64}/<redacted>/g' | cut -c1-160 | head -6
[[ $UP == 1 ]] || { echo "server did not become healthy; run down.sh and do not run the driver" >&2; exit 1; }
