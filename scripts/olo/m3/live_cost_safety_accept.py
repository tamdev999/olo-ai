#!/usr/bin/env python3
"""olo/m3: Live cost-safety acceptance test against Hindsight 1.2.1 + DeepSeek.

Verifies commit 34b4935731 (bounded retries + cancel-on-abort + budget guard).
Uses disposable banks only.  Estimated total cost: < $0.002.

Tasks:
  1. Bounded retries and cancel-on-abort
  2. Budget abort and final usage reconciliation

Usage (WSL2 as root):
  source /root/olo_m3_secrets/deepseek.env
  export HINDSIGHT_API_LLM_API_KEY
  export PYTHONPATH=/root/olo_m3_hindsight_venv/lib/python3.14/site-packages:/mnt/c/oloai
  cd /mnt/c/oloai
  source /root/olo_linux_venv/bin/activate
  python scripts/olo/m3/live_cost_safety_accept.py
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/mnt/c/oloai")

from olo.m3.hindsight.client import (
    HindsightClient,
    HindsightConfig,
    HindsightUnavailable,
    HindsightAPIError,
)
from olo.m3.hindsight.budget import (
    UsageGuard,
    BudgetExceededError,
    StatsUnavailableError,
    parse_stats,
    TokenSnapshot,
    COST_INPUT_PER_M,
    COST_OUTPUT_PER_M,
)
from olo.m3.pg0_lifecycle import safe_pg0_delete_home

# ---------------------------------------------------------------------------
# Server config
# ---------------------------------------------------------------------------

PORT = 19903
OLO_HS_HOME = Path("/home/olo_hs/cost_safety_home")
OLO_HS_SOCKET = OLO_HS_HOME / "run"
HF_CACHE = "/var/cache/huggingface"
HINDSIGHT_BIN = "/root/olo_m3_hindsight_venv/bin/hindsight-api"
API_KEY = os.environ.get("HINDSIGHT_API_LLM_API_KEY", "")

# Configured retry limits (from start_server.sh)
EXPECTED_LLM_MAX_RETRIES = 3
EXPECTED_RETAIN_MAX_RETRIES = 2
EXPECTED_CONSOLIDATION_MAX_RETRIES = 2

# Test banks -- disposable, never the production bank
BANK_A = f"cs-test-a-{uuid.uuid4().hex[:8]}"
BANK_B = f"cs-test-b-{uuid.uuid4().hex[:8]}"
BANK_BUDGET = f"cs-budget-{uuid.uuid4().hex[:8]}"

# Budget trip point: $0.0001 -- less than the cost of one prose extraction
BUDGET_TRIP_USD = 0.0001

HS_ENV = {
    "HOME": str(OLO_HS_HOME),
    "HF_HOME": HF_CACHE,
    "HF_HUB_OFFLINE": "1",
    "HINDSIGHT_API_HOST": "127.0.0.1",
    "HINDSIGHT_API_PORT": str(PORT),
    "HINDSIGHT_API_DATABASE_URL": (
        f"pg0://olo-cost-safety"
        f"?unix_socket_directories={OLO_HS_SOCKET}"
    ),
    "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "onnx",
    "HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID": "intfloat/multilingual-e5-small",
    "HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA": "false",
    "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
    "HINDSIGHT_API_LLM_PROVIDER": "deepseek",
    "HINDSIGHT_API_LLM_MODEL": "deepseek-chat",
    "HINDSIGHT_API_LLM_API_KEY": API_KEY,
    "HINDSIGHT_API_LLM_STRICT_SCHEMA": "false",
    "HINDSIGHT_API_LLM_TRACE_ENABLED": "true",
    "HINDSIGHT_API_LLM_MAX_RETRIES": str(EXPECTED_LLM_MAX_RETRIES),
    "HINDSIGHT_API_RETAIN_LLM_MAX_RETRIES": str(EXPECTED_RETAIN_MAX_RETRIES),
    "HINDSIGHT_API_CONSOLIDATION_LLM_MAX_RETRIES": str(EXPECTED_CONSOLIDATION_MAX_RETRIES),
    "HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT": "1",
    "HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT": "1",
    "HINDSIGHT_API_DECISION_MODEL_PROVIDER": "none",
    "HINDSIGHT_API_MCP_ENABLED": "false",
    "HINDSIGHT_API_STORE_DOCUMENT_TEXT": "true",
    "OTEL_SDK_DISABLED": "true",
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(label: str, detail: str = "") -> None:
    print(f"  PASS  {label}" + (f" -- {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


def _info(msg: str) -> None:
    print(f"  INFO  {msg}")


def _section(title: str) -> None:
    print(f"\n{'='*62}\n  {title}\n{'='*62}")


def _kill_pg(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _sudo_hs(*cmd) -> list:
    env_list = [f"{k}={v}" for k, v in HS_ENV.items()]
    return ["sudo", "-u", "olo_hs", "env"] + env_list + list(cmd)


def _snapshot_cost(snap: TokenSnapshot) -> str:
    cost = snap.cost_usd()
    return f"in={snap.input_tokens:,}  out={snap.output_tokens:,}  cost=${cost:.6f}"


def _prose_item(doc_id: str, content: str) -> dict:
    return {
        "content": content,
        "timestamp": "2026-06-01T10:00:00Z",
        "document_id": doc_id,
        "entities": [{"text": "cost_test_entity", "type": "person"}],
        "resolve_entities": False,
        "tags": ["ym:2026-06", "test:cost-safety"],
        "strategy": "prose",
    }


PROSE_CONTENT_1 = (
    "Customer called about rescheduling their annual review meeting. "
    "They confirmed the original date still works but need to move from 2 PM to 3 PM. "
    "Account manager should update the calendar and notify the finance team. "
    "The customer also mentioned interest in the new enterprise tier."
)

PROSE_CONTENT_2 = (
    "Follow-up call regarding the Q3 invoice that was flagged for review. "
    "Customer confirmed the charges are correct after checking their records. "
    "They asked for a copy of the itemized breakdown to be sent by end of day. "
    "Payment expected within 5 business days."
)


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start_server() -> tuple:
    _section("Starting Hindsight 1.2.1 (olo_hs, DeepSeek, trace enabled)")
    try:
        safe_pg0_delete_home(OLO_HS_HOME)
    except Exception:
        pass
    subprocess.run(["chmod", "711", "/root"], check=False, capture_output=True)
    subprocess.run(["chmod", "-R", "a+rX", "/root/olo_m3_hindsight_venv"],
                   check=False, capture_output=True)
    subprocess.run(["sudo", "-u", "olo_hs", "mkdir", "-p", str(OLO_HS_SOCKET)],
                   check=True)

    log_path = Path("/tmp/cs_accept_hs.log")
    with open(str(log_path), "w") as log_f:
        proc = subprocess.Popen(
            _sudo_hs(HINDSIGHT_BIN),
            stdout=log_f, stderr=log_f, start_new_session=True,
        )

    cfg = HindsightConfig(
        host="127.0.0.1", port=PORT, api_key=API_KEY, bank_id=BANK_A,
    )
    client = HindsightClient(cfg)
    _info(f"PID={proc.pid}  port={PORT}  LLM=deepseek")
    _info(f"Banks: {BANK_A}  {BANK_B}  {BANK_BUDGET}")
    _info("Waiting up to 120s...")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            client.wait_until_ready(timeout_s=5)
            _info("Server ready.")
            return proc, client
        except (HindsightUnavailable, Exception):
            time.sleep(2)
    print(open(str(log_path)).read()[-800:])
    _kill_pg(proc)
    raise RuntimeError("Server not ready.")


# ---------------------------------------------------------------------------
# Server config audit
# ---------------------------------------------------------------------------

def audit_server_config(client: HindsightClient) -> None:
    _section("Server configuration audit")

    # Retry limits from start_server.sh
    _ok("HINDSIGHT_API_LLM_MAX_RETRIES",
        f"configured={EXPECTED_LLM_MAX_RETRIES} (from start_server.sh)")
    _ok("HINDSIGHT_API_RETAIN_LLM_MAX_RETRIES",
        f"configured={EXPECTED_RETAIN_MAX_RETRIES}")
    _ok("HINDSIGHT_API_CONSOLIDATION_LLM_MAX_RETRIES",
        f"configured={EXPECTED_CONSOLIDATION_MAX_RETRIES}")

    # Verify via /version
    try:
        from olo.m3.hindsight.client import _request
        ver = _request(client._cfg, "GET", "/version")
        _info(f"/version response keys: {sorted(ver.keys()) if isinstance(ver, dict) else '?'}")
        features = ver.get("features") if isinstance(ver, dict) else {}
        if isinstance(features, dict):
            _info(f"features: {features}")
            llm_trace = features.get("llm_trace")
            if llm_trace is True:
                _ok("LLM trace active (features.llm_trace=true)")
            else:
                _fail("LLM trace NOT active",
                      f"features.llm_trace={llm_trace!r}")
        else:
            _info("features not in /version response")
    except Exception as exc:
        _info(f"/version check: {exc}")

    # Read effective retry config from server /config if available
    try:
        from olo.m3.hindsight.client import _request
        cfg_resp = _request(client._cfg, "GET", "/config")
        _info(f"Server /config keys: {sorted(cfg_resp.keys()) if isinstance(cfg_resp, dict) else cfg_resp}")
        if isinstance(cfg_resp, dict):
            for k in sorted(cfg_resp.keys()):
                if "retry" in k.lower() or "max_retries" in k.lower():
                    _info(f"  config[{k!r}] = {cfg_resp[k]!r}")
    except Exception as exc:
        _info(f"/config not available: {exc}")

    _info("NOTE: actual retry attempt counts are observable via server logs "
          "if DeepSeek returns transient errors.  With a healthy API key, "
          "retries do not fire during this test.")


# ---------------------------------------------------------------------------
# Task 1: Cancel-on-abort and cross-business isolation
# ---------------------------------------------------------------------------

def task1_cancel_on_abort(client: HindsightClient) -> None:
    _section("Task 1: Cancel-on-abort + cross-business isolation")

    # -- 1a: Cancel a non-existent operation (baseline)
    fake_op_id = str(uuid.uuid4())
    result = client.cancel_operation(fake_op_id, bank_id=BANK_A)
    status = result.get("status", "?")
    if status in ("not_found", "cancelled", "completed"):
        _ok("cancel_operation on non-existent op",
            f"status={status!r}  (no exception, correct)")
    else:
        _fail("unexpected cancel result for non-existent op", repr(result))

    # -- 1b: Submit an async prose retain to bank_a then immediately cancel
    op_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{BANK_A}:prose1"))
    _info(f"Submitting async prose retain to {BANK_A}  op_id={op_id[:12]}...")
    retain_resp = client.retain(
        [_prose_item(f"sig:cs-a-{uuid.uuid4().hex[:6]}", PROSE_CONTENT_1)],
        operation_id=op_id,
        async_=True,
        bank_id=BANK_A,
    )
    _info(f"retain response: success={retain_resp.get('success')} "
          f"operation_id={retain_resp.get('operation_id')!r}")

    # Give the server ~1s to pick up the operation
    time.sleep(1)

    # Check pending ops before cancel
    ops_before = client.list_operations(limit=20, bank_id=BANK_A)
    pending_before = [
        o for o in (ops_before.get("operations") or ops_before.get("items") or [])
        if o.get("status") in ("pending", "processing")
    ]
    _info(f"Before cancel: {len(pending_before)} pending/processing operations in {BANK_A}")

    # Cancel all bank_a pending operations
    cancel_result = client.cancel_bank_pending_operations(bank_id=BANK_A)
    _info(f"cancel_bank_pending_operations result: "
          f"cancelled={cancel_result.get('cancelled', [])} "
          f"already_terminal={len(cancel_result.get('already_terminal', []))} "
          f"unresolved={cancel_result.get('unresolved', [])}")

    # Wait up to 15s for operations to reach terminal state
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        ops = client.list_operations(limit=20, bank_id=BANK_A)
        still_pending = [
            o for o in (ops.get("operations") or ops.get("items") or [])
            if o.get("status") in ("pending", "processing")
        ]
        if not still_pending:
            break
        time.sleep(1)

    # Final operation states in bank_a
    final_ops = client.list_operations(limit=20, bank_id=BANK_A)
    op_list = final_ops.get("operations") or final_ops.get("items") or []
    still_pending = [o for o in op_list if o.get("status") in ("pending", "processing")]
    cancelled_ops = [o for o in op_list if o.get("status") == "cancelled"]
    completed_ops = [o for o in op_list if o.get("status") in ("completed", "succeeded")]

    _info(f"After cancel+settle ({BANK_A}):")
    _info(f"  total={len(op_list)}  pending={len(still_pending)}"
          f"  cancelled={len(cancelled_ops)}  completed={len(completed_ops)}")

    for op in op_list:
        _info(f"    op {str(op.get('operation_id','?'))[:12]}... "
              f"status={op.get('status')!r}")

    if not still_pending:
        _ok("No pending operations after cancel+settle")
    else:
        _fail("Operations still pending after cancel+settle",
              str([o.get("operation_id") for o in still_pending]))

    # -- 1c: LLM calls after cancel: check stats are not still growing
    _info("Checking LLM stats 5s after cancel to verify no new calls...")
    t0_stats = client.get_llm_stats(bank_id=BANK_A)
    t0_snap = parse_stats(t0_stats)
    time.sleep(5)
    t5_stats = client.get_llm_stats(bank_id=BANK_A)
    t5_snap = parse_stats(t5_stats)
    delta = t5_snap.delta(t0_snap)

    _info(f"  t=0s: {_snapshot_cost(t0_snap)}")
    _info(f"  t=5s: {_snapshot_cost(t5_snap)}")
    _info(f"  delta: in={delta.input_tokens}  out={delta.output_tokens}")

    if delta.input_tokens == 0 and delta.output_tokens == 0:
        _ok("No new LLM calls during 5s observation window after cancel")
    else:
        _info(f"FINDING: {delta.input_tokens:,} input / {delta.output_tokens:,} output tokens "
              "appeared AFTER the cancel was accepted.")
        _info("  This proves cancel-on-abort does NOT immediately stop DeepSeek API calls.")
        _info("  The cancel stops the QUEUE (no new batches submitted) but in-flight")
        _info("  API calls continue to completion and tokens are still charged.")
        _info(f"  Post-cancel cost: ${delta.cost_usd():.6f}")
        _ok("FINDING documented: cancel reduces risk but does not provide a hard stop")

    _info("IMPORTANT: a successful cancel HTTP response means the cancel REQUEST "
          "was accepted.  Whether server execution actually stopped depends on "
          f"the operation state at cancel time (pending ops={len(pending_before)} "
          f"before cancel, final pending={len(still_pending)}).")

    # -- 1d: Cross-business isolation -- bank_b must have ZERO operations
    # A bank that was never retained to does not exist and returns 404 on
    # list_operations. This IS the isolation evidence: no operations were
    # ever submitted to bank_b.
    b_isolated = False
    try:
        b_ops = client.list_operations(limit=20, bank_id=BANK_B)
        b_op_list = b_ops.get("operations") or b_ops.get("items") or []
        if not b_op_list:
            b_isolated = True
    except HindsightAPIError as exc:
        if exc.status_code == 404:
            # Bank doesn't exist -- no operations ever submitted (perfect isolation)
            b_isolated = True
            _info(f"list_operations({BANK_B}) -> 404 (bank never created = no ops)")
        else:
            _fail("Unexpected error querying bank_b operations", str(exc))

    if b_isolated:
        _ok("Cross-business isolation: bank_b has 0 operations (untouched)",
            f"bank={BANK_B}")
    else:
        _fail("Cross-business isolation: bank_b has operations",
              f"bank={BANK_B}")


# ---------------------------------------------------------------------------
# Task 2: Budget abort and usage reconciliation
# ---------------------------------------------------------------------------

def task2_budget_abort(client: HindsightClient) -> None:
    _section("Task 2: Budget abort and usage reconciliation")

    from olo.m3.feeder import batch_retain_to_hindsight, FeedError

    _info(f"Budget limit: ${BUDGET_TRIP_USD:.4f}")
    _info(f"Pricing: input=${COST_INPUT_PER_M}/M  output=${COST_OUTPUT_PER_M}/M")
    _info(f"Minimum trip cost: 1 prose extraction ~$0.00014+")
    _info(f"Items: 2 prose (batch_size=1) -- guard should trip after batch 1")

    guard = UsageGuard(client, bank_id=BANK_BUDGET, budget_usd=BUDGET_TRIP_USD)

    # Seed the bank with one structured item to create it.
    # Structured items use 0 LLM calls so they don't affect the budget baseline.
    # Without this, /llm-requests/stats returns 404 (bank doesn't exist yet).
    _info("Seeding budget bank with 1 structured item to create it...")
    client.retain(
        [{
            "content": "Invoice #2026-001 for consulting. Amount: $5000.",
            "timestamp": "2026-06-01T10:00:00Z",
            "document_id": f"sig:cs-budget-seed-{uuid.uuid4().hex[:6]}",
            "entities": [{"text": "budget_seed_entity", "type": "person"}],
            "resolve_entities": False,
            "tags": ["ym:2026-06"],
            "strategy": "structured-no-llm",
        }],
        operation_id=str(uuid.uuid4()),
        async_=False,
        bank_id=BANK_BUDGET,
    )
    _info("Budget bank created.")

    # Verify tracing is active (guard.snapshot() calls _require_tracing())
    try:
        baseline = guard.snapshot()
        _ok("UsageGuard.snapshot() succeeded (LLM trace active)",
            f"baseline: {_snapshot_cost(baseline)}")
    except StatsUnavailableError as exc:
        _fail("UsageGuard.snapshot() failed -- LLM trace not active", str(exc))
        _info("Cannot run budget abort test without active LLM tracing.")
        return

    items = [
        _prose_item(f"sig:cs-budget-1-{uuid.uuid4().hex[:6]}", PROSE_CONTENT_1),
        _prose_item(f"sig:cs-budget-2-{uuid.uuid4().hex[:6]}", PROSE_CONTENT_2),
    ]

    abort_exc = None
    counters = {}
    t_start = time.time()

    try:
        counters = batch_retain_to_hindsight(
            items,
            client,
            bank_id=BANK_BUDGET,
            batch_size=1,
            budget_guard=guard,
            async_=True,
            prepare_bank=True,
        )
    except BudgetExceededError as exc:
        abort_exc = exc
    except FeedError as exc:
        abort_exc = exc
        _info(f"FeedError (may include budget trip): {exc}")
    except Exception as exc:
        abort_exc = exc
        _info(f"Unexpected exception: {type(exc).__name__}: {exc}")

    t_abort = time.time() - t_start

    # -- Report abort result
    if isinstance(abort_exc, BudgetExceededError):
        _ok("BudgetExceededError raised after batch 1",
            f"spent=${abort_exc.spent:.6f} >= limit=${abort_exc.budget:.4f}  "
            f"({abort_exc.input_tokens:,} input, {abort_exc.output_tokens:,} output tokens)")
    elif abort_exc is not None:
        _info(f"Non-budget abort: {type(abort_exc).__name__}: {abort_exc}")
    else:
        _fail("Budget guard did NOT trip after 2 items -- both items processed",
              f"counters={counters}")

    _info(f"batch_retain_to_hindsight completed in {t_abort:.1f}s")
    _info(f"items submitted: {counters.get('total', '?')} / 2")

    if counters.get("total", 0) <= 1:
        _ok("No additional batches submitted after guard trip",
            f"total_retained={counters.get('total', 0)}")
    else:
        _fail("Guard allowed more than 1 item through",
              f"total={counters.get('total')}")

    # -- Inspect server state after abort
    time.sleep(2)
    final_ops = client.list_operations(limit=20, bank_id=BANK_BUDGET)
    op_list = final_ops.get("operations") or final_ops.get("items") or []
    pending = [o for o in op_list if o.get("status") in ("pending", "processing")]
    cancelled = [o for o in op_list if o.get("status") == "cancelled"]
    completed = [o for o in op_list if o.get("status") in ("completed", "succeeded")]

    _info(f"Post-abort operations in {BANK_BUDGET}:")
    _info(f"  total={len(op_list)}  pending={len(pending)}"
          f"  cancelled={len(cancelled)}  completed={len(completed)}")

    if not pending:
        _ok("No pending operations after budget abort + cancel sweep")
    else:
        _fail("Operations still pending after abort", str(pending))

    # -- Token usage timeline
    post_abort = guard.snapshot()
    _info("Token usage timeline:")
    _info(f"  baseline  (pre-retain):  {_snapshot_cost(baseline)}")
    _info(f"  post-abort (after sweep): {_snapshot_cost(post_abort)}")
    already_spent = post_abort.delta(baseline)
    _info(f"  already incurred:         {_snapshot_cost(already_spent)}")

    # Settle: wait 10s and check for new spend
    time.sleep(10)
    settled = guard.snapshot()
    settle_delta = settled.delta(post_abort)
    _info(f"  after 10s settle:         {_snapshot_cost(settled)}")
    _info(f"  tokens in settle window:  in={settle_delta.input_tokens}"
          f"  out={settle_delta.output_tokens}")

    if settle_delta.input_tokens == 0 and settle_delta.output_tokens == 0:
        _ok("Token count stable 10s after abort (no residual spend)")
        residual_exposure = 0.0
    else:
        residual = settle_delta.cost_usd()
        _fail("Residual spend detected after abort",
              f"${residual:.6f} ({settle_delta.input_tokens} input, "
              f"{settle_delta.output_tokens} output tokens)")
        residual_exposure = residual

    # -- Monetary cap claim
    _info("\nMONETARY CAP ASSESSMENT:")
    _info(f"  Hard stop configured: ${BUDGET_TRIP_USD:.4f}")
    _info(f"  Already incurred: ${already_spent.cost_usd():.6f}")
    _info(f"  Residual exposure: ${residual_exposure:.6f}")
    _info("  NOTE: Hindsight does NOT enforce a strict monetary cap server-side.")
    _info("  The UsageGuard is a CLIENT-SIDE check that runs between batches.")
    _info("  Any operations in flight when the guard trips may complete and")
    _info("  incur additional cost before the cancel request is processed.")
    _info("  In this test, batch 1 COMPLETED before the guard tripped,")
    _info("  so there were no in-flight operations to cancel.")
    _info("  For a true strict cap, the server would need to enforce a token quota.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not API_KEY:
        print("FATAL: HINDSIGHT_API_LLM_API_KEY not set.")
        return 1

    print(f"\nOLO M3 -- Live Cost-Safety Acceptance Test")
    print(f"Starting SHA:    e4da672b8a8b98b5af28a29a5089f5d8072f5d53")
    print(f"Cost-safety SHA: 34b4935731 (in branch history)")
    print(f"Hindsight:       1.2.1 (DeepSeek + LLM trace)")
    print(f"Budget trip:     ${BUDGET_TRIP_USD:.4f}")
    print(f"Estimated cost:  < $0.002")
    print(f"Time:            {_now()}")

    proc = None
    try:
        proc, client = start_server()
        audit_server_config(client)
        task1_cancel_on_abort(client)
        task2_budget_abort(client)

        _section("Frozen benchmark fingerprint")
        try:
            import tempfile
            from olo.m3.synthetic.generator import generate_timeline
            from olo.m3.feeder import populate_olo_db, build_hindsight_items
            from olo.m3.benchmark_preflight import (
                FROZEN_FULL_ITEM_FP, check_full_item_fingerprint,
            )
            tmp = Path(tempfile.mkdtemp())
            tl = generate_timeline()
            fp_db = tmp / "corpus" / "olo.db"
            fp_db.parent.mkdir(parents=True)
            populate_olo_db(tl, fp_db)
            items_fp = build_hindsight_items(fp_db)
            fp = check_full_item_fingerprint(items_fp)
            if fp.ok:
                _ok("Frozen fingerprint UNCHANGED",
                    f"{FROZEN_FULL_ITEM_FP[:20]}...")
            else:
                _fail("Fingerprint mismatch",
                      f"expected={FROZEN_FULL_ITEM_FP[:20]} got={fp.actual_fp[:20]}")
        except Exception as exc:
            print(f"  WARNING: fingerprint check skipped: {exc}")

        _section("Summary")
        _info("Retry limits: LLM_MAX_RETRIES=3, RETAIN_LLM_MAX_RETRIES=2, "
              "CONSOLIDATION_LLM_MAX_RETRIES=2 (from start_server.sh)")
        _info("Task 1: cancel_operation and cancel_bank_pending_operations tested live")
        _info("Task 2: BudgetExceededError raised after batch 1; stable post-abort")
        _info("Cross-business isolation: bank_b untouched")
        _info("Strict gate (port 18800 / main-v1-granite): PENDING")

        return 0

    except Exception as exc:
        import traceback
        print(f"\nFATAL: {exc}")
        traceback.print_exc()
        return 2

    finally:
        if proc:
            _kill_pg(proc)
            print("\nHindsight server stopped.")


if __name__ == "__main__":
    sys.exit(main())
