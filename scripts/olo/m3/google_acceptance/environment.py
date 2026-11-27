"""Host checks for the live acceptance run (no Google call, no credential read).

The run happens in WSL's default network namespace: Google must be
reachable, so the private namespace used for the offline suite cannot be
used.  Refused here:

* running as root (pg0, Hindsight's embedded Postgres, refuses root);
* a network namespace without a default route (the offline workaround);
* ``olo`` or ``hermes_cli`` imported from anywhere but this checkout (the
  venv's editable install may point at an older tree);
* a 127.0.0.1 listener that is still refused after the listener-registration
  delay of WSL's VirtioProxy fallback (about 0.25 s; see the readiness doc).
"""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path
from typing import Callable, Optional

REGISTRATION_WAIT_S = 1.0


def _default_route(route_table: str) -> bool:
    """``/proc/net/route`` has an IPv4 default route (destination 00000000)."""
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            return True
    return False


def loopback_reachable(wait_s: float = REGISTRATION_WAIT_S, attempts: int = 3) -> bool:
    """A fresh 127.0.0.1 listener accepts a connection after *wait_s*."""
    srv = socket.socket()
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(attempts)
        port = srv.getsockname()[1]
        threading.Thread(target=lambda: [srv.accept()[0].close() for _ in range(attempts)],
                         daemon=True).start()
        time.sleep(wait_s)
        for _ in range(attempts):
            with socket.create_connection(("127.0.0.1", port), timeout=3):
                pass
        return True
    except OSError:
        return False
    finally:
        srv.close()


def check(repo: Path, *, uid: Optional[int] = None, route_table: Optional[str] = None,
          module_files: Optional[dict[str, str]] = None,
          loopback: Callable[[], bool] = loopback_reachable) -> tuple[list[str], dict]:
    """Refusals and facts for this host; the defaults read the real host."""
    import hermes_cli
    import olo
    uid = os.geteuid() if uid is None else uid
    if route_table is None:
        route_table = Path("/proc/net/route").read_text(encoding="utf-8")
    files = module_files or {"olo": olo.__file__, "hermes_cli": hermes_cli.__file__}
    refusals: list[str] = []
    facts = {"uid": uid, "default_route": _default_route(route_table),
             "modules": {k: str(v) for k, v in files.items()}}
    if uid == 0:
        refusals.append("running as root: pg0 refuses root; run as the acceptance user (e.g. olo_hs)")
    if not facts["default_route"]:
        refusals.append("no default route: this is a private network namespace, and Google "
                        "would be unreachable; run in WSL's default namespace")
    for name, path in files.items():
        if not Path(path).resolve().is_relative_to(repo.resolve()):
            refusals.append(f"{name} is imported from {path}, not this checkout; "
                            f"set PYTHONPATH={repo}")
    facts["loopback_after_registration_wait"] = loopback()
    if not facts["loopback_after_registration_wait"]:
        refusals.append(f"a 127.0.0.1 listener is still refused after {REGISTRATION_WAIT_S}s")
    return refusals, facts
