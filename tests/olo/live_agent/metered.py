"""A loopback meter between a Hermes agent and its model provider.

Every model request of the turn passes through ``MeteredUpstream``, whatever
Hermes does internally (main turn, tool-search bridge, auxiliary calls,
retries), so its ceilings are hard:

* **requests**: at most ``max_calls`` chat-completion requests are forwarded;
  the next is refused locally (HTTP 400, never reaching the provider);
* **spend**: before forwarding, the request's worst case (input estimated
  from the body at 3 bytes a token, plus the clamped output limit, at the
  provider's list prices) must fit in what is left of ``max_usd``; after it,
  the provider's reported usage is charged (the worst case when none is
  reported, or when the request failed);
* **output**: ``max_tokens`` is clamped to ``max_output_tokens``;
* **surface**: only ``POST …/chat/completions`` is forwarded; ``GET …/models``
  is answered locally; anything else is refused.

The key is added here (``Authorization: Bearer``) and never written to the
Hermes home, the evidence or a log; Hermes holds a placeholder.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

PLACEHOLDER_KEY = "olo-metered-upstream"
_BYTES_PER_TOKEN = 3


class MeteredUpstream:
    def __init__(self, upstream: str, *, api_key: str, model: str, max_calls: int, max_usd: float,
                 max_output_tokens: int, price_in_per_m: float, price_out_per_m: float,
                 timeout_s: float = 120.0) -> None:
        self.upstream = upstream.rstrip("/")
        self._key, self.model = api_key, model
        self.max_calls, self.max_usd, self.max_output_tokens = max_calls, max_usd, max_output_tokens
        self.price_in, self.price_out, self.timeout_s = price_in_per_m, price_out_per_m, timeout_s
        self.calls = self.refused = 0
        self.prompt_tokens = self.completion_tokens = 0
        self.cost_usd = self.reserved_usd = 0.0
        self.requests: list[dict[str, Any]] = []        # what each forwarded request carried
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def __enter__(self) -> MeteredUpstream:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def report(self) -> dict[str, Any]:
        return {"model": self.model, "forwarded_calls": self.calls, "refused_calls": self.refused,
                "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                "cost_usd": round(self.cost_usd, 6),
                "caps": {"calls": self.max_calls, "usd": self.max_usd,
                         "max_output_tokens": self.max_output_tokens},
                "prices_per_m": {"input": self.price_in, "output": self.price_out}}

    # ── metering ─────────────────────────────────────────────────────────────
    def _cost(self, prompt: int, completion: int) -> float:
        return prompt * self.price_in / 1e6 + completion * self.price_out / 1e6

    def admit(self, body: dict, raw_len: int) -> tuple[Optional[str], float]:
        """``(refusal, reserved worst case)``; clamps the body's output limit."""
        limit = min(int(body.get("max_tokens") or body.get("max_completion_tokens")
                        or self.max_output_tokens), self.max_output_tokens)
        body["max_tokens"] = limit
        body.pop("max_completion_tokens", None)
        worst = self._cost(raw_len // _BYTES_PER_TOKEN + 1, limit)
        with self._lock:
            if self.calls >= self.max_calls:
                self.refused += 1
                return f"model-call cap reached ({self.max_calls})", 0.0
            if self.cost_usd + self.reserved_usd + worst > self.max_usd:
                self.refused += 1
                return f"spend cap would be exceeded (${self.max_usd})", 0.0
            self.calls += 1
            self.reserved_usd += worst
        return None, worst

    def settle(self, worst: float, usage: Optional[dict]) -> None:
        with self._lock:
            self.reserved_usd -= worst
            if usage and "prompt_tokens" in usage:
                p, c = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
                self.prompt_tokens += p
                self.completion_tokens += c
                self.cost_usd += self._cost(p, c)
            else:
                self.cost_usd += worst               # unknown or failed: charged its worst case

    def note(self, body: dict) -> None:
        self.requests.append({
            "stream": bool(body.get("stream")), "max_tokens": body.get("max_tokens"),
            "tools": sorted(t.get("function", {}).get("name", "") for t in body.get("tools") or []),
            "messages": body.get("messages") or []})


def _usage_from_sse(chunk: bytes, found: dict, pending: bytearray) -> None:
    """Scan complete SSE lines for ``usage`` (a line may span chunks)."""
    pending.extend(chunk)
    *lines, rest = bytes(pending).split(b"\n")
    pending[:] = rest
    for line in lines:
        line = line.strip()
        if line.startswith(b"data:") and b'"usage"' in line:
            try:
                usage = json.loads(line[5:].strip()).get("usage")
            except ValueError:
                continue
            if usage:
                found.update(usage)


def _handler(meter: MeteredUpstream):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:        # never log request lines
            return

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _refuse(self, why: str) -> None:
            self._send(400, {"error": {"message": f"olo metered upstream: {why}",
                                       "type": "invalid_request_error", "code": "olo_meter"}})

        def do_GET(self) -> None:
            if self.path.rstrip("/").endswith("/models"):
                self._send(200, {"object": "list", "data": [{"id": meter.model, "object": "model"}]})
            else:
                self._refuse("only chat completions are forwarded")

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not self.path.rstrip("/").endswith("/chat/completions"):
                return self._refuse("only chat completions are forwarded")
            body = json.loads(raw or b"{}")
            refusal, worst = meter.admit(body, len(raw))
            if refusal:
                return self._refuse(refusal)
            meter.note(body)
            stream = bool(body.get("stream"))
            if stream:
                body.setdefault("stream_options", {})["include_usage"] = True
            req = urllib.request.Request(
                meter.upstream + self.path[self.path.index("/chat/completions"):],
                data=json.dumps(body).encode(), method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {meter._key}"})
            usage: dict = {}
            try:
                with urllib.request.urlopen(req, timeout=meter.timeout_s) as resp:
                    self.send_response(resp.status)
                    self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
                    self.end_headers()
                    if stream:
                        pending = bytearray()
                        while True:
                            chunk = resp.read1(65536)
                            if not chunk:
                                _usage_from_sse(b"\n", usage, pending)
                                break
                            _usage_from_sse(chunk, usage, pending)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        data = resp.read()
                        usage = (json.loads(data or b"{}").get("usage") or {})
                        self.wfile.write(data)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:2000]
                meter.settle(worst, None)
                self.send_response(exc.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(detail)
                return
            except OSError:
                meter.settle(worst, None)
                return self._send(502, {"error": {"message": "olo metered upstream: provider unreachable"}})
            meter.settle(worst, usage)

    return Handler
