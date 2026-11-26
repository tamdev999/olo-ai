"""Tests for olo.hermes_plugin — plugin registration, CLI commands, shim install.

§8.6 (plugin portion) acceptance criteria.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_enabled_config(hermes_home: Path, names: list[str]) -> None:
    """Write plugins.enabled allow-list to config.yaml."""
    import hermes_yaml as yaml
    cfg_path = hermes_home / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"plugins": {"enabled": names}}), encoding="utf-8")


def _install_plugin_symlink(hermes_home: Path, repo_root: Path) -> None:
    """Create $HERMES_HOME/plugins/olo → <repo>/olo/hermes_plugin.

    Uses a symlink on POSIX; falls back to a directory junction (Windows)
    or shutil.copytree if the junction also fails.
    """
    import shutil
    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir(exist_ok=True)
    link = plugins_dir / "olo"
    if link.exists():
        return
    target = repo_root / "olo" / "hermes_plugin"
    if os.name == "nt":
        try:
            # Try directory junction first (no special privilege needed on Windows)
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            # Last resort: copy the directory
            shutil.copytree(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def _repo_root() -> Path:
    return Path(__file__).parent.parent.parent


# ── Plugin disabled by default ────────────────────────────────────────────────

def test_plugin_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without plugins.enabled, the olo plugin is not activated."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Install symlink but do NOT write config
    _install_plugin_symlink(home, _repo_root())

    import hermes_cli.plugins as pmod
    pmod._reset_plugin_managers_for_tests()
    mgr = pmod.PluginManager()
    mgr.discover_and_load()

    if "olo" in mgr._plugins:
        plugin_entry = mgr._plugins["olo"]
        assert not plugin_entry.enabled, (
            f"olo plugin should NOT be enabled without plugins.enabled, "
            f"but found enabled={plugin_entry.enabled}"
        )
    # Whether discovered or not, no cli_command for 'olo'
    pmod._reset_plugin_managers_for_tests()


def test_plugin_enabled_runs_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With plugins.enabled containing 'olo', hermes olo status exits 0."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _install_plugin_symlink(home, _repo_root())
    _write_enabled_config(home, ["olo"])

    import hermes_cli.plugins as pmod
    pmod._reset_plugin_managers_for_tests()
    mgr = pmod.PluginManager()
    mgr.discover_and_load()

    # Olo CLI command should be registered
    olo_cmd = next(
        (c for c in mgr._cli_commands.values() if c.get("name") == "olo"),
        None,
    )
    assert olo_cmd is not None, "olo CLI command not registered"
    pmod._reset_plugin_managers_for_tests()


# ── Setup writes shim once ────────────────────────────────────────────────────

def test_setup_writes_shim_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup installs shim; second call without --force is refused."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    args = types.SimpleNamespace(source="synthetic", option=[], force=False)
    _cmd_setup(args)

    shim = home / "scripts" / "olo_monitor_synthetic.py"
    assert shim.exists()
    content = shim.read_text(encoding="utf-8")
    assert "olo.runner" in content

    # Second call without --force should exit 1
    with pytest.raises(SystemExit) as exc:
        _cmd_setup(args)
    assert exc.value.code == 1


def test_setup_force_rewrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """setup --force overwrites existing shim."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types

    args_first = types.SimpleNamespace(source="synthetic", option=[], force=False)
    _cmd_setup(args_first)

    args_force = types.SimpleNamespace(source="synthetic", option=[], force=True)
    _cmd_setup(args_force)  # should not raise

    shim = home / "scripts" / "olo_monitor_synthetic.py"
    assert shim.exists()


# ── No tools or hooks registered ─────────────────────────────────────────────

def test_plugin_adds_only_the_read_only_context_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The olo plugin registers exactly one tool — the read-only
    olo_customer_context (post-audit batch 4) — and no hook callbacks."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _install_plugin_symlink(home, _repo_root())
    _write_enabled_config(home, ["olo"])

    import hermes_cli.plugins as pmod
    pmod._reset_plugin_managers_for_tests()
    mgr = pmod.PluginManager()
    mgr.discover_and_load()

    # Exactly the context tool, nothing else
    olo_tools = sorted(t for t in mgr._plugin_tool_names if "olo" in t.lower())
    assert olo_tools == ["olo_customer_context"], f"olo plugin registered tools: {olo_tools}"

    # No hook callbacks from olo
    for hook_name in list(pmod.VALID_HOOKS):
        callbacks = mgr._hooks.get(hook_name, [])
        for cb in callbacks:
            plugin_key = getattr(cb, "__plugin_key__", None) or ""
            assert "olo" not in plugin_key.lower(), (
                f"olo plugin registered hook callback for {hook_name}: {cb}"
            )

    pmod._reset_plugin_managers_for_tests()


# ── Shim content ──────────────────────────────────────────────────────────────

def test_shim_contains_fallback_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed shim contains both direct import attempt and repo-root fallback."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types
    args = types.SimpleNamespace(source="synthetic", option=[], force=False)
    _cmd_setup(args)

    shim = home / "scripts" / "olo_monitor_synthetic.py"
    content = shim.read_text(encoding="utf-8")
    assert "try:" in content
    assert "except ImportError:" in content
    assert "sys.path.insert" in content
    assert "olo.runner" in content


def test_shim_executes_successfully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed shim runs without error (25 signals ingested)."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_setup
    import types
    args = types.SimpleNamespace(source="synthetic", option=[], force=False)
    _cmd_setup(args)

    shim = home / "scripts" / "olo_monitor_synthetic.py"
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "PYTHONPATH": str(_repo_root()),
    }
    proc = subprocess.run(
        [sys.executable, str(shim)],
        capture_output=True, text=True, env=env, check=False,
    )
    assert proc.returncode == 0, f"shim failed: {proc.stderr}"
    assert proc.stdout == "", "shim should produce empty stdout on success"


# ── Status on empty home ──────────────────────────────────────────────────────

def test_status_works_on_empty_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hermes olo status works when no DB has been initialised yet."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from olo.hermes_plugin.cli import _cmd_status
    import types
    _cmd_status(types.SimpleNamespace())  # should not raise
