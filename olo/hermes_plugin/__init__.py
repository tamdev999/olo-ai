"""Olo AI Hermes plugin — the `hermes olo` CLI and the read-only customer context tool.

``olo_customer_context`` (toolset ``olo``) is the agent's only Olo surface:
the authoritative customer brief from olo.agent_context.  It reads; it never
acts.  Available when the active profile has an Olo database.
No side effects at import time.
"""
from __future__ import annotations

import json

TOOL_NAME = "olo_customer_context"

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Read Olo's authoritative business context for one customer: the commitment ledger's "
        "current commitments (status, confirmed date and start time, pending customer requests, "
        "superseded dates), events awaiting an operator, and labelled supporting memories. "
        "Read-only: it never contacts the customer or changes anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "customer": {
                "type": "string",
                "description": "The customer's contact id (cid_...), email address, or phone "
                               "number in international format (+15551234567).",
            },
            "question": {
                "type": "string",
                "description": "Optional: what you want to know; used to recall supporting "
                               "memories. Commitments are always included.",
            },
        },
        "required": ["customer"],
    },
}


def _olo_db():
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "olo" / "olo.db"


def _olo_available() -> bool:
    """The active profile has an Olo database (opt-in: `hermes olo setup`)."""
    return _olo_db().is_file()


def _hindsight_factory():
    """This profile's Hindsight client for a business, or None when the
    business has no Hindsight registry entry (projection not set up)."""
    from hermes_constants import get_hermes_home
    registry = get_hermes_home() / "hindsight_port_registry.json"
    if not registry.is_file():
        return None

    def factory(business_id: str):
        from olo.m3.hindsight.client import HindsightClient, HindsightConfig
        return HindsightClient(HindsightConfig.for_business(business_id, registry_path=str(registry)))
    return factory


def customer_context_tool(args: dict, **_kw) -> str:
    """Tool handler: the customer brief as JSON (read-only connection)."""
    import sqlite3

    from olo.agent_context import customer_brief

    db = _olo_db()
    if not db.is_file():
        return json.dumps({"error": "Olo is not set up for this profile"})
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        brief = customer_brief(conn, str(args.get("customer") or ""),
                               str(args.get("question") or ""),
                               client_factory=_hindsight_factory())
    finally:
        conn.close()
    return json.dumps(brief, default=str)


def register(ctx) -> None:
    """Register the `hermes olo` CLI command group and the context tool."""
    from olo.hermes_plugin.cli import setup_parser, handler

    ctx.register_cli_command(
        name="olo",
        help="Olo AI business signal pipeline",
        setup_fn=setup_parser,
        handler_fn=handler,
        description="Olo AI — business signal pipeline and commitment ledger",
    )
    ctx.register_tool(
        name=TOOL_NAME, toolset="olo", schema=TOOL_SCHEMA, handler=customer_context_tool,
        check_fn=_olo_available, description=TOOL_SCHEMA["description"],
    )
