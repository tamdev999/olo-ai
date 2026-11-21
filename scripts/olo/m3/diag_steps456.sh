#!/bin/bash
# Opus §9 diagnostic steps 4-6
# Run from inside WSL2 (127.0.0.1 probes server on loopback)
set -uo pipefail
cd /mnt/c/oloai

PROBE_COUNT=100
INTERVAL=0.1   # 100ms between probes

probe_url() {
    local url="$1"
    local label="$2"
    local ok=0 reset=0 refused=0 timeout_c=0 empty=0 other=0
    declare -A fails

    echo "=== $PROBE_COUNT probes: $label ($url) ==="
    for i in $(seq 1 $PROBE_COUNT); do
        exit_code=0
        http_code=$(curl -s -o /dev/null -w "%{http_code}" \
            --max-time 2 --connect-timeout 2 "$url" 2>/dev/null) || exit_code=$?

        if   [ $exit_code -eq  0 ] && [ "$http_code" = "200" ]; then ok=$((ok+1))
        elif [ $exit_code -eq 56 ]; then reset=$((reset+1));   fails[$i]="RESET(56)"
        elif [ $exit_code -eq  7 ]; then refused=$((refused+1)); fails[$i]="REFUSED(7)"
        elif [ $exit_code -eq 28 ]; then timeout_c=$((timeout_c+1)); fails[$i]="TIMEOUT(28)"
        elif [ $exit_code -eq 52 ]; then empty=$((empty+1));   fails[$i]="EMPTY(52)"
        else  other=$((other+1));                              fails[$i]="OTHER(exit=$exit_code,http=$http_code)"
        fi
        sleep $INTERVAL
    done

    echo "RESULTS: OK=$ok  RESET=$reset  REFUSED=$refused  TIMEOUT=$timeout_c  EMPTY=$empty  OTHER=$other"
    if [ ${#fails[@]} -gt 0 ]; then
        echo "Failed probes:"
        for idx in $(echo "${!fails[@]}" | tr ' ' '\n' | sort -n); do
            echo "  #$idx: ${fails[$idx]}"
        done
    fi
    echo ""
}

# --- Wait for Hindsight ---
echo "Waiting for Hindsight on 127.0.0.1:18800..."
for i in $(seq 1 20); do
    r=$(curl -s --max-time 3 http://127.0.0.1:18800/health/live 2>/dev/null) || true
    if echo "$r" | grep -q alive; then echo "  Ready ($i * 5s)"; break; fi
    sleep 5
    echo "  Attempt $i/20..."
done

# Confirm Linux process owner
echo "=== Linux listener owner: port 18800 ==="
ss -tlnp | grep 18800 || echo "  (ss shows nothing — may need sudo or server not ready)"
echo ""
echo "=== Hindsight PID (olo_hs) ==="
pid=$(pgrep -u olo_hs -f "hindsight-api" 2>/dev/null | head -1)
echo "  pid=$pid"
if [ -n "$pid" ]; then
    ls /proc/$pid/fd 2>/dev/null | wc -l | xargs echo "  fds="
    cat /proc/$pid/status 2>/dev/null | grep -E 'VmRSS|Threads' | tr '\n' ' '
    echo ""
fi
echo ""

# STEP 4: Plain Python HTTP server on 18799 (control)
echo "=== STEP 4: Plain Python http.server on 18799 (control) ==="
python3 -m http.server 18799 --bind 127.0.0.1 >/dev/null 2>&1 &
PY_PID=$!
sleep 1
echo "  Python server PID=$PY_PID"
ss -tlnp | grep 18799
echo ""
probe_url "http://127.0.0.1:18799/" "plain-python-http-server"
kill $PY_PID 2>/dev/null; wait $PY_PID 2>/dev/null || true

# STEP 5: Hindsight /health/live on loopback
echo "=== STEP 5: Hindsight /health/live on 127.0.0.1:18800 ==="
probe_url "http://127.0.0.1:18800/health/live" "hindsight-loopback"

# STEP 6: Hindsight via WSL physical IP (192.168.0.184)
# Only if it differs from loopback results — restricted to known WSL subnet
WSL_IP="192.168.0.184"
echo "=== STEP 6: Hindsight via WSL IP $WSL_IP:18800 ==="
# Hindsight is bound to 127.0.0.1 only — this will be REFUSED (expected)
# We test it anyway to characterise the difference
probe_url "http://${WSL_IP}:18800/health/live" "hindsight-wsL-ip"

echo "=== Diagnostic complete ==="
