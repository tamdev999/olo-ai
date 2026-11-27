"""A request budget and a record of Google's response shapes.

``RecordingClient`` wraps Olo's GoogleClient (every data call goes through
``get_json``).  It refuses the call after ``max_requests`` and records, per
endpoint template, the *shape* of each response: keys, value types, list
lengths, and the values of a few non-personal enum fields (``status``,
``mimeType``, ``responseStatus`` …).  Ids in paths become ``{id}``; no
string value, address, subject or body is ever recorded, and the token never
passes through here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ENUM_KEYS = frozenset({"status", "kind", "mimeType", "responseStatus", "eventType",
                        "transparency", "visibility", "accessRole", "historyTypes"})
_ID_SEGMENT = re.compile(r"(/(?:messages|calendars|events|threads|drafts)/)[^/?]+")


class BudgetExceeded(RuntimeError):
    """The run reached its Google request budget."""


def template(path: str) -> str:
    return _ID_SEGMENT.sub(r"\1{id}", path)


def shape(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "..."
    if isinstance(value, dict):
        return {k: (f"enum:{v}" if k in _ENUM_KEYS and isinstance(v, str) else shape(v, depth + 1))
                for k, v in sorted(value.items())}
    if isinstance(value, list):
        return {"list": len(value), "item": shape(value[0], depth + 1) if value else None}
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    return "str"


class RecordingClient:
    def __init__(self, client: Any, *, max_requests: int) -> None:
        self._client = client
        self.max_requests = max_requests
        self.requests = 0
        self.calls: list[dict] = []
        self.shapes: dict[str, list] = {}

    def get_json(self, service: str, path: str, params):
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request budget of {self.max_requests} reached")
        self.requests += 1
        names = sorted({k for k, _ in params})
        self.calls.append({"service": service, "path": template(path), "params": names})
        result = self._client.get_json(service, path, params)
        key = f"{service} GET {template(path)}"
        seen = self.shapes.setdefault(key, [])
        s = shape(result)
        if s not in seen and len(seen) < 20:
            seen.append(s)
        return result

    # GoogleClient helpers that build on get_json keep working through it.
    def get_message_or_none(self, message_id, metadata_headers):
        from olo.google.http import GoogleClient
        return GoogleClient.get_message_or_none(self, message_id, metadata_headers)

    def get_message_full(self, message_id):
        from olo.google.http import GoogleClient
        return GoogleClient.get_message_full(self, message_id)

    def list_messages(self, params):
        from olo.google.http import GoogleClient
        return GoogleClient.list_messages(self, params)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def save(self, evidence: Path, run: str) -> None:
        evidence.mkdir(parents=True, exist_ok=True)
        shapes_file = evidence / "api_shapes.json"
        merged = json.loads(shapes_file.read_text(encoding="utf-8")) if shapes_file.exists() else {}
        for key, shapes in self.shapes.items():
            bucket = merged.setdefault(key, [])
            bucket.extend(s for s in shapes if s not in bucket)
        shapes_file.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
        with (evidence / "requests.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"run": run, "requests": self.requests,
                                 "budget": self.max_requests, "calls": self.calls}) + "\n")
