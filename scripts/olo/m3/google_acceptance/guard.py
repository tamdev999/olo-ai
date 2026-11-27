"""Preflight: refuse anything but one dedicated, read-only, disposable setup.

Configuration comes from the operator's environment (this is a test
harness, not Olo configuration):

  OLO_ACCEPTANCE_HOME      the disposable HERMES_HOME of the acceptance run
  OLO_ACCEPTANCE_ACCOUNT   the dedicated test account's address
  OLO_ACCEPTANCE_CONFIRM   the same address again: "this account holds only
                           synthetic test data"
  OLO_ACCEPTANCE_CUSTOMER  the second test identity (the "customer")
  OLO_ACCEPTANCE_EVIDENCE  where evidence goes; never inside the home
  OLO_ACCEPTANCE_ALIAS     Olo account alias (default "accept")
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

ALLOWED_SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
})


@dataclass(frozen=True)
class Settings:
    home: Path
    account: str
    customer: str
    evidence: Path
    alias: str = "accept"


@dataclass
class Preflight:
    settings: Optional[Settings]
    refusals: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.settings is not None and not self.refusals


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def settings_from(env: Mapping[str, str]) -> Preflight:
    """Parse and check the environment (no Google call, no credential read)."""
    missing = [k for k in ("OLO_ACCEPTANCE_HOME", "OLO_ACCEPTANCE_ACCOUNT", "OLO_ACCEPTANCE_CONFIRM",
                           "OLO_ACCEPTANCE_CUSTOMER", "OLO_ACCEPTANCE_EVIDENCE") if not env.get(k)]
    if missing:
        return Preflight(None, [f"not configured: {', '.join(missing)}"])
    account = env["OLO_ACCEPTANCE_ACCOUNT"].strip().lower()
    s = Settings(home=Path(env["OLO_ACCEPTANCE_HOME"]), account=account,
                 customer=env["OLO_ACCEPTANCE_CUSTOMER"].strip().lower(),
                 evidence=Path(env["OLO_ACCEPTANCE_EVIDENCE"]),
                 alias=env.get("OLO_ACCEPTANCE_ALIAS", "accept"))
    pf = Preflight(s)
    if env["OLO_ACCEPTANCE_CONFIRM"].strip().lower() != account:
        pf.refusals.append("OLO_ACCEPTANCE_CONFIRM must repeat OLO_ACCEPTANCE_ACCOUNT exactly")
    if s.customer == account:
        pf.refusals.append("the customer identity must be a second account, not the business's")
    default_home = Path.home() / ".hermes"
    if s.home.resolve() == default_home.resolve() or not s.home.is_dir():
        pf.refusals.append(f"OLO_ACCEPTANCE_HOME must be an existing disposable home, not {default_home}")
    if _inside(s.evidence, s.home) or _inside(s.home, s.evidence):
        pf.refusals.append("evidence must be kept apart from the home that holds the credentials")
    return pf


def check_credentials(pf: Preflight, *, load: Callable[[str], Any],
                      aliases: Callable[[], list[str]]) -> Preflight:
    """Credentials of the dedicated account only, read-only, Testing mode."""
    s = pf.settings
    if s is None:
        return pf
    others = sorted(set(aliases()) - {s.alias})
    if others:
        pf.refusals.append(f"the acceptance home holds other Google accounts: {others}")
    try:
        creds = load(s.alias)
    except (FileNotFoundError, ValueError, OSError) as exc:
        pf.refusals.append(f"account {s.alias!r} not connected: {type(exc).__name__}")
        return pf
    granted = frozenset(creds.granted_scopes)
    pf.facts.update({"granted_scopes": sorted(granted),
                     "oauth_publishing_status": creds.oauth_publishing_status,
                     "scope_profile": creds.scope_profile})
    if (creds.email_address or "").strip().lower() != s.account:
        pf.refusals.append("the connected account is not OLO_ACCEPTANCE_ACCOUNT")
    if granted != ALLOWED_SCOPES:
        pf.refusals.append(f"scopes must be exactly the read-only set; extra {sorted(granted - ALLOWED_SCOPES)},"
                           f" missing {sorted(ALLOWED_SCOPES - granted)}")
    if creds.oauth_publishing_status != "testing":
        pf.refusals.append("the OAuth client must be in Testing mode for acceptance")
    return pf


def check_profile(pf: Preflight, client: Any) -> Preflight:
    """The live account behind the token (one read-only Gmail call)."""
    if pf.settings is None:
        return pf
    profile = client.get_json("gmail", "/users/me/profile", []) or {}
    seen = str(profile.get("emailAddress") or "").strip().lower()
    pf.facts["profile_matches"] = seen == pf.settings.account
    pf.facts["messages_total"] = profile.get("messagesTotal")
    if seen != pf.settings.account:
        pf.refusals.append("the token belongs to another mailbox than OLO_ACCEPTANCE_ACCOUNT")
    return pf


def run_preflight(env: Mapping[str, str] = os.environ, *, client_factory=None) -> Preflight:
    """Every check, in order; Google is contacted only once the local checks
    pass (one profile read)."""
    pf = settings_from(env)
    if not pf.ok:
        return pf
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(pf.settings.home)
    try:
        from olo.google.credentials import list_accounts, load_credentials
        check_credentials(pf, load=load_credentials, aliases=list_accounts)
        if pf.ok:
            from olo.google.http import GoogleClient
            factory = client_factory or GoogleClient
            check_profile(pf, factory(load_credentials(pf.settings.alias)))
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old
    return pf
