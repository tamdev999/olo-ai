"""Tests for scripts/olo/check_core_diff.sh — the M1 core-diff guard.

§8.6 (guard portion) acceptance criteria.
Scratch clones are made with git clone --no-hardlinks inside the test's temp dir.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_GUARD_SCRIPT = _REPO_ROOT / "scripts" / "olo" / "check_core_diff.sh"
_BASE_SHA = "f65af53f28ebec14c89b7ae603c701cff5faf6f0"

# On Windows, prefer Git Bash (MSYS2) over Cygwin — Git Bash correctly inherits
# the Windows CWD as a /c/... POSIX path, whereas Cygwin maps it inconsistently.
_GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
_BASH_EXE: str = str(_GIT_BASH) if (os.name == "nt" and _GIT_BASH.exists()) else "bash"


def _bash_available() -> bool:
    try:
        subprocess.run([_BASH_EXE, "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


pytestmark = pytest.mark.skipif(
    not _bash_available(), reason="bash not available on this platform"
)


# ── Diagnostic ────────────────────────────────────────────────────────────────

def test_bash_path_diagnostic(tmp_path: Path) -> None:
    """Verifies three things independently before any guard test runs:
      1. Python sees the cloned directory.
      2. bash (Git Bash on Windows) sees the directory via cwd=.
      3. Guard runs and produces output via cwd=.
    Fails hard with detail so the root cause is immediately visible.
    """
    dest = tmp_path / "clone_diag"

    # 1. Clone and verify Python sees it.
    r = subprocess.run(
        ["git", "clone", "--no-hardlinks", str(_REPO_ROOT), str(dest)],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, f"clone failed: {r.stderr}"
    assert dest.exists(), f"Python: clone dir missing: {dest}"
    assert (dest / ".git").exists(), "Python: .git missing in clone"

    # 2. bash test -d via cwd= — no path argument, no string interpolation.
    check = subprocess.run(
        [_BASH_EXE, "-c", "test -d . && echo DIR_EXISTS || echo DIR_MISSING"],
        capture_output=True, text=True, cwd=str(dest), check=False,
    )
    assert check.returncode == 0, f"bash -c failed: {check.stderr}"
    assert "DIR_EXISTS" in check.stdout, (
        f"bash cannot see cwd.\n"
        f"  bash exe: {_BASH_EXE!r}\n"
        f"  cwd: {dest!r}\n"
        f"  stdout: {check.stdout!r}\n"
        f"  stderr: {check.stderr!r}"
    )

    # 3. Guard runs with cwd= and produces output.
    guard_result = subprocess.run(
        [_BASH_EXE, str(_GUARD_SCRIPT), "HEAD"],
        capture_output=True, text=True, cwd=str(dest), check=False,
    )
    assert guard_result.returncode in (0, 1, 2), (
        f"guard returned unexpected code {guard_result.returncode}"
    )
    assert guard_result.stdout or guard_result.stderr, (
        f"guard produced no output — bash subprocess failed silently.\n"
        f"returncode: {guard_result.returncode}"
    )


# ── Core helper: run the guard with cwd= ──────────────────────────────────────

def _run_guard(repo_path: Path, head_ref: str = "HEAD") -> subprocess.CompletedProcess:
    """Run check_core_diff.sh in *repo_path* with cwd= (no bash cd, no path conversion).

    Uses _BASH_EXE (Git Bash on Windows, system bash on POSIX).
    Both platforms: subprocess sets the OS CWD; bash and git inherit it.
    """
    return subprocess.run(
        [_BASH_EXE, str(_GUARD_SCRIPT), head_ref],
        capture_output=True, text=True, cwd=str(repo_path.resolve()), check=False,
    )


def _clone(tmp_path: Path) -> Path:
    """Clone the repo without hardlinks; return the clone path."""
    dest = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--no-hardlinks", str(_REPO_ROOT), str(dest)],
        capture_output=True, check=True,
    )
    return dest


# ── Guard passes on the M1 work branch ───────────────────────────────────────

def test_guard_passes_on_branch() -> None:
    """Guard exits 0 on the current work branch (all changes in Olo paths)."""
    result = _run_guard(_REPO_ROOT)
    assert result.returncode == 0, (
        f"guard failed unexpectedly:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK:" in result.stdout


# ── Guard rejects core changes ────────────────────────────────────────────────

def test_guard_rejects_core_change(tmp_path: Path) -> None:
    """Modifying a Hermes core file causes exit 1 with the path listed."""
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", _BASE_SHA], capture_output=True,
                   check=False, cwd=str(clone))
    core_file = clone / "hermes_constants.py"
    core_file.write_text(
        core_file.read_text(encoding="utf-8", errors="replace")
        + "\n# olo-guard-test-modification\n",
        encoding="utf-8",
    )
    result = _run_guard(clone)
    assert result.returncode == 1, (
        f"expected exit 1, got {result.returncode}:\n{result.stdout}\n{result.stderr}"
    )
    assert "FAIL:" in result.stdout
    assert "hermes_constants.py" in result.stdout


def test_guard_rejects_core_change_even_with_approval_entry(tmp_path: Path) -> None:
    """Adding olo/CORE_CHANGES.md does NOT approve a core change."""
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", _BASE_SHA], capture_output=True,
                   check=False, cwd=str(clone))
    core_file = clone / "hermes_constants.py"
    core_file.write_text(
        core_file.read_text(encoding="utf-8", errors="replace") + "\n# test\n",
        encoding="utf-8",
    )
    changes_md = clone / "olo" / "CORE_CHANGES.md"
    changes_md.parent.mkdir(exist_ok=True)
    changes_md.write_text(
        "# Olo Core Changes\n- Modified hermes_constants.py (approved)\n",
        encoding="utf-8",
    )
    result = _run_guard(clone)
    assert result.returncode == 1, f"guard should still exit 1:\n{result.stdout}"


def test_guard_rejects_uncommitted_and_untracked_core_files(tmp_path: Path) -> None:
    """Unstaged edit + untracked file under tools/ → exit 1."""
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", _BASE_SHA], capture_output=True,
                   check=False, cwd=str(clone))
    hermes_file = clone / "hermes_constants.py"
    hermes_file.write_text(
        hermes_file.read_text(encoding="utf-8", errors="replace") + "\n# unstaged\n",
        encoding="utf-8",
    )
    untracked = clone / "tools" / "olo_new_tool.py"
    untracked.parent.mkdir(exist_ok=True)
    untracked.write_text("# untracked\n", encoding="utf-8")
    result = _run_guard(clone)
    assert result.returncode == 1
    assert "FAIL:" in result.stdout


def test_guard_rejects_other_workflow(tmp_path: Path) -> None:
    """A workflow other than olo-guard.yml causes exit 1."""
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", _BASE_SHA], capture_output=True,
                   check=False, cwd=str(clone))
    wf = clone / ".github" / "workflows" / "other-workflow.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text("name: Other\non: push\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps: []\n",
                  encoding="utf-8")
    result = _run_guard(clone)
    assert result.returncode == 1
    assert "other-workflow.yml" in result.stdout


# ── Guard errors without the base commit ─────────────────────────────────────

def test_guard_errors_without_baseline(tmp_path: Path) -> None:
    """A repo that does not contain the base commit → exit 2.

    git clone --depth 1 --no-hardlinks still shares the local pack, so the
    base commit is reachable. Instead we init a fresh empty repo and make a
    commit that is not a descendant of the base — the guard must reject it.
    """
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"],
                   capture_output=True, check=True, cwd=str(repo))
    subprocess.run(["git", "config", "user.name", "Test"],
                   capture_output=True, check=True, cwd=str(repo))
    # Commit one file — repo has no connection to the Hermes history
    (repo / "placeholder.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "placeholder.txt"],
                   capture_output=True, check=True, cwd=str(repo))
    subprocess.run(["git", "commit", "-m", "init"],
                   capture_output=True, check=True, cwd=str(repo))

    result = _run_guard(repo)
    assert result.returncode == 2, (
        f"expected exit 2 (base commit not in repo):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "fetch" in result.stderr.lower() or "not in this clone" in result.stderr


# ── Guard passes on olo-only changes ─────────────────────────────────────────

def test_guard_passes_olo_only_changes(tmp_path: Path) -> None:
    """Adding a file under olo/ does not trigger the guard."""
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", _BASE_SHA], capture_output=True,
                   check=False, cwd=str(clone))
    new_file = clone / "olo" / "_test_new_file.py"
    new_file.parent.mkdir(exist_ok=True)
    new_file.write_text("# olo-only\n", encoding="utf-8")
    result = _run_guard(clone)
    assert result.returncode == 0, (
        f"guard should pass for olo-only change:\n{result.stdout}\n{result.stderr}"
    )


def test_guard_passes_olo_guard_workflow_only(tmp_path: Path) -> None:
    """Adding only .github/workflows/olo-guard.yml is permitted."""
    clone = _clone(tmp_path)
    subprocess.run(["git", "checkout", _BASE_SHA], capture_output=True,
                   check=False, cwd=str(clone))
    wf = clone / ".github" / "workflows" / "olo-guard.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text("name: Olo Guard\non: push\njobs: {}\n", encoding="utf-8")
    result = _run_guard(clone)
    assert result.returncode == 0, (
        f"guard should pass for olo-guard.yml only:\n{result.stdout}\n{result.stderr}"
    )
