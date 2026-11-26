"""Loopback stand-ins for the paid services the S5 stages call, so the
installed scheduler path can be tested end to end without network or cost.

* ``FakeDeepSeek`` — an OpenAI-compatible ``/chat/completions`` endpoint
  answering by prompt fragment, with usage, as DeepSeek does.
* ``FakeHindsight`` — the Hindsight 1.2.1 HTTP surface Olo uses (version,
  bank config, retain, operations, consolidation, memory counts, LLM-trace
  stats, documents), enforcing the tenant key like the tenant extension.

Both run on 127.0.0.1 on an ephemeral port and record every request.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


class _Server:
    def __init__(self, handler_cls) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._httpd.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._httpd.shutdown()
        self._httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def owner(self):
        return self.server.owner  # type: ignore[attr-defined]

    def _send(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}


# ── DeepSeek ──────────────────────────────────────────────────────────────────

NOTHING = {"event_type": None, "action": None, "business_key": None, "confirmed_date": None,
           "proposed_date": None, "prior_confirmed_date": None, "supporting_passage": None,
           "confidence": 0.0}


class _DeepSeekHandler(_Handler):
    def do_POST(self) -> None:
        fake: FakeDeepSeek = self.owner
        body = self._body()
        prompt = "\n".join(str(m.get("content")) for m in body.get("messages") or [])
        fake.requests.append({k: v for k, v in body.items() if k != "messages"})
        if fake.fail_status:
            self._send({"error": {"message": "unavailable"}}, fake.fail_status)
            return
        reply = next((r for frag, r in fake.routes.items() if frag in prompt), NOTHING)
        self._send({"id": "fake", "object": "chat.completion", "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": json.dumps(reply)}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


class FakeDeepSeek(_Server):
    def __init__(self, routes: dict[str, dict]) -> None:
        super().__init__(_DeepSeekHandler)
        self.routes = routes
        self.requests: list[dict] = []
        self.fail_status: Optional[int] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


# ── Hindsight ─────────────────────────────────────────────────────────────────

class _HindsightHandler(_Handler):
    def _route(self, method: str) -> None:
        fake: FakeHindsight = self.owner
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        fake.requests.append((method, path))
        if fake.down:
            self._send({"detail": "unavailable"}, 503)
            return
        if path == "/version":
            self._send({"api_version": "1.2.1", "features": {"llm_trace": fake.trace}})
            return
        if path == "/health":
            self._send({"status": "ok"})
            return
        if self.headers.get("Authorization") != f"Bearer {fake.api_key}":
            self._send({"detail": "unauthorized"}, 401)
            return
        parts = path.strip("/").split("/")          # v1 default banks <bank> ...
        if parts[:3] != ["v1", "default", "banks"] or len(parts) < 4:
            self._send({"detail": "not found"}, 404)
            return
        bank, rest = urllib.parse.unquote(parts[3]), parts[4:]
        if method == "PUT" and not rest:
            fake.banks.setdefault(bank, {})
            self._send({"bank_id": bank})
            return
        if bank not in fake.banks:
            self._send({"detail": "bank not found"}, 404)
            return
        handler = {("GET", "config"): fake.get_config, ("PATCH", "config"): fake.patch_config,
                   ("POST", "memories"): fake.retain, ("GET", "operations"): fake.operation,
                   ("DELETE", "operations"): fake.cancel, ("POST", "consolidate"): fake.consolidate,
                   ("GET", "memories"): fake.memories_list, ("GET", "llm-requests"): fake.stats,
                   ("GET", "documents"): fake.document}.get((method, rest[0] if rest else ""))
        if handler is None:
            self._send({"detail": f"unsupported {method} {path}"}, 404)
            return
        status, obj = handler(bank, rest[1:], query, self._body() if method in ("POST", "PATCH") else {})
        self._send(obj, status)

    def _safe(self, method: str) -> None:
        try:
            self._route(method)
        except Exception:                      # a fake bug must surface, not drop the socket
            import traceback
            self.owner.errors.append(traceback.format_exc())
            import sys
            sys.stderr.write('FAKE-HINDSIGHT-ERROR ' + self.owner.errors[-1])
            self._send({"detail": "fake hindsight error"}, 500)

    def do_GET(self) -> None:
        self._safe("GET")

    def do_POST(self) -> None:
        self._safe("POST")

    def do_PUT(self) -> None:
        self._safe("PUT")

    def do_PATCH(self) -> None:
        self._safe("PATCH")

    def do_DELETE(self) -> None:
        self._safe("DELETE")


class FakeHindsight(_Server):
    """In-memory Hindsight: retains complete at once (``next_status``), prose
    memories stay pending until a consolidation runs, usage is reported per
    hour as ``hours`` holds it."""

    def __init__(self, api_key: str) -> None:
        super().__init__(_HindsightHandler)
        self.api_key = api_key
        self.banks: dict[str, dict] = {}
        self.docs: dict[str, dict] = {}
        self.ops: dict[str, str] = {}
        self.pending = 0
        self.next_status = "completed"
        self.down = False
        self.trace = True
        self.hours: dict[str, tuple[int, int]] = {}
        self.requests: list[tuple[str, str]] = []
        self.unknown_strategies: list[str] = []
        self.errors: list[str] = []

    def get_config(self, bank, rest, query, body):
        resolved = {"retain_extraction_mode": "concise", "enable_auto_consolidation": True,
                    "retain_strategies": {}, **self.banks[bank]}
        return 200, {"bank_id": bank, "config": resolved, "overrides": dict(self.banks[bank])}

    def patch_config(self, bank, rest, query, body):
        self.banks[bank].update(body.get("updates") or {})
        return self.get_config(bank, rest, query, body)

    def retain(self, bank, rest, query, body):
        strategies = (self.banks[bank].get("retain_strategies") or {})
        for item in body.get("items") or []:
            if item.get("strategy") and item["strategy"] not in strategies:
                self.unknown_strategies.append(item["strategy"])
            self.docs[item["document_id"]] = item
            if "olo:prose" in (item.get("tags") or []):
                self.pending += 1
        op = body.get("operation_id") or str(uuid.uuid4())
        self.ops[op] = self.next_status
        return 200, {"operation_id": op}

    def operation(self, bank, rest, query, body):
        if not rest:
            return 200, {"operations": []}
        op = urllib.parse.unquote(rest[0])
        return (200, {"status": self.ops[op]}) if op in self.ops else (404, {"detail": "not found"})

    def cancel(self, bank, rest, query, body):
        self.ops[urllib.parse.unquote(rest[0])] = "cancelled"
        return 200, {"success": True}

    def consolidate(self, bank, rest, query, body):
        op = str(uuid.uuid4())
        self.ops[op] = "completed"
        self.pending = 0
        return 200, {"operation_id": op}

    def memories_list(self, bank, rest, query, body):
        state = (query.get("consolidation_state") or [""])[0]
        return 200, {"total": self.pending if state == "pending" else 0, "items": []}

    def stats(self, bank, rest, query, body):
        if not self.trace:
            return 503, {"detail": "trace disabled"}
        buckets = [{"time": h, "statuses": {"success": 1},
                    "tokens": {"input": i, "output": o, "thoughts": 0}}
                   for h, (i, o) in self.hours.items()]
        return 200, {"bank_id": bank, "buckets": buckets}

    def document(self, bank, rest, query, body):
        doc = urllib.parse.unquote(rest[0]) if rest else ""
        if doc not in self.docs:
            return 404, {"detail": "not found"}
        return 200, {"id": doc, "memory_unit_count": 1}
