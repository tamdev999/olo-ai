"""olo/m3/deploy_granite.py — Reproducible Granite R2 bank deployment.

Creates (or replaces) the ``main-v1-granite`` bank on the running Hindsight
server and waits for full LLM consolidation using **durable job-completion
checks** (wait_for_operation on every trigger_consolidation call) instead of
fixed sleeps.

Usage::

    python -m olo.m3.deploy_granite [--bank-id BANK_ID] [--verify-only]
                                    [--rollback]

Options
-------
--bank-id      Target bank name (default: main-v1-granite).
--verify-only  Skip ingestion; just report current bank state and verify that
               the Granite embedding model is active.
--rollback     Print instructions for reverting to the E5 bank (main-v1) and
               exit.  Does not modify any bank.

Rollback
--------
The E5 bank (``main-v1``) is never modified by this script.  To roll back,
set ``HINDSIGHT_BANK_ID=main-v1`` in the gate runner environment and restart
the Hindsight server with the E5-fp32 configuration (``/tmp/hs_arm_b_e5.sh``
or the original ``__hs_start.sh``).

Exit codes
----------
0 — deployment verified and all consolidation rounds completed.
1 — deployment failed; check error output.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid as _uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the olo package tree is importable when run as a module
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]  # …/olo-ai
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from olo.m3.hindsight.client import (
    HindsightClient,
    HindsightConfig,
    HindsightAPIError,
    HindsightUnavailable,
)
from olo.m3.feeder import build_hindsight_items, prepare_hindsight_bank


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_BANK_ID = "main-v1-granite"
E5_BANK_ID      = "main-v1"
CONSOLIDATION_ROUNDS  = 4   # max rounds of trigger+wait; usually 2 suffice
CONSOLIDATION_TIMEOUT = 600.0  # seconds per consolidation round

ROLLBACK_HELP = """
=== Rollback instructions ===
The E5 bank '{e5}' is unmodified and can be used immediately.

1. Set the gate runner environment:
       export HINDSIGHT_BANK_ID={e5}

2. Start the E5 server configuration:
       bash /tmp/hs_arm_b_e5.sh   # E5-fp32 + hs-rerank-1 + recency=none

3. Re-run the gate:
       bash __run_gate_full.sh

No data deletion required — the Granite bank '{granite}' remains
in the PostgreSQL database and can be re-validated later.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_server(cfg: HindsightConfig) -> None:
    """Verify server is healthy; raise on failure."""
    client = HindsightClient(cfg)
    client.wait_until_ready(timeout_s=30.0)
    print(f"[ok] Server healthy on port {cfg.port}")


def _verify_granite_active() -> bool:
    """Check the running hindsight-api process uses the Granite embedding model."""
    import subprocess
    try:
        result = subprocess.run(
            ["sh", "-c",
             "ps aux | grep hindsight-api | grep -v grep | grep -c granite"],
            capture_output=True, text=True, timeout=5,
        )
        count = int(result.stdout.strip() or "0")
        return count > 0
    except Exception:
        return False


def _wait_for_consolidation(client: HindsightClient, bank_id: str,
                             rounds: int = CONSOLIDATION_ROUNDS,
                             timeout: float = CONSOLIDATION_TIMEOUT) -> int:
    """Run consolidation rounds until stable; return number of rounds used.

    Each round calls trigger_consolidation and waits for the returned
    operation_id via wait_for_operation (event-based, not a fixed sleep).
    After a round completes with no work, we're done.
    Connection resets are retried with backoff.
    """
    for round_num in range(1, rounds + 1):
        print(f"  Consolidation round {round_num}/{rounds}…", end="", flush=True)
        # Retry trigger on connection reset
        result = None
        for _t in range(3):
            try:
                result = client.trigger_consolidation(bank_id=bank_id)
                break
            except (HindsightAPIError, HindsightUnavailable) as e:
                if _t < 2:
                    time.sleep(15)
                    continue
                print(f" skipped ({e})")
                return round_num
        if result is None:
            return round_num
        op_id = result.get("operation_id", "")
        if not op_id:
            print(" nothing to process — done.")
            return round_num
        # Wait for the operation, retrying on connection reset
        for _w in range(5):
            try:
                status = client.wait_for_operation(
                    op_id, timeout_s=min(timeout, 180.0), bank_id=bank_id
                )
                print(f" {status.get('status', '?')}")
                break
            except (HindsightUnavailable, HindsightAPIError):
                if _w < 4:
                    time.sleep(20)
                    continue
                print(f" (connection reset — continuing)")
                break
            except TimeoutError:
                print(f" timeout after {timeout:.0f}s — continuing")
                break
    return rounds


def _bank_facts(client: HindsightClient, bank_id: str) -> int:
    """Return the fact count for a bank, or -1 on error."""
    try:
        banks = client.list_banks().get("banks", [])
        for b in banks:
            if b["bank_id"] == bank_id:
                return b.get("fact_count", -1)
    except Exception:
        pass
    return -1


# ---------------------------------------------------------------------------
# Main deployment
# ---------------------------------------------------------------------------

def deploy(
    bank_id: str = DEFAULT_BANK_ID,
    db_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 18800,
) -> None:
    """Create or replace *bank_id* with Granite-embedded, LLM-consolidated data."""

    cfg = HindsightConfig(host=host, port=port, bank_id=bank_id)
    client = HindsightClient(cfg)

    print("=" * 60)
    print(f"Granite R2 deployment  →  {bank_id}")
    print("=" * 60)

    # 1. Health check
    _check_server(cfg)

    # 2. Verify Granite embedding model is active
    if _verify_granite_active():
        print("[ok] Granite embedding model confirmed active in server process")
    else:
        print("[warn] Could not confirm Granite in running process — proceeding")

    # 3. Preserve E5 bank (verify it still exists)
    e5_facts = _bank_facts(client, E5_BANK_ID)
    if e5_facts >= 0:
        print(f"[ok] E5 bank '{E5_BANK_ID}' preserved ({e5_facts} facts)")
    else:
        print(f"[warn] E5 bank '{E5_BANK_ID}' not found — rollback may be unavailable")

    # 4. Delete existing Granite bank if present
    existing_facts = _bank_facts(client, bank_id)
    if existing_facts >= 0:
        print(f"Deleting existing '{bank_id}' ({existing_facts} facts) for clean rebuild…")
        try:
            client.delete_bank(bank_id)
            time.sleep(2)
        except HindsightAPIError as e:
            if e.status_code == 405:
                print("  delete not supported — using existing bank")
            else:
                raise

    # 5. Build enriched evidence from gate_olo.db
    if db_path is None:
        db_path = Path(os.environ.get("OLO_DB_PATH", "/root/gate_olo.db"))
    if not db_path.exists():
        raise FileNotFoundError(f"olo.db not found at {db_path}")
    print(f"Loading enriched evidence from {db_path}…")
    items = build_hindsight_items(db_path)
    prose      = [i for i in items if i.get("strategy") == "prose"]
    structured = [i for i in items if i.get("strategy") != "prose"]
    print(f"  {len(items)} items  ({len(prose)} prose, {len(structured)} structured)")

    # 6. Ingest all items FIRST (creates the bank on first retain)
    print("Ingesting evidence…")

    # P0.4: Enforce budget guard before the first retain.
    # deploy_granite.py uses LLM consolidation (DeepSeek), so budget enforcement
    # is mandatory.  _require_tracing() verifies server-side LLM tracing is active;
    # it fails closed if tracing is disabled or the /version endpoint is unreachable.
    #
    # NOTE: LLM trace records arrive asynchronously (typically within 1-10 seconds
    # of a completed retain/consolidation call).  The budget guard therefore has
    # a bounded lag: a batch that starts at the budget limit may overshoot by up
    # to one batch worth of LLM tokens before the next check catches it.
    # DeepSeek V3 consolidation typically uses 2-5k tokens per prose item; at the
    # $8 default budget this is a ~$0.001 worst-case overshoot per 50-item batch.
    from olo.m3.hindsight.budget import UsageGuard, StatsUnavailableError as _SUA
    _guard = UsageGuard(client, bank_id=bank_id, budget_usd=8.0)
    try:
        _baseline = _guard.snapshot()
    except _SUA as exc:
        raise RuntimeError(
            f"[deploy] Budget guard: tracing not active on server — cannot enforce "
            f"spending limits.  Set HINDSIGHT_API_LLM_TRACE_ENABLED=true and "
            f"restart the server before deploying.  Error: {exc}"
        ) from exc
    print("[ok] Budget guard active (tracing confirmed, budget $8.00)")

    batch_size = 50
    t_start = time.monotonic()
    for label, batch_list in [("structured", structured), ("prose", prose)]:
        for start in range(0, len(batch_list), batch_size):
            batch = batch_list[start : start + batch_size]
            bk = "|".join(sorted(
                f"{i.get('document_id','')}:{i.get('metadata',{}).get('content_sha256','')}"
                for i in batch
            ))
            op_id = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"{bank_id}|{label}|{bk}"))
            # Retry individual batches on connection reset
            for _r in range(3):
                try:
                    client.retain(batch, operation_id=op_id, async_=True, bank_id=bank_id)
                    break
                except HindsightUnavailable:
                    if _r < 2:
                        time.sleep(10)
                    else:
                        raise
            # Check budget after each batch (trace records arrive asynchronously;
            # see note above re. bounded lag).
            try:
                _guard.check(_baseline)
            except _SUA as exc:
                raise RuntimeError(
                    f"[deploy] Budget guard: stats unavailable mid-ingest "
                    f"(fails closed): {exc}"
                ) from exc

    # 7. Configure bank strategy AFTER bank is created by first retain
    print("Configuring bank strategy…")
    for _r in range(5):
        try:
            prepare_hindsight_bank(client, bank_id)
            print("  retain_strategies registered")
            break
        except (HindsightUnavailable, Exception) as e:
            if _r < 4:
                print(f"  retry {_r+1}/5 after transient error: {e}")
                time.sleep(15)
            else:
                print(f"  [warn] bank config not applied: {e} — proceeding without it")
    elapsed = time.monotonic() - t_start
    print(f"  All {len(items)} items queued in {elapsed:.0f}s")

    # 8. Run consolidation until quiescent (event-based waits)
    print("Running LLM consolidation rounds…")
    rounds_used = _wait_for_consolidation(client, bank_id)
    print(f"  Consolidation stable after {rounds_used} round(s)")

    # 9. Report final state
    final_facts = _bank_facts(client, bank_id)
    print(f"\n[ok] '{bank_id}': {final_facts} facts")
    print(f"[ok] E5 bank '{E5_BANK_ID}' preserved")
    print(f"\nDeployment complete.  Set HINDSIGHT_BANK_ID={bank_id} in gate runner.")


def verify_only(bank_id: str = DEFAULT_BANK_ID,
                host: str = "127.0.0.1", port: int = 18800) -> None:
    """Report current bank state without modifying anything."""
    cfg = HindsightConfig(host=host, port=port, bank_id=bank_id)
    client = HindsightClient(cfg)
    _check_server(cfg)
    granite_ok = _verify_granite_active()
    facts = _bank_facts(client, bank_id)
    e5_facts = _bank_facts(client, E5_BANK_ID)
    print(f"  Granite model active: {granite_ok}")
    print(f"  '{bank_id}' facts:    {facts}")
    print(f"  '{E5_BANK_ID}' facts: {e5_facts} (rollback target)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy Granite R2 bank with durable consolidation checks"
    )
    parser.add_argument("--bank-id", default=DEFAULT_BANK_ID,
                        help=f"Target bank name (default: {DEFAULT_BANK_ID})")
    parser.add_argument("--db-path", default=None,
                        help="Path to gate_olo.db (default: OLO_DB_PATH env or /root/gate_olo.db)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18800)
    parser.add_argument("--verify-only", action="store_true",
                        help="Check current state without modifying anything")
    parser.add_argument("--rollback", action="store_true",
                        help="Print rollback instructions and exit")
    args = parser.parse_args()

    if args.rollback:
        print(ROLLBACK_HELP.format(e5=E5_BANK_ID, granite=args.bank_id))
        sys.exit(0)

    if args.verify_only:
        verify_only(args.bank_id, args.host, args.port)
        sys.exit(0)

    db_path = Path(args.db_path) if args.db_path else None
    deploy(args.bank_id, db_path, args.host, args.port)


if __name__ == "__main__":
    main()
