#!/usr/bin/env bash
# Stage B: Server reliability test with FD instrumentation.
# Verifies the server stays responsive during all M3 workloads.
set -euo pipefail
cd /mnt/c/oloai
SNAP="bash /mnt/c/oloai/scripts/olo/m3/fd_snapshot.sh"
source /root/olo_linux_venv/bin/activate
source /root/olo_m3_secrets/deepseek.env 2>/dev/null || true
export PYTHONPATH=/mnt/c/oloai
export HINDSIGHT_HOST=127.0.0.1
export HINDSIGHT_PORT=18800
export HINDSIGHT_BANK_ID=main-v1-granite

echo "=== Stage B: Server Reliability + FD Investigation ==="
echo "Timestamp: $(date -u)"
echo ""

$SNAP "BASELINE"

echo ""
echo "--- Phase 1: 20 health probes ---"
ok=0; fail=0
for i in $(seq 1 20); do
    r=$(curl -s --max-time 3 http://127.0.0.1:18800/health/live 2>/dev/null || true)
    if echo "$r" | grep -q alive; then
        ok=$((ok+1))
    else
        fail=$((fail+1))
        echo "  health probe $i FAILED: $r"
    fi
    sleep 0.2
done
echo "  health probes: $ok/20 OK, $fail failed"
$SNAP "AFTER_HEALTH_PROBES"

echo ""
echo "--- Phase 2: Structured ingest (20 items, no LLM) ---"
python3 - <<'EOF'
import sys, uuid, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable
from olo.m3.feeder import STRUCTURED_STRATEGY

cfg = HindsightConfig.from_env()
client = HindsightClient(cfg)
scratch = f"stageb-struct-{uuid.uuid4().hex[:6]}"

# Configure bank first
from olo.m3.feeder import prepare_hindsight_bank
for _ in range(3):
    try:
        prepare_hindsight_bank(client, scratch)
        break
    except Exception:
        time.sleep(3)

items = [
    {
        "content": f"Invoice {i}: amount $500. Status: open. Customer: Acme.",
        "timestamp": "2026-01-15T00:00:00Z",
        "document_id": f"sig:stageb-struct-{i:03d}",
        "entities": [], "tags": ["src:stripe_fixture", "ym:2026-01"],
        "metadata": {"signal_id": f"stgb{i}", "content_sha256": f"stgb{i}",
                     "render_version": "1", "is_llm_eligible": "false"},
        "strategy": STRUCTURED_STRATEGY, "resolve_entities": False,
    }
    for i in range(20)
]
t0 = time.monotonic()
for start in range(0, len(items), 5):
    batch = items[start:start + 5]
    op = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{scratch}-{start}"))
    client.retain(batch, operation_id=op, async_=False, bank_id=scratch)
print(f"  Structured ingest: 20 items in {time.monotonic()-t0:.1f}s")
try:
    client.delete_bank(scratch)
except Exception:
    pass
EOF
$SNAP "AFTER_STRUCT_INGEST"

echo ""
echo "--- Phase 3: Prose ingest with DeepSeek (3 items, real LLM) ---"
python3 - <<'EOF'
import sys, uuid, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable
cfg = HindsightConfig.from_env()
client = HindsightClient(cfg)
scratch = f"stageb-prose-{uuid.uuid4().hex[:6]}"
t0 = time.monotonic()
for i, content in enumerate([
    "Hi, could we reschedule our Q2 review meeting from April 10 to April 17? I have a conflict.",
    "Following up on Invoice INV-2026-042. We paid $1,200 on April 5 but the account still shows unpaid.",
    "Team, the delivery for order ORD-7890 was confirmed for April 22. The original date was April 15.",
]):
    item = {
        "content": content, "timestamp": "2026-04-01T00:00:00Z",
        "document_id": f"sig:stageb-prose-{i}",
        "entities": [], "tags": ["src:gmail_owner", "ym:2026-04"],
        "metadata": {"signal_id": f"sbp{i}", "content_sha256": f"sbp{i}",
                     "render_version": "1", "is_llm_eligible": "true"},
        "resolve_entities": False,
    }
    op = str(uuid.uuid5(uuid.NAMESPACE_OID, item["document_id"]))
    resp = client.retain([item], operation_id=op, async_=False, bank_id=scratch)
    usage = resp.get("usage") or {}
    out_tok = usage.get("output_tokens", 0)
    print(f"  Item {i}: output_tokens={out_tok}")
print(f"  Prose ingest: 3 items in {time.monotonic()-t0:.1f}s")
try:
    client.delete_bank(scratch)
except Exception:
    pass
EOF
$SNAP "AFTER_PROSE_INGEST"

echo ""
echo "--- Phase 4: Concurrent recalls during consolidation ---"
python3 - <<'EOF'
import sys, uuid, time, threading
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable, HindsightAPIError
cfg = HindsightConfig.from_env()
client = HindsightClient(cfg)

# Submit async prose retain (triggers background consolidation)
prose = {
    "content": "Reminder: the Q3 planning meeting is scheduled for July 15. All team leads must attend.",
    "timestamp": "2026-07-01T00:00:00Z",
    "document_id": f"sig:stageb-bg-{uuid.uuid4().hex[:6]}",
    "entities": [], "tags": ["src:gmail_owner", "ym:2026-07"],
    "metadata": {"signal_id": "stageb-bg", "content_sha256": "sbg",
                 "render_version": "1", "is_llm_eligible": "true"},
    "resolve_entities": False,
}
scratch = f"stageb-bg-{uuid.uuid4().hex[:6]}"
client.retain([prose], operation_id=str(uuid.uuid4()), async_=True, bank_id=scratch)

# 10 recalls against main bank while consolidation runs
hits = 0; misses = 0
for i in range(10):
    t1 = time.monotonic()
    try:
        result = client.recall("delivery meeting schedule", max_tokens=1000, trace=False)
        hits += 1
    except HindsightUnavailable as e:
        misses += 1
        print(f"  recall {i+1}: FAIL {str(e)[:60]}")
    time.sleep(0.3)

print(f"  Concurrent recalls: {hits}/10 succeeded, {misses} failed")
try:
    client.delete_bank(scratch)
except Exception:
    pass
EOF
$SNAP "AFTER_CONCURRENT_RECALLS"

echo ""
echo "--- Phase 5: 10 create/retain/delete bank cycles ---"
python3 - <<'EOF'
import sys, uuid, time
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightAPIError
from olo.m3.feeder import prepare_hindsight_bank, STRUCTURED_STRATEGY
cfg = HindsightConfig.from_env()
client = HindsightClient(cfg)

leaks = 0
for cycle in range(10):
    b = f"stageb-cycle-{cycle}-{uuid.uuid4().hex[:4]}"
    try:
        prepare_hindsight_bank(client, b)
        item = {
            "content": f"Cycle {cycle} test item",
            "timestamp": "2026-01-01T00:00:00Z",
            "document_id": f"sig:cy{cycle}",
            "entities": [], "tags": ["src:test"],
            "metadata": {"signal_id": f"cy{cycle}", "content_sha256": f"cy{cycle}",
                         "render_version": "1", "is_llm_eligible": "false"},
            "strategy": STRUCTURED_STRATEGY, "resolve_entities": False,
        }
        client.retain([item], operation_id=str(uuid.uuid4()), async_=False, bank_id=b)
        client.delete_bank(b)
    except Exception as e:
        leaks += 1
        print(f"  cycle {cycle} error: {e}")

print(f"  Bank cycles: 10 completed, {leaks} errors")
EOF
$SNAP "AFTER_BANK_CYCLES"

echo ""
echo "=== Stage B complete ==="
