"""Mode B (paid extraction) guard rails for the acceptance run.

Mode B runs Olo's production extraction: ``olo.activation.activate`` builds
``make_deepseek_model_fn`` (model, base URL and non-thinking mode from
olo.extractor) exactly as the installed worker does.  What this module adds
is the hard ceiling around it:

* **Run caps.** A run may make at most ``calls`` model requests and spend at
  most ``usd`` (priced with olo.m3.hindsight.budget's deepseek-flash list
  price).  Usage is accumulated in the run's facts after every sync, and each
  sync's Budget gets only what is left.  Spend is reckoned at the *output*
  rate (an upper bound).  Once either cap is reached, a Mode B sync is
  refused.  Every request counts toward the cap: the SDK's own retries are
  off (``DEEPSEEK_MAX_RETRIES``), and a call that raised is charged
  (``Budget.charge_failed``).
* **No Hindsight inference.** Projection is structured-only (no prose is
  retained, so Hindsight's LLM extraction and consolidation never run), and
  the business must be set up with ``HINDSIGHT_API_LLM_PROVIDER=none`` and no
  LLM key.
* **No backlog.** A home that already holds prose awaiting extraction (for
  example a Mode A home) would spend the cap on it: Mode B refuses to sync
  while any extraction candidate is not this run's.

``offline_checks`` is the Mode B preflight.  It reads no Google data and makes
no model call; it reads the key file only to build the production model
function, and never prints it.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Defaults and hard limits a caller cannot exceed.
MAX_CALLS_DEFAULT, MAX_CALLS_LIMIT = 8, 20
MAX_USD_DEFAULT, MAX_USD_LIMIT = 0.05, 0.50
_USAGE_KEYS = ("calls_used", "failed_calls", "tokens_used", "cache_hits", "cost_usd")


@dataclass(frozen=True)
class Caps:
    calls: int
    usd: float


def caps(calls: int, usd: float) -> Caps:
    if not 1 <= calls <= MAX_CALLS_LIMIT:
        raise SystemExit(f"--max-model-calls must be 1..{MAX_CALLS_LIMIT}, not {calls}")
    if not 0 < usd <= MAX_USD_LIMIT:
        raise SystemExit(f"--max-usd must be in (0, {MAX_USD_LIMIT}], not {usd}")
    return Caps(calls, usd)


def usage(facts: dict) -> dict:
    """The run's accumulated Mode B usage (created empty)."""
    used = facts.setdefault("mode_b", {})
    for k in _USAGE_KEYS:
        used.setdefault(k, 0)
    return used


def allowance(c: Caps, used: dict) -> tuple[int, int]:
    """``(calls, tokens)`` the next sync may spend; ``(0, 0)`` once a cap is reached."""
    from olo.m3.hindsight.budget import COST_OUTPUT_PER_M
    calls = c.calls - int(used["calls_used"])
    tokens = int((c.usd - float(used["cost_usd"])) * 1_000_000 / COST_OUTPUT_PER_M)
    return (calls, tokens) if calls > 0 and tokens > 0 else (0, 0)


def record(used: dict, report: dict, c: Caps) -> None:
    for k in _USAGE_KEYS:
        used[k] = round(used[k] + report.get(k, 0), 6) if k == "cost_usd" else used[k] + report.get(k, 0)
    used["caps"] = {"calls": c.calls, "usd": c.usd}


def backlog(conn: sqlite3.Connection, run_id: str) -> int:
    """Prose extraction candidates that are not this run's (would spend the cap)."""
    from olo.signal_content import PROSE_KINDS
    kinds = sorted(PROSE_KINDS)
    marks = ",".join("?" * len(kinds))
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM signal_resolution sr JOIN signals s"
            " ON s.external_id = sr.signal_id AND s.source = sr.source"
            f" WHERE sr.identity_status = 'resolved' AND s.kind IN ({marks})"
            "   AND sr.commitment_status IN ('pending', 'failed')"
            "   AND COALESCE(s.subject, '') NOT LIKE ?",
            (*kinds, f"%acc {run_id}%")).fetchone()[0]
    except sqlite3.OperationalError:
        return 0                          # a fresh home: nothing ingested yet


def _env_names(path: Path) -> dict[str, str]:
    """Only the two settings this guard reads from the business .env."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and name in ("HINDSIGHT_API_LLM_PROVIDER", "HINDSIGHT_API_LLM_API_KEY"):
            out[name] = "set" if name.endswith("API_KEY") and value.strip() else value.strip()
    return out


def offline_checks(home: Path, business: str, key_file: str | None, c: Caps, run_id: str,
                   conn: sqlite3.Connection, now: str) -> tuple[list[str], dict[str, Any]]:
    """Mode B preflight: ``(refusals, facts)`` without Google or a model call."""
    from olo import extractor as ex
    refusals: list[str] = []
    facts: dict[str, Any] = {
        "model": ex.DEFAULT_DEEPSEEK_MODEL, "base_url": ex.DEEPSEEK_BASE_URL,
        "non_thinking": ex.DEEPSEEK_NON_THINKING == {"thinking": {"type": "disabled"}},
        "max_retries": ex.DEEPSEEK_MAX_RETRIES, "timeout_s": ex.DEEPSEEK_TIMEOUT_S,
        "extraction_version": ex.EXTRACTION_VERSION, "caps": {"calls": c.calls, "usd": c.usd}}
    if ex.DEEPSEEK_MAX_RETRIES != 0:
        refusals.append("the DeepSeek client retries on its own: requests would exceed the call cap")
    if not run_id:
        refusals.append("Mode B needs a run id (--run-id / $OLO_ACCEPTANCE_RUN) for its caps")
    if not key_file:
        refusals.append("Mode B needs --key-file (an owner-only DeepSeek key file)")
    else:
        try:
            st = Path(key_file).lstat()
            owner_only = stat.S_ISREG(st.st_mode) and not st.st_mode & 0o077
            facts["key_file"] = {"regular_owner_only": owner_only,
                                 "owned_by_this_user": st.st_uid == os.getuid()}
            if not owner_only or st.st_uid != os.getuid():
                refusals.append("the key file must be a regular file owned by this user, mode 600")
        except FileNotFoundError:
            refusals.append("the key file does not exist")
    env = home / "businesses" / business / ".env"
    hs = _env_names(env) if env.exists() else {}
    facts["hindsight_llm_provider"] = hs.get("HINDSIGHT_API_LLM_PROVIDER", "unset")
    if hs.get("HINDSIGHT_API_LLM_PROVIDER") != "none" or "HINDSIGHT_API_LLM_API_KEY" in hs:
        refusals.append("the Hindsight business must use provider none with no LLM key (no Hindsight inference)")
    row = conn.execute("SELECT key_file FROM hindsight_service WHERE id=1").fetchone() \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='hindsight_service'").fetchone() else None
    if row is not None and row[0]:
        refusals.append("the Hindsight service is configured with an LLM key file")
    facts["prose_backlog"] = backlog(conn, run_id)
    if facts["prose_backlog"]:
        refusals.append(f"{facts['prose_backlog']} prose signals of other runs await extraction:"
                        " Mode B needs a home without that backlog")
    if key_file and not refusals:
        from olo.activation import ActivationRequest, activate
        act = activate(ActivationRequest(
            business_id=business, enable_commitments=True, extract_max_calls=c.calls,
            deepseek_key_file=key_file, own_emails=("business@example.invalid",)), conn, now)
        fn = act.commitment.model_fn
        facts["model_fn"] = {"factory": "olo.extractor.make_deepseek_model_fn",
                             "model_id": getattr(fn, "model_id", None),
                             "extraction_enabled": act.commitment.extraction_enabled}
        if getattr(fn, "model_id", "") != ex._deepseek_model_id(
                ex.DEFAULT_DEEPSEEK_MODEL, ex.DEEPSEEK_BASE_URL, 0, ex.DEFAULT_MAX_OUTPUT_TOKENS):
            refusals.append(f"activation built an unexpected model: {getattr(fn, 'model_id', None)}")
    return refusals, facts
