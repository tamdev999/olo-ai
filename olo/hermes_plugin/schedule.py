"""Cron schedules for an Olo business (`hermes olo setup --schedule`).

Every scheduled Olo job is a no-agent Hermes cron job running a shim from
``$HERMES_HOME/scripts`` (cron refuses anything else):

  olo-<source>[-<alias>]   the source's worker (``olo_monitor_*.py``)
  olo-hindsight-<biz>      one Hindsight supervision pass every minute
                           (``olo_hindsight_<biz>.py``), installed with
                           ``--enable-projection``

Shims pin the checkout that installed them ahead of anything else on
``sys.path`` and refuse to run another copy, and jobs pin the interpreter
that ran setup, so cron runs this code with these dependencies.  Creation is
idempotent by job name: re-running setup updates the job in place.
"""
from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["SUPERVISOR_SCHEDULE", "ensure_job", "shim_source", "supervisor_job_name",
           "supervisor_shim_name"]

SUPERVISOR_SCHEDULE = "every 1m"

_PREAMBLE = '''#!/usr/bin/env python3
# Olo AI shim — installed by `hermes olo setup`.
# Do NOT edit directly; re-run `hermes olo setup ... --force` to regenerate.
#
# The literals below are produced by repr() at setup time (injection-safe).
import os
import sys

_repo_root = {repo_root}
# Always the checkout that installed this shim, ahead of any other copy an
# editable install or PYTHONPATH could put first (a stale one ran in WSL).
sys.path.insert(0, _repo_root)
import hermes_constants as _hc
import olo as _olo

for _mod in (_olo, _hc):
    _where = os.path.abspath(_mod.__file__)
    if not _where.startswith(os.path.abspath(_repo_root) + os.sep):
        raise SystemExit(f"olo shim: {{_mod.__name__}} imported from {{_where}}, "
                         f"not from {{_repo_root}}; re-run hermes olo setup")
'''


def shim_source(repo_root: str, body: str) -> str:
    """A shim: the repository pin, then *body* (already literal-safe)."""
    return _PREAMBLE.format(repo_root=repr(repo_root)) + body


def supervisor_shim_name(business_id: str) -> str:
    return f"olo_hindsight_{business_id.replace('-', '_')}.py"


def supervisor_job_name(business_id: str) -> str:
    return f"olo-hindsight-{business_id}"


def ensure_job(name: str, schedule: str, script: str) -> tuple[str, str]:
    """Create or update the no-agent cron job *name*; ``(job id, action)``."""
    from cron.jobs import create_job, list_jobs, update_job
    wanted = {"script": script, "no_agent": True, "deliver": "local",
              "interpreter": sys.executable}
    for job in list_jobs(include_disabled=True):
        if job.get("name") != name:
            continue
        changes = {k: v for k, v in wanted.items() if job.get(k) != v}
        if job.get("schedule_display") != schedule:
            changes["schedule"] = schedule
        if changes:
            update_job(job["id"], changes)
            return job["id"], "updated"
        return job["id"], "unchanged"
    job = create_job(None, schedule, name=name, **wanted)
    return job["id"], "created"


def write_shim(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
