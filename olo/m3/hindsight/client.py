"""Olo M3 — Hindsight HTTP client.

Wraps the Hindsight v1.2.1 REST API for retain, recall, health and admin.
Configured per business environment (host, port, API key from Hermes secret scope).

All methods are synchronous wrappers around the HTTP API.
The Hindsight server runs in its own venv; this client lives in the Olo venv.

Opus architecture: §B.2 (API surface used).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class HindsightConfig:
    """Per-business Hindsight connection config."""
    host: str = "127.0.0.1"
    port: int = 8765
    api_key: str = ""   # loaded from Hermes secret scope; never hardcoded
    timeout_s: float = 30.0
    bank_id: str = "main-v1"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls, prefix: str = "") -> "HindsightConfig":
        """Load config from environment variables (Hermes secret scope pattern)."""
        p = prefix.upper() + "_" if prefix else ""
        return cls(
            host=os.environ.get(f"{p}HINDSIGHT_HOST", "127.0.0.1"),
            port=int(os.environ.get(f"{p}HINDSIGHT_PORT", "8765")),
            api_key=os.environ.get(f"{p}HINDSIGHT_API_KEY", ""),
            timeout_s=float(os.environ.get(f"{p}HINDSIGHT_TIMEOUT", "30")),
            bank_id=os.environ.get(f"{p}HINDSIGHT_BANK_ID", "main-v1"),
        )


# ── Exceptions ────────────────────────────────────────────────────────────────

class HindsightError(Exception):
    """Base error for Hindsight client calls."""
    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_body: Optional[bytes] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class HindsightUnavailable(HindsightError):
    """Server is not reachable."""


class HindsightAPIError(HindsightError):
    """Server returned a 4xx/5xx error."""


# ── Low-level HTTP ────────────────────────────────────────────────────────────

def _request(
    cfg: HindsightConfig,
    method: str,
    path: str,
    body: Optional[dict] = None,
    params: Optional[dict] = None,
) -> dict:
    """Make one HTTP request to Hindsight and return the parsed JSON response.

    Raises HindsightUnavailable on network errors,
           HindsightAPIError on 4xx/5xx responses.
    """
    url = cfg.base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            pass
        raise HindsightAPIError(
            f"Hindsight {method} {path} → HTTP {exc.code}",
            status_code=exc.code,
            response_body=raw,
        ) from exc
    except (urllib.error.URLError, ConnectionRefusedError, OSError) as exc:
        raise HindsightUnavailable(
            f"Hindsight not reachable at {cfg.base_url}: {exc}"
        ) from exc


# ── Public API ────────────────────────────────────────────────────────────────

class HindsightClient:
    """Synchronous client for the Hindsight v1.2.1 API.

    Usage::

        cfg = HindsightConfig.from_env("ACME")
        client = HindsightClient(cfg)
        client.health_check()   # raises HindsightUnavailable if down
        client.retain([{...}], operation_id="olo-idx:...", async_=False)
        results = client.recall("delivery reschedule", tags=["ent:ent_01ACME..."])
    """

    def __init__(self, cfg: HindsightConfig) -> None:
        self._cfg = cfg

    # ── Health ────────────────────────────────────────────────────────────────

    def health_check(self) -> dict:
        """Return health info or raise HindsightUnavailable."""
        return _request(self._cfg, "GET", "/health")

    def wait_until_ready(self, timeout_s: float = 30.0, poll_interval: float = 0.5) -> None:
        """Block until the server is healthy or timeout expires."""
        deadline = time.monotonic() + timeout_s
        last_exc: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                self.health_check()
                return
            except HindsightUnavailable as e:
                last_exc = e
                time.sleep(poll_interval)
        raise HindsightUnavailable(
            f"Hindsight not ready after {timeout_s}s"
        ) from last_exc

    # ── Retain ────────────────────────────────────────────────────────────────

    def retain(
        self,
        items: list[dict],
        *,
        operation_id: str,
        async_: bool = True,
        bank_id: Optional[str] = None,
    ) -> dict:
        """Submit items for retention.

        Args:
            items: list of MemoryItem dicts (content, timestamp, document_id, …)
            operation_id: idempotency key; re-submitting same ID returns original
            async_: True (default) = fire-and-forget into worker queue;
                    False = synchronous (blocks until processed)
            bank_id: override the default bank for this batch
        """
        payload: dict[str, Any] = {
            "items": items,
            "async": async_,
            "operation_id": operation_id,
        }
        if bank_id or self._cfg.bank_id:
            payload["bank_id"] = bank_id or self._cfg.bank_id
        return _request(self._cfg, "POST", "/memories/retain", payload)

    def get_operation_status(self, operation_id: str) -> dict:
        """Check the status of an async retain operation."""
        return _request(self._cfg, "GET", f"/operations/{urllib.parse.quote(operation_id)}")

    def wait_for_operation(
        self,
        operation_id: str,
        timeout_s: float = 60.0,
        poll_interval: float = 1.0,
    ) -> dict:
        """Poll until a retain operation reaches a terminal state."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.get_operation_status(operation_id)
            state = status.get("status", "")
            if state in ("completed", "failed", "partial"):
                return status
            time.sleep(poll_interval)
        raise TimeoutError(f"Operation {operation_id} not complete after {timeout_s}s")

    # ── Recall ────────────────────────────────────────────────────────────────

    def recall(
        self,
        query: str,
        *,
        max_tokens: int = 4000,
        trace: bool = True,
        tags: Optional[list[str]] = None,
        tags_match: str = "any",
        temporal_window: Optional[dict] = None,
        bank_id: Optional[str] = None,
        types: Optional[list[str]] = None,
    ) -> dict:
        """Run a recall query and return results with optional trace.

        Args:
            query:          Natural language query
            max_tokens:     Token budget for results
            trace:          Include ranking trace (required for V8 provenance)
            tags:           Filter by tags (e.g. ["ent:ent_01ACME..."])
            tags_match:     "any" (default) or "all"
            temporal_window: {"from": ISO, "to": ISO} date filter
            bank_id:        Override default bank
            types:          Subset of memory types to return (default: all)
        """
        payload: dict[str, Any] = {
            "query": query,
            "max_tokens": max_tokens,
            "trace": trace,
        }
        if tags:
            payload["tags"] = tags
            payload["tags_match"] = tags_match
        if temporal_window:
            payload["temporal_window"] = temporal_window
        if bank_id or self._cfg.bank_id:
            payload["bank_id"] = bank_id or self._cfg.bank_id
        if types:
            payload["types"] = types
        return _request(self._cfg, "POST", "/memories/recall", payload)

    # ── Documents ─────────────────────────────────────────────────────────────

    def get_document(self, document_id: str, bank_id: Optional[str] = None) -> dict:
        """Get a document by its document_id."""
        params: dict[str, str] = {}
        if bank_id or self._cfg.bank_id:
            params["bank_id"] = bank_id or self._cfg.bank_id
        return _request(
            self._cfg, "GET",
            f"/memories/documents/{urllib.parse.quote(document_id)}",
            params=params or None,
        )

    def list_documents(
        self,
        limit: int = 100,
        offset: int = 0,
        bank_id: Optional[str] = None,
    ) -> dict:
        """List document IDs in the bank."""
        params = {"limit": str(limit), "offset": str(offset)}
        if bank_id or self._cfg.bank_id:
            params["bank_id"] = bank_id or self._cfg.bank_id
        return _request(self._cfg, "GET", "/memories/documents", params=params)

    def delete_document(self, document_id: str, bank_id: Optional[str] = None) -> dict:
        """Delete a document and all its memory units."""
        params: dict[str, str] = {}
        if bank_id or self._cfg.bank_id:
            params["bank_id"] = bank_id or self._cfg.bank_id
        return _request(
            self._cfg, "DELETE",
            f"/memories/documents/{urllib.parse.quote(document_id)}",
            params=params or None,
        )

    # ── Banks ─────────────────────────────────────────────────────────────────

    def create_bank(self, bank_id: str, description: str = "") -> dict:
        """Create a new memory bank."""
        return _request(self._cfg, "POST", "/banks", {
            "bank_id": bank_id,
            "description": description,
        })

    def list_banks(self) -> dict:
        """List all banks in this environment."""
        return _request(self._cfg, "GET", "/banks")

    def delete_bank(self, bank_id: str) -> dict:
        """Delete a bank and all its data (irreversible)."""
        return _request(
            self._cfg, "DELETE", f"/banks/{urllib.parse.quote(bank_id)}"
        )

    # ── Admin ─────────────────────────────────────────────────────────────────

    def admin_health(self) -> dict:
        """Get detailed health including worker status."""
        return _request(self._cfg, "GET", "/admin/health")

    def retry_failed(self, bank_id: Optional[str] = None) -> dict:
        """Retry failed consolidation operations."""
        params: dict[str, str] = {}
        if bank_id or self._cfg.bank_id:
            params["bank_id"] = bank_id or self._cfg.bank_id
        return _request(self._cfg, "POST", "/admin/retry_failed", params=params or None)
