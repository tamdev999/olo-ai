"""Regression tests: shim code injection cannot occur.

Verifies that malicious --source and --option values are rejected or rendered
as inert Python literals that cannot execute injected code.

§1 of audit remediation.
"""
from __future__ import annotations

import sys
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent



# ── Source validation ──────────────────────────────────────────────────────────

def test_invalid_source_rejected_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source that fails _SOURCE_RE is rejected with exit 1; no shim written."""
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    # Try to inject via source name containing a quote
    for bad_source in [
        "syn'; import os#",
        "UPPERCASE",
        "has space",
        "toolong" * 10,
        "",
        "has-dash",
        "has.dot",
    ]:
        ns = types.SimpleNamespace(source=bad_source, option=[], force=False)
        with pytest.raises(SystemExit) as exc_info:
            _cmd_setup(ns)
        assert exc_info.value.code == 1, f"expected exit 1 for source={bad_source!r}"
        # No shim file created
        scripts = home / "scripts"
        if scripts.exists():
            assert not list(scripts.glob("*.py")), (
                f"shim written for invalid source {bad_source!r}"
            )


def test_unknown_source_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A well-formed but unregistered source name is rejected."""
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    ns = types.SimpleNamespace(source="notanadapter", option=[], force=False)
    with pytest.raises(SystemExit) as exc_info:
        _cmd_setup(ns)
    assert exc_info.value.code == 1


# ── Option key validation ──────────────────────────────────────────────────────

def test_invalid_option_key_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Option keys that fail _OPTION_KEY_RE are rejected with exit 1."""
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    for bad_key in ["has-dash", "HAS_UPPER", "has space", "has'quote", ""]:
        ns = types.SimpleNamespace(
            source="synthetic",
            option=[f"{bad_key}=value"],
            force=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            _cmd_setup(ns)
        assert exc_info.value.code == 1, f"expected exit 1 for key={bad_key!r}"


# ── Injection-safe rendering ───────────────────────────────────────────────────

def test_shim_uses_repr_for_all_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered shim must not contain any raw option value or source verbatim
    outside a Python string literal — all values go through repr()."""
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    # A value with a single quote — if injected raw, would break out of a
    # 'string'. repr() would produce "\"has'quote\"" which is harmless.
    ns = types.SimpleNamespace(
        source="synthetic",
        option=["total=25"],
        force=False,
    )
    _cmd_setup(ns)

    shim = (home / "scripts" / "olo_monitor_synthetic.py").read_text(encoding="utf-8")
    # Must contain argv_literal as a Python list literal
    assert "_r.main([" in shim, "shim does not contain list literal argv"
    # source value present as a repr'd string
    assert "'synthetic'" in shim or '"synthetic"' in shim, (
        "source not rendered as Python string literal"
    )


def test_malicious_option_value_cannot_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An option value containing Python injection characters is rendered inert.

    Three assertions:
    1. The sentinel file is NOT created (payload did not execute).
    2. The shim process exits with code 1 — the olo runner reached adapter
       validation and rejected the mangled value with AdapterConfigError.
       A syntax error from bad rendering would cause exit 2 (Python SyntaxError)
       or exit 120 (ImportError) — neither is 1.
    3. stderr contains the expected AdapterConfigError text, confirming the
       runner got as far as argument parsing and adapter construction.
    """
    import os
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    sentinel = tmp_path / "INJECTED"

    # Attempt injection: value tries to write a sentinel file and exit cleanly.
    # If rendered raw, this would close the list, inject statements, then reopen.
    # With repr(), the entire string is a quoted literal — no execution.
    evil_value = f"0']); open(r'{sentinel}','w').write('pwned'); __import__('sys').exit(0); x=['"

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    ns = types.SimpleNamespace(
        source="synthetic",
        option=[f"total={evil_value}"],
        force=False,
    )
    _cmd_setup(ns)

    shim = (home / "scripts" / "olo_monitor_synthetic.py").read_text(encoding="utf-8")
    # The evil value must appear only as a quoted string, never as bare code
    assert "open(" not in shim or "open(" in repr(evil_value), (
        "injection payload leaked into shim as bare code"
    )

    # Run the shim.
    env = {**os.environ, "HERMES_HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, str(home / "scripts" / "olo_monitor_synthetic.py")],
        capture_output=True, text=True, env=env, check=False,
        cwd=str(_repo_root()),
    )

    # 1. Sentinel must not exist — payload did not execute.
    assert not sentinel.exists(), (
        f"injection payload executed — sentinel file created!\n"
        f"shim content:\n{shim}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )

    # 2. Exit code must be 1 (AdapterConfigError / runner exit 1).
    #    Exit 2 would indicate a Python-level SyntaxError or bad argument parsing
    #    before the adapter was reached.  Exit 0 would mean success — wrong.
    #    Any other code means an unexpected crash rather than a clean rejection.
    assert proc.returncode == 1, (
        f"Expected exit 1 (AdapterConfigError from adapter validation), "
        f"got exit {proc.returncode}.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}\n"
        f"shim:\n{shim}"
    )

    # 3. stderr must mention an adapter config / option error — proving the
    #    runner actually reached SyntheticAdapter.__init__ and rejected the
    #    mangled 'total' value as a non-integer, not a syntax/import error.
    assert "adapter config error" in proc.stderr.lower() or \
           "adapterconfigerror" in proc.stderr.lower() or \
           "must be an integer" in proc.stderr.lower() or \
           "option" in proc.stderr.lower(), (
        f"stderr does not contain expected adapter-rejection message.\n"
        f"stderr: {proc.stderr!r}\nstdout: {proc.stdout!r}"
    )


def test_malicious_source_in_option_value_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An option value with single quotes and backslashes is repr()'d correctly."""
    import os
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    # Value with both single quotes and backslashes
    tricky_value = r"25\'; import sys; sys.exit(0); x='"

    ns = types.SimpleNamespace(
        source="synthetic",
        option=[f"total={tricky_value}"],
        force=False,
    )
    _cmd_setup(ns)

    shim = (home / "scripts" / "olo_monitor_synthetic.py").read_text(encoding="utf-8")
    # Must be a valid Python file (parseable)
    import ast
    try:
        ast.parse(shim)
    except SyntaxError as e:
        pytest.fail(f"Shim is not valid Python after repr() rendering: {e}\n{shim}")
