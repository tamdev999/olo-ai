#!/bin/bash
for i in $(seq 1 15); do
    sleep 5
    result=$(curl -s --max-time 3 http://127.0.0.1:18800/health/live)
    if echo "$result" | grep -q alive; then
        echo "Server ready at attempt $i (${i}0s)"
        exit 0
    fi
    echo "Waiting... $i/15 (${i}0s)"
done
echo "Server not ready after 75s"
exit 1
