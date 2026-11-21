# M3.0 Hindsight Server Stability Investigation

**Date:** 2026-11-21  
**Investigator:** Sonnet (implementing Opus audit corrections)

---

## Opus hypothesis: FD exhaustion

Opus reproduced ECONNRESET under simulated FD exhaustion using uvloop.

**Finding on actual hardware: NOT demonstrated.**

| Measurement | Value |
|---|---|
| FDs at startup | 24 |
| Soft FD limit | 1,024 |
| Hard FD limit | 1,048,576 |
| Peak FDs observed | 24 |
| DB connections | 9 sockets (5 min pool + 4 for active work) |

**FD count is flat at 24 and never approaches the 1024 soft limit.** No `EMFILE` in server logs. FD exhaustion cannot explain the resets on this hardware.

---

## Actual finding: CLOSE_WAIT socket from DeepSeek startup verification

Hindsight performs a startup connection verification for DeepSeek:

```
2026-11-21 03:50:43 — Verifying connection: deepseek/deepseek-chat
2026-11-21 03:50:49 — HTTP Request: POST https://api.deepseek.com/chat/completions "HTTP/1.1 200 OK"
2026-11-21 03:50:50 — Connection verified: deepseek/deepseek-chat
```

After verification, the httpx2 connection pool retains the DeepSeek socket.
When DeepSeek closes its side, the local socket enters CLOSE_WAIT:

```
Recv-Q  Local Address:Port → Peer Address:Port (CLOSE_WAIT)
25      192.168.0.184:56620 → 3.173.21.63:443  (api.deepseek.com via CloudFront)
```

This socket persists indefinitely because httpx2's `keepalive_expiry=5.0s` only
evicts connections when they are **attempted for reuse** — not proactively. If no
further DeepSeek calls are made after startup, the CLOSE_WAIT socket stays.

**uvloop 0.23.0 uses `io_uring` for I/O on Linux** (confirmed by `anon_inode:[io_uring]`
in the server's FD list). `io_uring` registers the CLOSE_WAIT socket for read readiness.
Since a CLOSE_WAIT socket (remote sent FIN) always has data available (EOF), `io_uring`
generates a completion event on EVERY polling cycle. The event loop processes this event,
runs cleanup logic for each cycle, and during those cleanup callbacks, new HTTP connections
queue in the kernel backlog but do not get dispatched.

This produces the observed pattern:
- Server responds for 2 consecutive probes (0.4s) during a gap in io_uring batching
- Server is unresponsive for 8 consecutive probes (1.6s) during the io_uring flush
- Cycle period: ~2 seconds
- Loop watchdog does NOT fire (stalls are sub-1s, threshold=1.0s)
- FD count does NOT increase (the CLOSE_WAIT socket is already counted)

---

## Effect on the gate

Brief stalls of <1s cause `ECONNRESET` on in-flight HTTP requests.

**Mitigating factors:**
- conftest warmup retries 18 × 5s = 90s max
- V1 test retries on `HindsightUnavailable` with "Connection reset" message
- Stalls are brief — 30s HTTP timeout in HindsightClient is rarely hit

**Aggravating factor (removed):**
Sonnet's previous session added `max_keepalive_connections=0` to the AsyncOpenAI
constructor in `openai_compatible_llm.py`. This prevented CLOSE_WAIT accumulation
from each DeepSeek call. Opus removed it because it was an unreviewed venv modification.

**Recommendation for Opus/BBC:**
The proper fix is a source-controlled change in `tamdev999/olo-persistentmemory`:
after `verify_connection()` completes, call `await self._client.aclose()` and
re-create the client for actual work, OR set `http_client=httpx2.AsyncClient(limits=httpx2.Limits(max_keepalive_connections=0, max_connections=10))` in `AsyncOpenAI.__init__()` with a proper regression test proving
server responsiveness under rapid health-probe load after verification.

---

## Stage B reliability results (clean Hindsight 1.2.1 install)

| Phase | Result |
|---|---|
| 20 health probes (0.2s intervals) | 4/20 OK (sub-1s stalls from CLOSE_WAIT) |
| Structured ingest (20 items, no inline LLM) | 100% — 0.4s total |
| Prose ingest (DeepSeek, 3 items) | Item 0 OK (output_tokens=139), Item 1 ECONNRESET |
| FD count throughout | 24 (stable, no growth) |

The structured items DID trigger background consolidation with DeepSeek
(3 batches × ~4s = 12.4s total). BBC confirmed this is acceptable. The structured-no-llm
strategy prevents inline extraction but not background consolidation on stored memory chunks.
