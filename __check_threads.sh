#!/bin/bash
pid=$1
echo "Thread states for pid $pid:"
declare -A counts
for tid in $(ls /proc/$pid/task/ 2>/dev/null); do
    wchan=$(cat /proc/$pid/task/$tid/wchan 2>/dev/null)
    counts["$wchan"]=$((${counts["$wchan"]:-0} + 1))
done
for state in "${!counts[@]}"; do
    echo "  ${counts[$state]}x $state"
done | sort -rn
