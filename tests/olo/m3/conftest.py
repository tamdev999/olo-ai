"""M3 integration test conftest.

Provides server stability between integration test classes to prevent
connection resets from one heavy test class cascading into the next.
"""
from __future__ import annotations

import os
import subprocess
import time

import pytest


@pytest.fixture(autouse=True, scope="class")
def _m3_server_stabilize(request):
    """Ensure Hindsight server is fully stable before each integration class.

    After V1's 48 rapid ONNX recalls the server needs ~15s to fully settle.
    This fixture fires before every integration test class with a warm-up
    recall to verify ONNX pool is fully ready.

    Fixes sequential test instability.
    """
    markers = {m.name for m in request.node.iter_markers()}
    if "integration" not in markers:
        yield
        return

    try:
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig, HindsightUnavailable

        cfg = HindsightConfig.from_env()
        client = HindsightClient(cfg)

        # Warm-up: attempt a lightweight recall to verify ONNX pool is ready.
        # Retry multiple times — connection resets after heavy loads need ~15s.
        stabilised = False
        for _attempt in range(8):
            try:
                client.recall("warmup", max_tokens=10, trace=False)
                stabilised = True
                break
            except HindsightUnavailable:
                time.sleep(5)

        if not stabilised:
            # Try health check at minimum
            try:
                client.wait_until_ready(timeout_s=30.0)
            except HindsightUnavailable:
                pass  # let the test fixture handle the error
        else:
            # Server stabilised — brief settle before yielding
            time.sleep(2)

    except Exception:
        pass  # structural tests don't need server

    yield

    # Post-test cleanup: kill any olo_hs processes spawned by V5/V7
    if os.environ.get("OLO_GATE_STRICT"):
        try:
            subprocess.run(
                ["pkill", "-u", "olo_hs", "-f", "hindsight-api"],
                check=False, capture_output=True,
            )
            # Also kill stale pg0 PostgreSQL subprocesses and clean lock files
            for pg0_name in ["olo-v5gate", "olo-v7-biz-aa", "olo-v7-biz-bb"]:
                subprocess.run(["pkill", "-f", pg0_name], check=False, capture_output=True)
            # Remove stale PGSQL lock files so next test can bind port 5433
            from pathlib import Path as _Path
            for lf in _Path("/tmp").glob(".s.PGSQL.*"):
                try:
                    lf.unlink()
                except Exception:
                    pass
            time.sleep(2)
        except Exception:
            pass
