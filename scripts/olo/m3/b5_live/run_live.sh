#!/usr/bin/env bash
# Post-audit batch 5 — the real pinned Hindsight 1.2.1 under Olo's supervisor,
# on a disposable business and bank.  Run as root in WSL.
#
# * The business is created by the real setup.sh (provider none: no LLM is
#   configured, so no paid call can be made) and its server is started,
#   supervised, killed and restarted by Olo (olo.hindsight_service).
# * By default everything runs inside a private network namespace: the server
#   and Olo share a kernel loopback, and nothing can reach the internet.
#   NETNS=default runs in WSL's default namespace instead (the one the live
#   Google run uses, with the VirtioProxy loopback relay); its evidence goes to
#   evidence_wsl_default_namespace.json.  Either way no model is downloaded
#   (HF_HUB_OFFLINE=1, local cache) and no API is called (provider none).
# * Only Calendar-shaped data is projected: calendar events and ledger
#   states are structured documents (no LLM extraction).
# * pg0 refuses to run as root, so the business runs as olo_hs (as in S5).
#
# Evidence (no keys) is written to $OUT/evidence.json; the disposable home is
# removed afterwards.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
HH=/home/olo_hs/b5live_hermes
PORT="${PORT:-18911}"
VENV=/root/olo_m3_hindsight_venv
OLO_PY=/root/olo_linux_venv/bin/python
PG0SRC="${PG0SRC:-/home/olo_hs/mig_accept_home/.pg0/installation}"
OUT="${OUT:-$REPO/docs/olo/m3/b5_live}"

rm -rf "$HH"
install -d -o olo_hs -g olo_hs -m 700 "$HH" "$HH/olo"
ln -s "$VENV" "$HH/olo/hindsight-venv"
cp "$REPO/olo/m3/hindsight/setup.sh" "$HH/setup.sh"
chown olo_hs:olo_hs "$HH/setup.sh"
mkdir -p "$OUT"

NETNS="${NETNS:-private}"
case "$NETNS" in
    private) NS=(unshare -n); EVIDENCE="$OUT/evidence.json" ;;
    default) NS=(); EVIDENCE="$OUT/evidence_wsl_default_namespace.json" ;;
    *) echo "NETNS must be private or default" >&2; exit 2 ;;
esac

status=0
"${NS[@]}" env REPO="$REPO" HH="$HH" PORT="$PORT" VENV="$VENV" OLO_PY="$OLO_PY" \
    PG0SRC="$PG0SRC" EVIDENCE="$EVIDENCE" NETNS="$NETNS" bash -c '
set -euo pipefail
[ "$NETNS" = default ] || ip link set lo up
as_olo() { setpriv --reuid=olo_hs --regid=olo_hs --init-groups "$@"; }
as_olo env -i PATH=/usr/bin:/bin HOME=/home/olo_hs HERMES_HOME="$HH" HINDSIGHT_PORT="$PORT" \
    HINDSIGHT_PYTHON="$VENV/bin/python3" HF_HOME=/var/cache/huggingface HF_HUB_OFFLINE=1 \
    bash "$HH/setup.sh" b5live --provider=none | grep -E "Business|Provenance|Tenant|Bank ID" || true
BH="$HH/businesses/b5live"
grep -E "^HINDSIGHT_API_(LLM_PROVIDER|TENANT_EXTENSION|PORT)=" "$BH/.env"
install -d -o olo_hs -g olo_hs "$BH/.pg0"
cp -a "$PG0SRC" "$BH/.pg0/"
chown -R olo_hs:olo_hs "$BH/.pg0"
as_olo env -i PATH=/usr/bin:/bin HOME=/home/olo_hs HERMES_HOME="$HH" PYTHONPATH="$REPO" \
    HF_HUB_OFFLINE=1 "$OLO_PY" "$REPO/scripts/olo/m3/b5_live/driver.py" --out "$EVIDENCE"
' || status=$?
rm -rf "$HH"
exit $status
