"""Minimal reproduction for the ECONNRESET failure.

Reproduces the connection-reset pattern seen during gate tests:
  Phase A: Trigger DeepSeek consolidation (mimics V9 prose test).
  Phase B: Fire rapid recall requests while consolidation runs (mimics V1).

Captures:
  - Server stdout/stderr (via /tmp/hs_server_diag.log)
  - Exact request that gets reset
  - Whether the server process is still alive
  - RSS and thread count before/during/after
  - Socket state (ESTABLISHED/TIME_WAIT/CLOSE_WAIT)

Run with: source /root/olo_linux_venv/bin/activate && python3 __reproduce_reset.py
"""
from __future__ import annotations
import os
import sys
import time
import uuid
import subprocess
import threading
import signal

sys.path.insert(0, '/mnt/c/oloai')

from olo.m3.hindsight.client import (
    HindsightClient, HindsightConfig, HindsightAPIError, HindsightUnavailable,
)

PORT = 18800
BANK_ID = 'main-v1'
SCRATCH_BANK = f"repro-{uuid.uuid4().hex[:8]}"

cfg = HindsightConfig(host='127.0.0.1', port=PORT, bank_id=BANK_ID, timeout_s=30)
client = HindsightClient(cfg)

# ── helpers ──────────────────────────────────────────────────────────────────

def server_pid() -> int | None:
    r = subprocess.run(
        ['pgrep', '-f', 'hindsight-api'],
        capture_output=True, text=True,
    )
    pids = [int(p) for p in r.stdout.split() if p.isdigit()]
    return pids[0] if pids else None

def server_rss(pid: int) -> int:
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS'):
                    return int(line.split()[1])  # kB
    except Exception:
        pass
    return -1

def server_threads(pid: int) -> int:
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('Threads'):
                    return int(line.split()[1])
    except Exception:
        pass
    return -1

def socket_counts() -> dict:
    r = subprocess.run(
        ['ss', '-tn', 'dst', '127.0.0.1:18800'],
        capture_output=True, text=True,
    )
    states: dict[str, int] = {}
    for line in r.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            state = parts[0]
            states[state] = states.get(state, 0) + 1
    return states

def pg_conn_count() -> int:
    r = subprocess.run(
        ['psql', '-U', 'hindsight', '-t', '-c',
         'SELECT count(*) FROM pg_stat_activity'],
        capture_output=True, text=True,
        env={**os.environ, 'PGHOST': '/var/run/postgresql'},
    )
    try:
        return int(r.stdout.strip())
    except Exception:
        return -1

# ── diagnostic snapshot ───────────────────────────────────────────────────────

def snapshot(label: str) -> None:
    pid = server_pid()
    rss = server_rss(pid) // 1024 if pid else -1
    thr = server_threads(pid) if pid else -1
    socks = socket_counts()
    pg = pg_conn_count()
    print(f"[{label}] pid={pid} rss={rss}MB threads={thr} sockets={socks} pg_conns={pg}")

# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("=== Connection-Reset Reproduction ===")
    print(f"Server: port={PORT} bank={BANK_ID}")
    print(f"Scratch bank: {SCRATCH_BANK}")

    # ── Phase 0: baseline ────────────────────────────────────────────────────
    client.wait_until_ready(10.0)
    snapshot("BASELINE")

    from olo.m3.synthetic.generator import ENTITIES
    ent = ENTITIES[0]

    # ── Phase A: trigger DeepSeek consolidation (mimics V9 prose test) ───────
    print("\n--- Phase A: trigger prose consolidation (mimics V9) ---")
    prose_item = {
        "content": (
            "Agent: Olo support. Customer: Hi, I need to reschedule my meeting "
            "originally planned for April 04 to April 08. Agent: Done. "
            "Reference: ORD-TEST-001."
        ),
        "timestamp": "2026-04-01T10:00:00Z",
        "document_id": f"sig:repro-prose-{uuid.uuid4().hex[:8]}",
        "entities": [{"text": ent.entity_id, "type": "person"}],
        "tags": [f"ent:{ent.entity_id}", "ym:2026-04", "src:voice_fixture",
                 "kind:call.transcript", "dc:voice"],
        "strategy": "prose",
        "metadata": {
            "signal_id": "repro001", "source": "repro",
            "kind": "call.transcript", "is_llm_eligible": "True",
            "content_sha256": "0" * 64,
        },
    }
    op_a = str(uuid.uuid4())
    t0 = time.monotonic()
    try:
        resp_a = client.retain([prose_item], operation_id=op_a,
                               async_=True, bank_id=SCRATCH_BANK)
        print(f"  Prose retain submitted (op={op_a[:8]}…) in {time.monotonic()-t0:.2f}s")
    except Exception as e:
        print(f"  Prose retain FAILED: {e}")
        return

    snapshot("AFTER-V9-RETAIN")

    # ── Phase B: rapid recalls while consolidation runs (mimics V1) ──────────
    print("\n--- Phase B: 20 rapid recalls while consolidation runs (mimics V1) ---")

    errors: list[tuple[int, str]] = []
    times: list[float] = []
    tg = [{"tags": [f"ent:{ent.entity_id}"], "match": "all_strict"}]

    # Fire recalls immediately without waiting for consolidation to drain
    for i in range(20):
        t1 = time.monotonic()
        try:
            rr = client.recall(
                f"delivery reschedule April request {ent.company}",
                tag_groups=tg, max_tokens=4000, trace=False,
            )
            elapsed = time.monotonic() - t1
            times.append(elapsed)
        except HindsightUnavailable as e:
            elapsed = time.monotonic() - t1
            errors.append((i, str(e)))
            print(f"  recall {i+1}/20: RESET at {elapsed:.2f}s — {e}")
        except Exception as e:
            elapsed = time.monotonic() - t1
            errors.append((i, str(e)))
            print(f"  recall {i+1}/20: ERROR at {elapsed:.2f}s — {e}")

        snapshot(f"RECALL-{i+1}")

        if errors and i == 0:
            # First recall already failed — capture server state immediately
            pid = server_pid()
            print(f"\n  FIRST-RECALL-RESET diagnostic:")
            print(f"    Server alive: {pid is not None}")
            print(f"    RSS: {server_rss(pid)//1024 if pid else '?'} MB")
            print(f"    Socket state: {socket_counts()}")
            subprocess.run(
                ['sh', '-c',
                 f'tail -20 /tmp/hindsight_*.log 2>/dev/null || echo "no log file"']
            )
            break

    if times:
        avg = sum(times) / len(times)
        print(f"\n  Completed: {len(times)}/{20} recalls, avg={avg:.2f}s")
    if errors:
        print(f"  Errors: {len(errors)}/{20}")
        for idx, msg in errors[:3]:
            print(f"    recall {idx+1}: {msg[:100]}")

    snapshot("AFTER-RECALLS")

    # ── Phase C: wait for consolidation and re-test ───────────────────────────
    print("\n--- Phase C: wait 15s, then re-test ---")
    time.sleep(15)
    snapshot("AFTER-WAIT")

    errors_c = 0
    for i in range(5):
        try:
            client.recall(
                f"delivery reschedule {ent.company}",
                tag_groups=tg, max_tokens=4000, trace=False,
            )
        except Exception as e:
            errors_c += 1
            print(f"  post-wait recall {i+1}: {e}")
    print(f"  Post-wait errors: {errors_c}/5")

    # ── cleanup ───────────────────────────────────────────────────────────────
    try:
        client.delete_bank(SCRATCH_BANK)
    except Exception:
        pass

    print("\n=== Reproduction complete ===")
    if errors:
        print(f"REPRODUCED: {len(errors)} connection resets during rapid recalls after prose retain")
    else:
        print("NOT REPRODUCED: no connection resets")


if __name__ == '__main__':
    main()
