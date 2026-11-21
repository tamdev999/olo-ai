#!/bin/bash
for i in 1 2 3 4 5 6 7 8; do
    echo -n "attempt $i ($(date +%H:%M:%S)): "
    result=$(curl -s --max-time 5 http://127.0.0.1:18800/health/live 2>&1)
    code=$?
    if [ $code -eq 0 ] && [ -n "$result" ]; then
        echo "OK: $result"
    elif [ $code -eq 28 ]; then
        echo "TIMEOUT"
    elif [ $code -eq 56 ]; then
        echo "ECONNRESET"
    else
        echo "FAIL code=$code output=$result"
    fi
    sleep 2
done
