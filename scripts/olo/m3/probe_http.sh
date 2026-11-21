#!/bin/bash
# Probe HTTP server: run 100 consecutive curl requests and record exact exit codes.
# Usage: bash probe_http.sh <URL> <label>
URL="${1:-http://127.0.0.1:18799/}"
LABEL="${2:-test}"
TOTAL=100

ok=0; reset=0; refused=0; timeout_c=0; other=0
declare -A codes

echo "=== 100 HTTP probes against $URL ($LABEL) ==="
for i in $(seq 1 $TOTAL); do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 --connect-timeout 2 "$URL" 2>/dev/null)
    curl_exit=$?
    if [ $curl_exit -eq 0 ] && [ "$code" = "200" ]; then
        ok=$((ok+1))
    elif [ $curl_exit -eq 56 ]; then
        reset=$((reset+1))
        codes[$i]="RESET"
    elif [ $curl_exit -eq 7 ]; then
        refused=$((refused+1))
        codes[$i]="REFUSED"
    elif [ $curl_exit -eq 28 ]; then
        timeout_c=$((timeout_c+1))
        codes[$i]="TIMEOUT"
    else
        other=$((other+1))
        codes[$i]="OTHER(exit=$curl_exit,http=$code)"
    fi
    sleep 0.1
done

echo "Results: OK=$ok RESET=$reset REFUSED=$refused TIMEOUT=$timeout_c OTHER=$other"
echo ""
if [ ${#codes[@]} -gt 0 ]; then
    echo "Failed probe indices and reasons:"
    for idx in $(echo "${!codes[@]}" | tr ' ' '\n' | sort -n); do
        echo "  probe $idx: ${codes[$idx]}"
    done
fi
