"""M3 integration test conftest.

Provides server stability between integration test classes to prevent
connection resets from one heavy test class cascading into the next.
"""
from __future__ import annotations

import os
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
        # Retry multiple times — connection resets after heavy consolidation
        # (e.g. P1's trigger_consolidation) need up to 90s to drain.
        stabilised = False
        for _attempt in range(18):   # 18 × 5 s = 90 s max wait
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
            # Server stabilised — brief settle before yielding.
            # 5 s gives consolidation triggered by the previous test class
            # (e.g. V9 prose detection, P1 scratch bank) time to drain from
            # the LLM concurrency pool before V3/V6 fire their hs_client
            # fixture's wait_until_ready.
            time.sleep(5)

    except Exception:
        pass  # structural tests don't need server

    yield

    # Post-test: no destructive cleanup here.
    # Each integration test that starts its own server processes must track
    # those processes and kill only them (using process groups) in its own
    # finally block.  Global pkill/-u/-f/-9 and /tmp socket deletion are
    # prohibited: they kill unrelated processes and servers.
