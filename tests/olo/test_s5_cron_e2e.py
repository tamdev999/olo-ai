"""The installed S5 pipeline end to end: `hermes olo setup` writes the shim,
`hermes cron` runs it, and Gmail → SQLite → Rolodex → extraction → ledger →
Hindsight projection happens with no test-only launcher.

Real Hermes CLI subprocesses against a temporary HERMES_HOME; Google, DeepSeek
and Hindsight are loopback fakes (tests/olo/fake_google.py,
tests/olo/fake_s5_services.py).  No network, no paid calls.

Also proves the post-audit operational contract on that same installed path:
* disabled by default — a plain `setup` writes no S5 argument;
* a degraded run (extraction errors, Hindsight down) is not reported as a
  success, and `hermes olo status` shows why;
* a failed Google fetch still lets already-ingested signals reach the ledger.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.olo.fake_google import FakeGoogleServer
from tests.olo.fake_s5_services import FakeDeepSeek, FakeHindsight
from tests.olo.test_google_cron_e2e import (
    _cron_run_and_wait,
    _enable_plugin,
    _install_plugin,
    _parse_job_id,
    _repo_root,
    _run,
    _save_creds,
)

pytestmark = pytest.mark.not_integration

BIZ, BANK, TENANT_KEY = "biz", "olo-biz", "tenant-key-for-tests-only"
OWNER = "test@example.com"                 # the connected account (FakeGoogleServer email)
PASSAGE = "ORD-501 delivery confirmed for 2026-06-10"
ROUTES = {PASSAGE: {"event_type": "delivery", "action": "commit", "business_key": "ORD-501",
                    "confirmed_date": "2026-06-10", "proposed_date": None,
                    "prior_confirmed_date": None, "supporting_passage": PASSAGE,
                    "confidence": 0.95}}


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "hermes_home"
    h.mkdir()
    _install_plugin(h, _repo_root())
    _enable_plugin(h)
    return h


def _business(home: Path, hindsight: FakeHindsight, tmp_path: Path) -> tuple[Path, Path]:
    """What setup.sh leaves for a business: port registry and tenant key; plus
    an owner-only DeepSeek key file."""
    registry = home / "hindsight_port_registry.json"
    registry.write_text(json.dumps({BIZ: {"port": hindsight.port, "bank_id": BANK}}), encoding="utf-8")
    env = home / "businesses" / BIZ / ".env"
    env.parent.mkdir(parents=True)
    env.write_text(f"HINDSIGHT_API_TENANT_API_KEY={TENANT_KEY}\n", encoding="utf-8")
    key = tmp_path / "deepseek.env"
    key.write_text("DEEPSEEK_API_KEY=sk-test-not-a-real-key\n", encoding="utf-8")
    key.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return registry, key


def _setup_s5(home: Path, registry: Path, key: Path, deepseek: FakeDeepSeek, *extra: str):
    return _run(["olo", "setup", "--source", "gmail", "--option", "account=acme",
                 "--enable-commitments", "--enable-projection", "--business-id", BIZ,
                 "--bank-id", BANK, "--deepseek-key-file", str(key),
                 "--deepseek-base-url", deepseek.base_url, "--extract-max-calls", "5",
                 "--projection-max-docs", "20", "--projection-max-prose-docs", "8",
                 "--projection-budget-usd", "1.0", "--own-email", OWNER,
                 "--hindsight-registry", str(registry), *extra], home)


def _cron(home: Path) -> str:
    r = _run(["cron", "create", "every 5m", "--name", "olo-gmail-acme",
              "--script", "olo_monitor_gmail_acme.py", "--no-agent", "--deliver", "local"], home)
    assert r.returncode == 0, r.stderr
    job = _parse_job_id(r.stdout)
    assert job
    return job


def _db(home: Path):
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    try:
        from olo.paths import db_path
        from olo.store import connect
        return connect(db_path())
    finally:
        if old is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old


def _health(home: Path) -> dict[str, str]:
    conn = _db(home)
    rows = conn.execute("SELECT stage, status FROM pipeline_health WHERE source='gmail_acme'").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def _sent(fake: FakeGoogleServer, msg_id: str, subject: str) -> None:
    fake.gmail.add_message(msg_id, labels=["SENT"], subject=subject, from_=OWNER,
                           to="Alice Customer <alice@customer.test>")


def test_installed_scheduler_runs_the_whole_pipeline(home: Path, tmp_path: Path) -> None:
    with FakeGoogleServer(email=OWNER) as google, FakeDeepSeek(ROUTES) as deepseek, \
            FakeHindsight(TENANT_KEY) as hindsight:
        _save_creds(google, "acme", home)
        registry, key = _business(home, hindsight, tmp_path)
        r = _setup_s5(home, registry, key, deepseek)
        assert r.returncode == 0, r.stderr

        shim = (home / "scripts" / "olo_monitor_gmail_acme.py").read_text(encoding="utf-8")
        for flag in ("--enable-commitments", "--enable-projection", "--business-id", BANK,
                     "--deepseek-key-file", str(key), deepseek.base_url):
            assert repr(flag) in shim                     # argv literals are repr()-quoted
        assert "sk-test-not-a-real-key" not in shim and TENANT_KEY not in shim

        job = _cron(home)
        _cron_run_and_wait(home, job, expect_status="completed")          # bootstrap
        _sent(google, "m1", PASSAGE)
        _cron_run_and_wait(home, job, expect_status="completed")

        conn = _db(home)
        [(cid, key_, contact)] = conn.execute(
            "SELECT id, business_key, contact_id FROM commitments").fetchall()
        version = conn.execute(
            "SELECT action, actor_role, confirmed_date, is_confirmed FROM commitment_versions"
            " WHERE commitment_id=?", (cid,)).fetchone()
        conn.close()
        assert key_ == "ORD-501" and tuple(version) == ("commit", "business", "2026-06-10", 1)
        assert {"sig:c:m1", f"ledger:{cid}:v1"} <= set(hindsight.docs)
        assert "ledger:current" in hindsight.docs[f"ledger:{cid}:v1"]["tags"]
        assert hindsight.docs["sig:c:m1"]["tags"].count(f"ent:{contact}") == 1
        assert hindsight.unknown_strategies == []
        assert deepseek.requests and all(
            q["model"] == "deepseek-flash" and q["thinking"] == {"type": "disabled"}
            for q in deepseek.requests)
        assert _health(home) == {"ingest": "ok", "identity": "ok", "commitment": "ok",
                                 "projection": "ok", "operator": "ok"}

        status = _run(["olo", "status"], home)
        assert status.returncode == 0, status.stderr
        assert "Pipeline health:" in status.stdout and "Attention: none" in status.stdout
        assert hindsight.errors == [], hindsight.errors


def test_degradation_is_not_success_and_failed_fetch_still_processes(home: Path, tmp_path: Path) -> None:
    with FakeGoogleServer(email=OWNER) as google, FakeDeepSeek(ROUTES) as deepseek, \
            FakeHindsight(TENANT_KEY) as hindsight:
        _save_creds(google, "acme", home)
        registry, key = _business(home, hindsight, tmp_path)
        assert _setup_s5(home, registry, key, deepseek).returncode == 0
        job = _cron(home)
        _cron_run_and_wait(home, job, expect_status="completed")          # bootstrap

        # 1. Extraction fails (DeepSeek 500): the run must not look successful.
        _sent(google, "m1", PASSAGE)
        deepseek.fail_status = 500
        _cron_run_and_wait(home, job, expect_status="failed")
        assert _health(home)["commitment"] == "degraded"
        conn = _db(home)
        assert conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 0
        conn.close()

        # 2. Google fails, DeepSeek is back: the ingested m1 still reaches the
        # ledger before the run is recorded as failed.
        deepseek.fail_status = None
        google.inject_500()
        _cron_run_and_wait(home, job, expect_status="failed")
        conn = _db(home)
        assert conn.execute("SELECT COUNT(*) FROM commitments").fetchone()[0] == 1
        conn.close()
        assert _health(home)["ingest"] == "failed"
        assert _health(home)["commitment"] == "ok"

        # 3. Hindsight is down: projection is degraded, the run fails, status says so.
        hindsight.down = True
        _cron_run_and_wait(home, job, expect_status="failed")
        assert _health(home)["projection"] == "degraded"
        status = _run(["olo", "status"], home)
        assert "[DEGRADED] gmail_acme/projection" in status.stdout
        assert "bank_unprepared" in status.stdout

        # 4. Everything back: success again, nothing waiting.
        hindsight.down = False
        _cron_run_and_wait(home, job, expect_status="completed")
        assert set(_health(home).values()) == {"ok"}
        assert "Attention: none" in _run(["olo", "status"], home).stdout
        assert hindsight.errors == [], hindsight.errors


def test_setup_keeps_s5_disabled_by_default_and_refuses_unsafe_options(home: Path, tmp_path: Path) -> None:
    with FakeGoogleServer(email=OWNER) as google, FakeDeepSeek(ROUTES) as deepseek, \
            FakeHindsight(TENANT_KEY) as hindsight:
        _save_creds(google, "acme", home)
        registry, key = _business(home, hindsight, tmp_path)

        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme",
                  "--business-id", BIZ], home)
        assert r.returncode == 1 and "without --enable-commitments" in r.stderr

        r = _setup_s5(home, registry, key, deepseek, "--force")
        assert r.returncode == 0
        bad = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme", "--force",
                    "--enable-commitments", "--business-id", BIZ, "--deepseek-key-file", str(key),
                    "--deepseek-base-url", "https://example.com/v1", "--extract-max-calls", "5",
                    "--own-email", OWNER], home)
        assert bad.returncode == 1 and "--deepseek-base-url" in bad.stderr

        r = _run(["olo", "setup", "--source", "gmail", "--option", "account=acme", "--force"], home)
        assert r.returncode == 0
        shim = (home / "scripts" / "olo_monitor_gmail_acme.py").read_text(encoding="utf-8")
        assert "--enable" not in shim and "--business-id" not in shim


@pytest.mark.skipif(sys.platform == "win32", reason="owner-only key file mode is POSIX")
def test_setup_refuses_a_group_readable_key_file(home: Path, tmp_path: Path) -> None:
    with FakeGoogleServer(email=OWNER) as google, FakeDeepSeek(ROUTES) as deepseek, \
            FakeHindsight(TENANT_KEY) as hindsight:
        _save_creds(google, "acme", home)
        registry, key = _business(home, hindsight, tmp_path)
        key.chmod(0o640)
        r = _setup_s5(home, registry, key, deepseek)
        assert r.returncode == 1 and "owner-only" in r.stderr


REQUEST = "Could we move the ORD-501 delivery from 2026-06-10 to 2026-06-15"
RESCUE_ROUTES = {**ROUTES, REQUEST: {
    "event_type": "delivery", "action": "request_change", "business_key": "ORD-501",
    "confirmed_date": None, "proposed_date": "2026-06-15", "prior_confirmed_date": "2026-06-10",
    "supporting_passage": REQUEST, "confidence": 0.95}}


def test_spam_rescue_reaches_sqlite_ledger_and_hindsight_through_the_shim(home: Path, tmp_path: Path) -> None:
    """Source → persistence → processing through the generated cron shim: the
    customer's change request lands in spam, is captured with its full
    content once the owner marks it "Not spam", and reaches the ledger as a
    pending (unconfirmed) customer request and Hindsight."""
    with FakeGoogleServer(email=OWNER) as google, FakeDeepSeek(RESCUE_ROUTES) as deepseek,             FakeHindsight(TENANT_KEY) as hindsight:
        _save_creds(google, "acme", home)
        registry, key = _business(home, hindsight, tmp_path)
        assert _setup_s5(home, registry, key, deepseek).returncode == 0
        job = _cron(home)
        _cron_run_and_wait(home, job, expect_status="completed")          # bootstrap
        _sent(google, "m1", PASSAGE)
        google.gmail.add_message("m2", labels=["SPAM"], subject=REQUEST + "?",
                                 from_="Alice Customer <alice@customer.test>", to=OWNER)
        _cron_run_and_wait(home, job, expect_status="completed")
        conn = _db(home)
        assert conn.execute("SELECT COUNT(*) FROM signals WHERE external_id='c:m2'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM commitment_versions").fetchone()[0] == 1
        conn.close()

        google.gmail.move_labels("m2", add=["INBOX"], remove=["SPAM"])      # "Not spam"
        _cron_run_and_wait(home, job, expect_status="completed")
        conn = _db(home)
        kinds = {r[0] for r in conn.execute(
            "SELECT kind FROM signals WHERE json_extract(payload,'$.message_id')='m2'")}
        versions = conn.execute(
            "SELECT version, actor_role, action, proposed_date, is_confirmed FROM commitment_versions"
            " ORDER BY version").fetchall()
        state = conn.execute("SELECT state FROM gmail_message_state WHERE message_id='m2'").fetchone()[0]
        conn.close()
        assert {"email.received", "email.content", "email.labels_removed"} <= kinds
        assert [tuple(v) for v in versions] == [
            (1, "business", "commit", None, 1),
            (2, "customer", "request_change", "2026-06-15", 0)]
        assert state == "active"
        assert "sig:c:m2" in hindsight.docs and hindsight.errors == []


def test_subprocesses_import_this_repository(tmp_path: Path) -> None:
    """A script run by path, the way cron runs a shim, resolves `olo` and
    `hermes_cli` to the code under test with the environment every Olo test
    inherits (the WSL test venv's editable install points at a stale copy)."""
    import subprocess
    repo = _repo_root().resolve()
    script = tmp_path / "which_code.py"
    script.write_text("import olo.runner, hermes_cli\n"
                      "print(olo.runner.__file__)\nprint(hermes_cli.__file__)\n", encoding="utf-8")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         env={**os.environ, "HERMES_HOME": str(tmp_path)},
                         cwd=str(tmp_path), check=True)
    lines = out.stdout.splitlines()
    assert len(lines) == 2, out.stdout
    for line in lines:
        assert Path(line).resolve().is_relative_to(repo), line
