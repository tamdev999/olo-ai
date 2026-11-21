#!/bin/bash
# Probe Hindsight while DeepSeek consolidation runs in background.
# This simulates the conditions under which the previous gate runs failed.
set -uo pipefail
cd /mnt/c/oloai
source /root/olo_linux_venv/bin/activate
source /root/olo_m3_secrets/deepseek.env 2>/dev/null || true
export PYTHONPATH=/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT=18800

echo "=== Load test: probe health/live while DeepSeek consolidation runs ==="

# Start background consolidation (async prose retain → triggers DeepSeek)
python3 - <<'PYEOF'
import sys, uuid, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
from olo.m3.feeder import STRUCTURED_STRATEGY
cfg = HindsightConfig.from_env()
client = HindsightClient(cfg)
scratch = f"loadtest-{uuid.uuid4().hex[:6]}"
# Submit 5 prose items asynchronously to trigger background DeepSeek consolidation
for i in range(5):
    item = {
        "content": f"Meeting rescheduled from March {i+1} to March {i+8}. Customer confirmed. Ref REF-{i:04d}.",
        "timestamp": "2026-03-01T00:00:00Z",
        "document_id": f"sig:load-prose-{i:03d}-{uuid.uuid4().hex[:6]}",
        "entities": [], "tags": ["src:gmail_owner", "ym:2026-03"],
        "metadata": {"signal_id": f"lp{i}", "content_sha256": f"lp{i}", "render_version": "1", "is_llm_eligible": "true"},
        "resolve_entities": False,
    }
    client.retain([item], operation_id=str(uuid.uuid4()), async_=True, bank_id=scratch)
print(f"5 prose items queued for consolidation in bank {scratch}")
# Leave scratch bank for cleanup
with open('/tmp/loadtest_bank.txt', 'w') as f:
    f.write(scratch)
PYEOF

echo "Waiting 2s for consolidation to start, then probing..."
sleep 2

# Run 200 probes while consolidation is running
ok=0; reset=0; refused=0; timeout_c=0; other=0
for i in $(seq 1 200); do
    exit_code=0
    http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 --connect-timeout 2 \
        http://127.0.0.1:18800/health/live 2>/dev/null) || exit_code=$?
    if   [ $exit_code -eq  0 ] && [ "$http_code" = "200" ]; then ok=$((ok+1))
    elif [ $exit_code -eq 56 ]; then reset=$((reset+1));    echo "  #$i RESET(56)"
    elif [ $exit_code -eq  7 ]; then refused=$((refused+1)); echo "  #$i REFUSED(7)"
    elif [ $exit_code -eq 28 ]; then timeout_c=$((timeout_c+1)); echo "  #$i TIMEOUT(28)"
    else  other=$((other+1));                                echo "  #$i OTHER(exit=$exit_code,http=$http_code)"
    fi
    sleep 0.15
done
echo "RESULTS (under load): OK=$ok  RESET=$reset  REFUSED=$refused  TIMEOUT=$timeout_c  OTHER=$other"

# Cleanup
scratch=$(cat /tmp/loadtest_bank.txt 2>/dev/null || true)
if [ -n "$scratch" ]; then
    python3 -c "
import sys; sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig
c = HindsightClient(HindsightConfig.from_env())
try: c.delete_bank('$scratch')
except: pass
print('Bank cleaned up')
" 2>/dev/null || true
fi
