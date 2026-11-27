"""olo/activation.py — explicit, fail-closed activation of the S5 stages.

The commitment and projection stages of ``run_source`` are disabled unless an
operator enables them for one named business.  ``activate`` validates the
request before any signal is fetched and refuses (``ActivationError``) when:

* no business is named, or this olo.db is bound to a different business;
* projection is enabled without an explicit bank, or the bank differs from
  the business's entry in the Hindsight port registry, or from the bank this
  olo.db is already bound to;
* the DeepSeek key, the Hindsight registry or its tenant API key is missing;
* any budget is missing or not positive (extraction calls; projection
  document caps and usage budget);
* the commitment stage would run without trusted business identity (the
  business's own addresses, configured or learned from its sent mail).

Configuration reused, not invented: DeepSeek via ``make_deepseek_model_fn``
(key from ``DEEPSEEK_API_KEY`` or an owner-only key file), Hindsight via
``HindsightConfig.for_business`` (``$HERMES_HOME/hindsight_port_registry.json``
or an explicit registry path), spend via ``UsageGuard`` (server-reported LLM
usage; a client-side stop, not a server-enforced cap).
"""
from __future__ import annotations

import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "Activation",
    "ActivationError",
    "ActivationRequest",
    "activate",
    "bind_business",
    "load_key_file",
]

_BUSINESS_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_KEY_NAMES = ("DEEPSEEK_API_KEY", "HINDSIGHT_API_LLM_API_KEY")


class ActivationError(Exception):
    """The requested activation is unsafe or incomplete; nothing was started."""


@dataclass(frozen=True)
class ActivationRequest:
    business_id: Optional[str] = None
    enable_commitments: bool = False
    enable_projection: bool = False
    bank_id: Optional[str] = None
    deepseek_key_file: Optional[str] = None
    extract_max_calls: Optional[int] = None
    extract_max_tokens: Optional[int] = None
    projection_max_docs: Optional[int] = None
    projection_max_prose_docs: Optional[int] = None
    projection_budget_usd: Optional[float] = None
    own_emails: tuple[str, ...] = ()
    hindsight_registry: Optional[str] = None
    deepseek_base_url: Optional[str] = None

    @property
    def enabled(self) -> bool:
        return self.enable_commitments or self.enable_projection


@dataclass(frozen=True)
class Activation:
    commitment: Any = None          # CommitmentStageConfig | None
    projection: Any = None          # ProjectionConfig | None
    own_identifiers: frozenset = frozenset()


def load_key_file(path: str) -> str:
    """The DeepSeek key from an owner-only env-format file (never echoed)."""
    p = Path(path)
    try:
        st = p.lstat()
    except FileNotFoundError:
        raise ActivationError(f"key file not found: {p}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ActivationError(f"key file must be a regular file, not a link: {p}")
    if sys.platform != "win32" and st.st_mode & 0o077:
        raise ActivationError(f"key file must be owner-only (chmod 600): {p}")
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            values[name.strip()] = value.strip().strip("\"'")
    for name in _KEY_NAMES:
        if values.get(name):
            return values[name]
    raise ActivationError(f"key file defines none of {', '.join(_KEY_NAMES)}: {p}")


def _binding(conn: sqlite3.Connection, name: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM olo_bindings WHERE name=?", (name,)).fetchone()
    return row[0] if row else None


def bind_business(conn: sqlite3.Connection, business_id: str, now: str) -> None:
    """Bind this olo.db to *business_id* once; refuse any other business later."""
    bound = _binding(conn, "business_id")
    if bound is None:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO olo_bindings (name, value, bound_at)"
                     " VALUES ('business_id', ?, ?)", (business_id, now))
        conn.execute("COMMIT")
        bound = _binding(conn, "business_id")
    if bound != business_id:
        raise ActivationError(
            f"this olo.db belongs to business {bound!r}, not {business_id!r}")


def _no_model(prompt: str):
    """The model of a no-model activation (--extract-max-calls 0): the budget
    refuses before any call, and this refuses should one ever be attempted."""
    raise RuntimeError("extraction is disabled (--extract-max-calls 0)")


def _non_negative(value: Optional[float], name: str) -> float:
    if value is None:
        raise ActivationError(f"{name} is required when the stage is enabled")
    if value < 0:
        raise ActivationError(f"{name} must not be negative (got {value!r})")
    return value


def _positive(value: Optional[float], name: str) -> float:
    if value is None:
        raise ActivationError(f"{name} is required when the stage is enabled")
    if value <= 0:
        raise ActivationError(f"{name} must be positive (got {value!r})")
    return value


#: Where extraction may send business text: DeepSeek itself, or a loopback
#: server (tests and local proxies).  Anything else is refused.
_DEEPSEEK_URL_RE = re.compile(
    r"https://api\.deepseek\.com(/v1)?/?|http://(127\.0\.0\.1|localhost):\d{1,5}(/v1)?/?")


def _deepseek_base_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not _DEEPSEEK_URL_RE.fullmatch(value):
        raise ActivationError(
            f"--deepseek-base-url must be https://api.deepseek.com[/v1] or a loopback "
            f"http://127.0.0.1:<port>[/v1] URL, not {value!r}")
    return value.rstrip("/")


def _registry_path(req: ActivationRequest, environ: Mapping[str, str]) -> str:
    if req.hindsight_registry:
        path = Path(req.hindsight_registry)
    elif environ.get("HERMES_HOME"):
        path = Path(environ["HERMES_HOME"]) / "hindsight_port_registry.json"
    else:
        from hermes_constants import get_hermes_home
        path = get_hermes_home() / "hindsight_port_registry.json"
    if not path.exists():
        raise ActivationError(f"Hindsight port registry not found: {path}")
    return str(path)


def activate(
    req: ActivationRequest,
    conn: sqlite3.Connection,
    now: str,
    *,
    environ: Mapping[str, str] = os.environ,
    model_factory: Optional[Callable[..., Any]] = None,
    hindsight_factory: Optional[Callable[..., Any]] = None,
) -> Activation:
    """Validate *req* and build the stage configs, or raise ActivationError.

    Disabled requests return an empty Activation without touching *conn*.
    """
    if not req.enabled:
        return Activation()
    if not req.business_id or not _BUSINESS_RE.fullmatch(req.business_id):
        raise ActivationError("an explicit --business-id (lowercase id) is required")

    commitment = projection = None
    own: frozenset = frozenset()

    if req.enable_projection:
        if not req.bank_id:
            raise ActivationError("--bank-id is required to enable projection")
        bound_bank = _binding(conn, "hindsight_bank_id")
        if bound_bank is not None and bound_bank != req.bank_id:
            raise ActivationError(
                f"this olo.db is bound to bank {bound_bank!r}, not {req.bank_id!r}")
        max_docs = int(_positive(req.projection_max_docs, "--projection-max-docs"))
        # 0 = structured documents only: prose (which Hindsight's LLM would
        # extract) is never submitted.
        max_prose = int(_non_negative(req.projection_max_prose_docs, "--projection-max-prose-docs"))
        budget_usd = _positive(req.projection_budget_usd, "--projection-budget-usd")
        from olo.m3.hindsight.client import HindsightConfig
        try:
            hcfg = HindsightConfig.for_business(
                req.business_id, registry_path=_registry_path(req, environ))
        except ValueError as exc:
            raise ActivationError(str(exc)) from None
        if hcfg.bank_id != req.bank_id:
            raise ActivationError(
                f"registry routes {req.business_id!r} to bank {hcfg.bank_id!r}, "
                f"not {req.bank_id!r}")
        if not hcfg.api_key:
            raise ActivationError(f"no Hindsight tenant API key for {req.business_id!r}")

    if req.enable_commitments:
        # 0 = no model: calendar commitments (deterministic) are applied and
        # prose waits; no key is read and no call can be made.
        max_calls = int(_non_negative(req.extract_max_calls, "--extract-max-calls"))
        max_tokens = (int(_positive(req.extract_max_tokens or max_calls * 2_000,
                                    "--extract-max-tokens")) if max_calls else 0)
        key = ""
        if max_calls:
            key = (load_key_file(req.deepseek_key_file) if req.deepseek_key_file
                   else environ.get("DEEPSEEK_API_KEY", ""))
            if not key:
                raise ActivationError(
                    "no DeepSeek key: set DEEPSEEK_API_KEY or pass --deepseek-key-file"
                    " (or --extract-max-calls 0 for calendar commitments only)")
        base_url = _deepseek_base_url(req.deepseek_base_url)
        from olo.adapters.contacts import load_own_identifiers
        own = frozenset(("email", e.strip().lower()) for e in req.own_emails if "@" in e)
        own |= load_own_identifiers(conn)
        if not own:
            raise ActivationError(
                "no trusted business identity: pass --own-email (the business's "
                "own address) or ingest its sent mail first")

    # Everything validated: only now bind and build.
    bind_business(conn, req.business_id, now)

    if req.enable_commitments:
        from olo.commitments import CommitmentStageConfig
        from olo.extractor import Budget, make_deepseek_model_fn
        if max_calls:
            model_kwargs = {"base_url": base_url} if base_url else {}
            model_fn = (model_factory or make_deepseek_model_fn)(api_key=key, **model_kwargs)
        else:
            model_fn = _no_model
        commitment = CommitmentStageConfig(
            model_fn=model_fn,
            budget=Budget(max_calls=max_calls, max_total_tokens=max_tokens),
            extraction_enabled=bool(max_calls),
        )

    if req.enable_projection:
        from olo.m3.hindsight.budget import UsageGuard
        from olo.m3.hindsight.client import HindsightClient
        from olo.projection import ProjectionConfig, ProjectionLimits
        client = (hindsight_factory or HindsightClient)(hcfg)
        projection = ProjectionConfig(
            client=client,
            bank_id=req.bank_id,
            usage_guard=UsageGuard(client, bank_id=req.bank_id, budget_usd=budget_usd),
            limits=ProjectionLimits(max_docs_per_run=max_docs, max_prose_docs_per_run=max_prose),
            await_commitment=req.enable_commitments,
        )

    return Activation(commitment=commitment, projection=projection, own_identifiers=own)
