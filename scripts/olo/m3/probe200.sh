#!/bin/bash
set -uo pipefail
ok=0; reset=0; refused=0; timeout_c=0; other=0
echo "=== 200 rapid probes with TIME_WAIT accumulated ==="
for i in $(seq 1 200); do
    exit_code=0
    http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 --connect-timeout 2 \
        http://127.0.0.1:18800/health/live 2>/dev/null) || exit_code=$?
    if   [ $exit_code -eq  0 ] && [ "$http_code" = "200" ]; then ok=$((ok+1))
    elif [ $exit_code -eq 56 ]; then reset=$((reset+1));    echo "  #$i RESET(56)"
    elif [ $exit_code -eq  7 ]; then refused=$((refused+1)); echo "  #$i REFUSED(7)"
    elif [ $exit_code -eq 28 ]; then timeout_c=$((timeout_c+1)); echo "  #$i TIMEOUT(28)"
    else  other=$((other+1));                                echo "  #$i OTHER(exit=$exit_code,http=$http_code)"
    fi
    sleep 0.1
done
echo "RESULTS: OK=$ok  RESET=$reset  REFUSED=$refused  TIMEOUT=$timeout_c  OTHER=$other"
