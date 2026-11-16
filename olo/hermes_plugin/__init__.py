"""Olo AI Hermes plugin — registers the `hermes olo` CLI command group.

§6.1: CLI-only. No tools, no hooks, no system-prompt sections in M1.
No side effects at import time.
"""
from __future__ import annotations


def register(ctx) -> None:
    """Register the `hermes olo` CLI command group.

    M1 surface: CLI only.
    - No tools registered (keeps test_prompt_surface.py byte-identical).
    - No hooks registered.
    - No system-prompt sections.
    """
    from olo.hermes_plugin.cli import setup_parser, handler

    ctx.register_cli_command(
        name="olo",
        help="Olo AI business signal pipeline",
        setup_fn=setup_parser,
        handler_fn=handler,
        description="Olo AI M1 — zero-LLM business signal pipeline",
    )
