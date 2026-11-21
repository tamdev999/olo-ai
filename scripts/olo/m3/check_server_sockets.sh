#!/bin/bash
pid=$(pgrep -u olo_hs -f hindsight-api 2>/dev/null | head -1 || true)
[ -z "$pid" ] && echo "No olo_hs hindsight-api process" && exit 0
echo "Server PID: $pid"
echo "=== Socket FDs ==="
ls -la /proc/$pid/fd 2>/dev/null | grep socket
echo ""
echo "=== Socket states ==="
for fd in /proc/$pid/fd/[0-9]*; do
  target=$(readlink "$fd" 2>/dev/null) || continue
  echo "$target" | grep -q 'socket:' || continue
  inode=$(echo "$target" | grep -oP '(?<=\[)\d+(?=\])')
  state=$(grep -P "^ *[0-9A-F]+: .* ${inode} " /proc/net/tcp6 /proc/net/tcp 2>/dev/null | head -1 | awk '{print $4}')
  echo "fd=$(basename $fd) inode=$inode raw_state=$state"
done
echo ""
echo "=== CLOSE_WAIT total ==="
ss -tn state close-wait 2>/dev/null
