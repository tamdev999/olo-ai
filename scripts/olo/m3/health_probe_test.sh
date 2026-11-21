#!/bin/bash
ok=0; fail=0
for i in $(seq 1 20); do
    r=$(curl -s --max-time 2 http://127.0.0.1:18800/health/live 2>/dev/null || true)
    if echo "$r" | grep -q alive; then
        ok=$((ok+1))
    else
        fail=$((fail+1))
    fi
    sleep 0.2
done
echo "Health probes (20x, 0.2s interval): $ok/20 OK, $fail failed"
