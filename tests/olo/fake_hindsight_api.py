"""A stand-in `hindsight-api` executable for supervisor tests.

Started the way olo.hindsight_service starts Hindsight (`--host 127.0.0.1
--port N`, cwd = the business home), it reads ``./.env`` like the real
server: ``HINDSIGHT_API_TENANT_API_KEY`` is enforced on every
``/v1/default/banks/...`` request, ``/health`` is open.  ``FAKE_TENANT_AUTH=off``
in ``.env`` serves without authentication (a misconfigured server).
``FAKE_PG0=on`` starts an embedded database the way Hindsight's pg0 does:
detached (it outlives this server), under ``./.pg0/instances/fake/data``, and
reused by the next server while it runs (tests/olo/fake_pg0.py).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _ensure_database() -> None:
    data = Path(".pg0/instances/fake/data").resolve()
    pidfile = data / "postmaster.pid"
    if pidfile.exists():
        pid = int(pidfile.read_text(encoding="utf-8").partition("\n")[0])
        try:
            if Path(f"/proc/{pid}/cwd").resolve() == data:
                return                                  # running: reuse it
        except OSError:
            pass
    subprocess.Popen([sys.executable, str(Path(__file__).with_name("fake_pg0.py")), str(data)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, env=dict(os.environ))


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            out[name.strip()] = value.strip()
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    env = _env()
    key = env.get("HINDSIGHT_API_TENANT_API_KEY", "")
    auth = env.get("FAKE_TENANT_AUTH", "on") != "off"
    if env.get("FAKE_PG0") == "on":
        _ensure_database()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, status: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "healthy"})
            elif auth and self.headers.get("Authorization") != f"Bearer {key}":
                self._send(401, {"detail": "unauthorized"})
            else:
                self._send(404, {"detail": "bank not found"})

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
