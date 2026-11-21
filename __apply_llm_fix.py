"""Apply the CLOSE_WAIT fix to the pinned Hindsight venv.

Modifies openai_compatible_llm.py to disable HTTP keepalive for DeepSeek
connections, preventing CLOSE_WAIT socket accumulation that caused server
stalls during gate tests.
"""
import re
import sys

TARGET = "/root/olo_m3_hindsight_venv/lib/python3.14/site-packages/hindsight_api/engine/providers/openai_compatible_llm.py"

with open(TARGET, "r", encoding="utf-8") as f:
    src = f.read()

# Verify the target line exists and hasn't been patched
if "max_keepalive_connections=0" in src:
    print("FIX ALREADY APPLIED — no change needed.")
    sys.exit(0)

OLD = """        self._client = AsyncOpenAI(**client_kwargs)"""

NEW = """        # Disable HTTP keep-alive for the underlying httpx2 client.
        # Persistent keepalive connections to DeepSeek enter CLOSE_WAIT after each
        # request (remote sends FIN; local pool holds the socket open for reuse).
        # When httpx2 tries to reuse a CLOSE_WAIT socket, the write succeeds but
        # the read returns EOF immediately.  With max_retries=0 this surfaced as a
        # hard APIConnectionError, triggering consolidation's adaptive-splitting
        # retry loop, holding the LLM concurrency semaphore, and causing perceived
        # server stalls under concurrent gate load (olo-ai M3.0 diagnosis, Nov 2026).
        # With max_keepalive_connections=0, every request gets a fresh TCP+TLS
        # connection and is closed on completion — no CLOSE_WAIT accumulation.
        # DeepSeek extraction calls run for 2-30 s, so the ~100 ms TLS overhead
        # is negligible.  max_connections=10 caps concurrent DeepSeek sockets to
        # a reasonable bound (CONSOLIDATION_LLM_MAX_CONCURRENT is typically 2).
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
            # httpx2 not available or its API changed — fall back to default pooling.
            pass
        self._client = AsyncOpenAI(**client_kwargs)"""

count = src.count(OLD)
if count == 0:
    print(f"ERROR: target string not found in {TARGET}")
    print("       The file may have changed. Manual inspection needed.")
    sys.exit(1)
if count > 1:
    print(f"ERROR: target string found {count} times — ambiguous replacement")
    sys.exit(1)

patched = src.replace(OLD, NEW, 1)

with open(TARGET, "w", encoding="utf-8") as f:
    f.write(patched)

print(f"FIX APPLIED to {TARGET}")
print(f"  Replaced 1 occurrence of target string.")
# Quick sanity: the patched file should import and compile
import py_compile, tempfile, os
tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False)
tmp.write(patched.encode("utf-8"))
tmp.close()
try:
    py_compile.compile(tmp.name, doraise=True)
    print("  Syntax check: PASS")
except py_compile.PyCompileError as e:
    print(f"  Syntax check: FAIL — {e}")
    sys.exit(1)
finally:
    os.unlink(tmp.name)
