"""Olo M3 — Hindsight HTTP client.

Wraps the Hindsight v1.2.1 REST API for retain, recall, health and admin.
Configured per business environment (host, port, API key from Hermes secret scope).

All methods are synchronous wrappers around the HTTP API.
The Hindsight server runs in its own venv; this client lives in the Olo venv.

Security: all HTTP goes through open_credentialed_url (C2 fix, §4.1).
HindsightConfig enforces that the host is 127.0.0.1 or localhost so that
the C2 redirect-stripping guarantee covers any accidental redirect.

Opus architecture: §B.2 (API surface used).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# ── Configuration ─────────────────────────────────────────────────────────────

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass
class HindsightConfig:
    """Per-business Hindsight connection config.

    host must be 127.0.0.1 or localhost — Hindsight is a loopback IPC service.
    Attempting to point it at an external host raises ValueError at construction
    so that credential leakage through open_credentialed_url's redirect-stripping
    path is impossible (C2, §4.1).
    """
    host: str = "127.0.0.1"
    port: int = 8765
    api_key: str = ""   # loaded from Hermes secret scope; never hardcoded
    timeout_s: float = 30.0
    bank_id: str = "main-v1"

    def __post_init__(self) -> None:
        if self.host not in _LOOPBACK_HOSTS:
            raise ValueError(
                f"HindsightConfig.host must be a loopback address "
                f"(127.0.0.1 / localhost), got {self.host!r}. "
                "Hindsight is a per-business loopback IPC service; "
                "pointing it at an external host is a misconfiguration."
            )

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

    @classmethod
    def for_business(cls, business_id: str, registry_path: Optional[str] = None) -> "HindsightConfig":
        """Load config for a specific business from the port registry.

        The registry is written by setup.sh at
        ``$HERMES_HOME/hindsight_port_registry.json`` and maps
        ``business_id -> {port, bank_id}``.

        Audit correction D9 (fix 7): unique port per business, registry routing.
        """
        import json as _json
        import pathlib as _pathlib

        # Determine registry path
        reg_path: _pathlib.Path
        if registry_path:
            reg_path = _pathlib.Path(registry_path)
        else:
            hermes_home = os.environ.get("HERMES_HOME", "")
            if hermes_home:
                reg_path = _pathlib.Path(hermes_home) / "hindsight_port_registry.json"
            else:
                # Fall back to per-business env convention
                prefix = business_id.upper().replace("-", "_")
                return cls.from_env(prefix=prefix)

        if not reg_path.exists():
            raise ValueError(
                f"Hindsight port registry not found at {reg_path}. "
                f"Run setup.sh for business {business_id!r} first."
            )

        registry = _json.loads(reg_path.read_text())
        entry = registry.get(business_id)
        if entry is None:
            raise ValueError(
                f"Business {business_id!r} not found in registry {reg_path}. "
                f"Known businesses: {list(registry.keys())}"
            )

        return cls(
            host="127.0.0.1",
            port=int(entry["port"]),
            # Use the bank_id from the registry (written by setup.sh as
            # "olo-{business_id}").  A mis-routed client targeting the wrong
            # bank gets 404, not another business's data (C4 fix).
            bank_id=entry.get("bank_id") or f"olo-{business_id}",
            # Load the per-business API key from the .env file for authenticated
            # requests.  The key is not in the registry (security); read it
            # from the per-business directory.
            api_key=cls._load_business_api_key(business_id, reg_path),
        )

    @classmethod
    def _load_business_api_key(
        cls,
        business_id: str,
        registry_path: "pathlib.Path",  # type: ignore[name-defined]
    ) -> str:
        """Load the per-business tenant API key from the business .env file."""
        import pathlib as _pathlib
        # The .env file lives next to the registry, in businesses/{id}/.env
        hermes_home = registry_path.parent
        env_file = hermes_home / "businesses" / business_id / ".env"
        if not env_file.exists():
            return ""
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("HINDSIGHT_API_TENANT_EXTENSION_API_KEY="):
                return line.split("=", 1)[1].strip()
        return ""


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

    Uses open_credentialed_url (C2 fix, §4.1) so Authorization headers are
    never forwarded on a cross-origin redirect.

    Connection resets on the first request after a cold ONNX start are
    retried once automatically (the ONNX embedder initialises asynchronously;
    the first request races with model loading and is occasionally dropped).

    Raises HindsightUnavailable on network errors,
           HindsightAPIError on 4xx/5xx responses.
    """
    from hermes_cli.urllib_security import open_credentialed_url

    url = cfg.base_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    data = json.dumps(body).encode() if body is not None else None

    last_exc: Optional[Exception] = None
    for _attempt in range(3):  # up to 2 retries on connection reset
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with open_credentialed_url(req, timeout=cfg.timeout_s) as resp:
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
            # Retry only on transient connection failures, and only for
            # idempotent calls: GET/HEAD requests, or POST requests that carry
            # an operation_id (the server will deduplicate on replay).
            # Retrying a POST without operation_id risks duplicate writes and
            # unexpected LLM costs.
            is_idempotent = method in ("GET", "HEAD") or (
                method == "POST"
                and body is not None
                and "operation_id" in body
            )
            # Match by errno to avoid false positives from substring scanning.
            _TRANSIENT_ERRNOS = frozenset({
                104,  # ECONNRESET
                111,  # ECONNREFUSED
            })
            inner = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            is_transient = isinstance(inner, OSError) and (
                getattr(inner, "errno", None) in _TRANSIENT_ERRNOS
            )
            if is_idempotent and is_transient and _attempt < 2:
                last_exc = exc
                time.sleep(8 + _attempt * 5)  # 8s, then 13s
                continue
            raise HindsightUnavailable(
                f"Hindsight not reachable at {cfg.base_url}: {exc}"
            ) from exc

    raise HindsightUnavailable(
        f"Hindsight not reachable at {cfg.base_url}: {last_exc}"
    ) from last_exc


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

    # API prefix — v1.2.1 uses versioned, tenant-scoped, bank-scoped paths
    _API_PREFIX = "/v1/default"

    def _bank_path(self, suffix: str, bank_id: Optional[str] = None) -> str:
        b = bank_id or self._cfg.bank_id
        return f"{self._API_PREFIX}/banks/{urllib.parse.quote(b, safe='')}{suffix}"

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
            except HindsightAPIError as e:
                # 503 = server starting up / temporarily unavailable — keep polling
                if e.status_code in (503, 502):
                    last_exc = HindsightUnavailable(
                        f"Hindsight not ready (HTTP {e.status_code})"
                    )
                    time.sleep(poll_interval)
                else:
                    raise
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

        POST /v1/default/banks/{bank_id}/memories
        operation_id must be a valid UUID (v1.2.1 requirement).
        """
        import uuid as _uuid
        # Derive a stable UUID from the caller's string operation_id
        # so idempotency is preserved while satisfying the UUID constraint.
        try:
            op_uuid = str(_uuid.UUID(operation_id))
        except ValueError:
            op_uuid = str(_uuid.uuid5(_uuid.NAMESPACE_OID, operation_id))
        payload: dict[str, Any] = {
            "items": items,
            "async": async_,
            "operation_id": op_uuid,
        }
        return _request(self._cfg, "POST", self._bank_path("/memories", bank_id), payload)

    def get_operation_status(self, operation_id: str, bank_id: Optional[str] = None) -> dict:
        """Check the status of an async retain operation.

        GET /v1/default/banks/{bank_id}/operations/{operation_id}
        """
        path = self._bank_path(
            f"/operations/{urllib.parse.quote(operation_id, safe='')}",
            bank_id,
        )
        return _request(self._cfg, "GET", path)

    def wait_for_operation(
        self,
        operation_id: str,
        timeout_s: float = 60.0,
        poll_interval: float = 1.0,
        bank_id: Optional[str] = None,
    ) -> dict:
        """Poll until a retain operation reaches a terminal state."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self.get_operation_status(operation_id, bank_id)
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
        tags_match: str = "all_strict",
        tag_groups: Optional[list[dict]] = None,
        temporal_window: Optional[dict] = None,
        bank_id: Optional[str] = None,
        types: Optional[list[str]] = None,
    ) -> dict:
        """Run a recall query.

        POST /v1/default/banks/{bank_id}/memories/recall

        ``tags_match`` defaults to ``"all_strict"`` so entity tags are never
        mixed with untagged memories (audit correction D10, fix 9).

        ``tag_groups`` enables AND-of-groups filtering, e.g.:
            tag_groups = [
                {"tags": ["ent:ent_01ACME000000000000000000"], "match": "all_strict"},
                {"tags": ["ym:2026-04", "ym:2026-05"], "match": "any_strict"},
            ]
        When ``tag_groups`` is supplied, ``tags`` and ``tags_match`` are ignored
        (the groups fully specify the filter).
        """
        payload: dict[str, Any] = {
            "query": query,
            "max_tokens": max_tokens,
            "trace": trace,
        }
        if tag_groups is not None:
            # Structured AND-of-groups filter — supersedes flat tags
            payload["tag_groups"] = tag_groups
        elif tags:
            payload["tags"] = tags
            payload["tags_match"] = tags_match
        if temporal_window:
            payload["temporal_window"] = temporal_window
        if types:
            payload["types"] = types
        return _request(self._cfg, "POST", self._bank_path("/memories/recall", bank_id), payload)

    # ── Documents ─────────────────────────────────────────────────────────────

    def get_document(self, document_id: str, bank_id: Optional[str] = None) -> dict:
        path = self._bank_path(
            f"/documents/{urllib.parse.quote(document_id, safe='')}",
            bank_id,
        )
        return _request(self._cfg, "GET", path)

    def get_document_history(
        self, document_id: str, bank_id: Optional[str] = None
    ) -> dict:
        """Return the full revision history for a document.

        GET /v1/default/banks/{bank_id}/documents/{document_id}/history

        Returns a list or dict of history entries.  Each entry contains the
        content and metadata at that revision.

        Audit V3 fix: use the real document-history endpoint instead of the
        observation-level ``/memories/{id}/history`` path.
        """
        path = self._bank_path(
            f"/documents/{urllib.parse.quote(document_id, safe='')}/history",
            bank_id,
        )
        return _request(self._cfg, "GET", path)

    def list_documents(
        self,
        limit: int = 100,
        offset: int = 0,
        bank_id: Optional[str] = None,
    ) -> dict:
        params = {"limit": str(limit), "offset": str(offset)}
        return _request(self._cfg, "GET", self._bank_path("/documents", bank_id), params=params)

    def list_all_documents_with_tags(
        self,
        tags: list[str],
        bank_id: Optional[str] = None,
        page_size: int = 200,
    ) -> list[dict]:
        """List ALL documents in the bank that have any of the given tags.

        Uses pagination to fetch all documents, then filters locally by tags.
        This avoids ranking bias from semantic recall.
        """
        all_docs: list[dict] = []
        offset = 0
        while True:
            try:
                resp = self.list_documents(limit=page_size, offset=offset, bank_id=bank_id)
            except HindsightAPIError as e:
                if e.status_code == 404:
                    break  # bank does not exist = 0 documents
                raise
            # Hindsight v1.2.1 returns {"items": [...], "total": N, ...}
            docs = resp.get("items", [])
            if not docs:
                break
            all_docs.extend(docs)
            if len(docs) < page_size:
                break
            offset += page_size

        # Filter locally by tags
        tag_set = set(tags)
        return [d for d in all_docs
                if tag_set & set(d.get("tags", []) or [])]

    def delete_document(self, document_id: str, bank_id: Optional[str] = None) -> dict:
        path = self._bank_path(
            f"/documents/{urllib.parse.quote(document_id, safe='')}",
            bank_id,
        )
        return _request(self._cfg, "DELETE", path)

    # ── Banks ─────────────────────────────────────────────────────────────────

    def create_bank(self, bank_id: str, description: str = "") -> dict:
        return _request(self._cfg, "POST", f"{self._API_PREFIX}/banks", {
            "bank_id": bank_id,
            "description": description,
        })

    def list_banks(self) -> dict:
        return _request(self._cfg, "GET", f"{self._API_PREFIX}/banks")

    def delete_bank(self, bank_id: str) -> dict:
        """Delete a bank and all its data (irreversible).

        Note: Hindsight v1.2.1 may return HTTP 405 if banks cannot be
        deleted this way — catch gracefully in callers.
        """
        return _request(
            self._cfg, "DELETE",
            f"{self._API_PREFIX}/banks/{urllib.parse.quote(bank_id, safe='')}",
        )

    def get_observation_history(
        self, memory_id: str, bank_id: Optional[str] = None
    ) -> dict:
        """Get the history of a single observation.

        GET /v1/default/banks/{bank_id}/memories/{memory_id}/history
        Returns the full history of changes with source facts.
        """
        path = self._bank_path(
            f"/memories/{urllib.parse.quote(memory_id, safe='')}/history",
            bank_id,
        )
        return _request(self._cfg, "GET", path)

    # ── Admin ─────────────────────────────────────────────────────────────────

    def admin_health(self) -> dict:
        return _request(self._cfg, "GET", "/health")

    def retry_failed(self, bank_id: Optional[str] = None) -> dict:
        return _request(
            self._cfg, "POST",
            self._bank_path("/consolidation/recover", bank_id),
        )

    def get_llm_stats(self, bank_id: Optional[str] = None) -> dict:
        """Return LLM usage stats for the bank.

        GET /v1/default/banks/{bank_id}/llm-requests/stats
        Response: {"bank_id": "...", "buckets": [{"total": N, "tokens": {"input": N, "output": N}}, ...]}
        Used by UsageGuard for the $8 budget hard stop (audit correction D1).
        """
        return _request(self._cfg, "GET", self._bank_path("/llm-requests/stats", bank_id))

    def get_bank_stats(self, bank_id: Optional[str] = None) -> dict:
        """Return bank stats including pending_consolidation count.

        GET /v1/default/banks/{bank_id}/stats
        """
        return _request(self._cfg, "GET", self._bank_path("/stats", bank_id))

    def list_operations(
        self,
        limit: int = 10,
        status: Optional[str] = None,
        bank_id: Optional[str] = None,
    ) -> dict:
        """List recent retain operations, optionally filtered by status.

        GET /v1/default/banks/{bank_id}/operations?limit=N[&status=S]
        Used by V5 to detect in-flight operations before kill-9.
        """
        params: dict = {"limit": str(limit)}
        if status:
            params["status"] = status
        return _request(
            self._cfg, "GET",
            self._bank_path("/operations", bank_id),
            params=params,
        )
