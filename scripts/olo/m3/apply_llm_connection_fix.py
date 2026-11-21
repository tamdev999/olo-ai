"""Apply the LLM HTTP client keepalive fix to the installed Hindsight venv.

ROOT CAUSE (confirmed 2026-11-21):
  Hindsight's AsyncOpenAI constructor uses httpx2 with default connection pooling
  (max_keepalive_connections=100). After each DeepSeek request completes, the
  connection is returned to the keepalive pool. When DeepSeek closes its side
  (sending FIN), the local socket enters CLOSE_WAIT. uvloop 0.23.0 uses io_uring
  for I/O; io_uring registers CLOSE_WAIT sockets as "always readable" and fires
  a completion event on every poll cycle. This produces sub-1s event loop stalls
  that cause ECONNRESET on concurrent HTTP requests and bypass the loop_watchdog
  (threshold=1.0s).

  Confirmed: FD count stays at 24 (soft limit=1024); FD exhaustion is NOT the cause.

FIX:
  Setting max_keepalive_connections=0 forces connection-per-request semantics —
  each DeepSeek call opens a fresh TLS connection and closes it on completion.
  No CLOSE_WAIT accumulation → no io_uring busy-loop → no event loop stalls.
  DeepSeek calls run for 1-30s each; the TLS overhead (~100ms) is negligible.

UPSTREAM FIX REQUIRED:
  This patch modifies the installed hindsight-api-slim package. The correct fix
  is to update openai_compatible_llm.py in tamdev999/olo-persistentmemory with:

    import httpx2 as _httpx2
    _limits = _httpx2.Limits(max_keepalive_connections=0, max_connections=10)
    _timeout_obj = build_sdk_timeout(self.timeout, timeout_cls=_httpx2.Timeout) if self.timeout else None
    _http_kw = {"limits": _limits, "follow_redirects": True}
    if _timeout_obj is not None:
        _http_kw["timeout"] = _timeout_obj
    client_kwargs["http_client"] = _httpx2.AsyncClient(**_http_kw)
    self._client = AsyncOpenAI(**client_kwargs)

  Include a regression test that:
    1. Starts a Hindsight server
    2. Issues 20 health probes at 200ms intervals after a DeepSeek verification call
    3. Asserts >= 18/20 probes succeed

USAGE: python3 scripts/olo/m3/apply_llm_connection_fix.py [--check | --revert]
"""
from __future__ import annotations

import sys
import py_compile
import tempfile
import os

TARGET = (
    "/root/olo_m3_hindsight_venv/lib/python3.14/site-packages/"
    "hindsight_api/engine/providers/openai_compatible_llm.py"
)

OLD = "        self._client = AsyncOpenAI(**client_kwargs)"

NEW = """\
        # Prevent CLOSE_WAIT accumulation in the httpx2 keepalive pool.
        # When DeepSeek closes its side of a TLS connection, the local socket
        # enters CLOSE_WAIT. uvloop 0.23.0 uses io_uring, which registers
        # CLOSE_WAIT sockets as always-readable; this fires a completion event
        # on every io_uring poll cycle, producing sub-1s event loop stalls that
        # cause ECONNRESET on concurrent requests and bypass loop_watchdog.
        # max_keepalive_connections=0 forces connection-per-request semantics —
        # no CLOSE_WAIT accumulation → no io_uring busy-loop → no stalls.
        # TLS overhead (~100ms) is negligible vs. DeepSeek calls (1-30s).
        # TODO: remove once upstream tamdev999/olo-persistentmemory has this fix.
        try:
            import httpx2 as _httpx2  # vendored by openai>=3
            _limits = _httpx2.Limits(max_keepalive_connections=0, max_connections=10)
            _timeout_obj = (
                build_sdk_timeout(self.timeout, timeout_cls=_httpx2.Timeout)
                if self.timeout is not None
                else None
            )
            _http_kw: dict = {"limits": _limits, "follow_redirects": True}
            if _timeout_obj is not None:
                _http_kw["timeout"] = _timeout_obj
            client_kwargs["http_client"] = _httpx2.AsyncClient(**_http_kw)
        except (ImportError, TypeError, AttributeError):
            pass  # fall back to default pooling if httpx2 API changes
        self._client = AsyncOpenAI(**client_kwargs)"""


def _read() -> str:
    with open(TARGET, encoding="utf-8") as f:
        return f.read()


def _write(src: str) -> None:
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(src)


def check() -> None:
    src = _read()
    if "max_keepalive_connections=0" in src:
        print("APPLIED: LLM keepalive fix is present.")
    else:
        print("NOT APPLIED: LLM keepalive fix is absent.")


def apply() -> None:
    src = _read()
    if "max_keepalive_connections=0" in src:
        print("Already applied — no change needed.")
        return
    count = src.count(OLD)
    if count == 0:
        sys.exit("ERROR: target string not found — Hindsight version may differ.")
    if count > 1:
        sys.exit(f"ERROR: found {count} occurrences — ambiguous replacement.")
    patched = src.replace(OLD, NEW, 1)
    # Syntax check before writing
    tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
    try:
        tmp.write(patched.encode("utf-8"))
        tmp.close()
        py_compile.compile(tmp.name, doraise=True)
    except py_compile.PyCompileError as e:
        sys.exit(f"Syntax check failed: {e}")
    finally:
        os.unlink(tmp.name)
    _write(patched)
    print(f"APPLIED: LLM keepalive fix written to {TARGET}")


def revert() -> None:
    src = _read()
    if "max_keepalive_connections=0" not in src:
        print("Not applied — nothing to revert.")
        return
    reverted = src.replace(NEW, OLD, 1)
    if "max_keepalive_connections=0" in reverted:
        sys.exit("ERROR: revert did not remove the patch — manual cleanup needed.")
    _write(reverted)
    print(f"REVERTED: LLM keepalive fix removed from {TARGET}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "apply"
    if action == "--check":
        check()
    elif action == "--revert":
        revert()
    else:
        apply()
