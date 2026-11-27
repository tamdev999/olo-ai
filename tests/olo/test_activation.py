"""tests/olo/test_activation.py — S5 part 3: explicit, fail-closed stage activation.

The commitment and projection stages stay off unless enabled for one named
business, with credentials, budgets, bank routing and trusted identity all
present.  Every refusal happens before any signal is fetched and leaves the
database unbound.  No network: Hindsight clients are only constructed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from olo.activation import ActivationError, ActivationRequest, activate, load_key_file
from olo.store import connect, migrate

pytestmark = pytest.mark.not_integration

NOW = "2026-05-01T12:00:00.000000Z"
FAKE_KEY = "sk-fake-activation-0123456789"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "olo.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture()
def registry(tmp_path) -> Path:
    reg = tmp_path / "hindsight_port_registry.json"
    reg.write_text(json.dumps({"biz": {"port": 18800, "bank_id": "olo-biz"},
                               "other": {"port": 18801, "bank_id": "olo-other"}}))
    for business in ("biz", "other"):
        env = tmp_path / "businesses" / business / ".env"
        env.parent.mkdir(parents=True)
        env.write_text(f"HINDSIGHT_API_TENANT_EXTENSION_API_KEY=tenant-{business}-key\n")
    return reg


def _model_factory(**kw):
    def fn(prompt):
        raise AssertionError("no model call during activation")
    fn.model_id = "fake"
    return fn


def _req(registry: Path | None = None, **kw) -> ActivationRequest:
    base = dict(business_id="biz", enable_commitments=True, enable_projection=True,
                bank_id="olo-biz", extract_max_calls=50, projection_max_docs=100,
                projection_max_prose_docs=20, projection_budget_usd=1.0,
                own_emails=("owner@biz.example",),
                hindsight_registry=str(registry) if registry else None)
    base.update(kw)
    return ActivationRequest(**base)


def _activate(conn, req, **env):
    return activate(req, conn, NOW, environ={"DEEPSEEK_API_KEY": FAKE_KEY, **env},
                    model_factory=_model_factory)


def _bindings(conn) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT name, value FROM olo_bindings")}


class TestDisabledByDefault:
    def test_no_flags_means_no_stages_and_no_writes(self, conn) -> None:
        act = activate(ActivationRequest(), conn, NOW, environ={})
        assert act.commitment is None and act.projection is None
        assert _bindings(conn) == {}

    def test_runner_main_without_flags_runs_no_stages(self, monkeypatch) -> None:
        from olo import runner
        seen = {}

        def fake_run_source(adapter, **kw):
            seen.update(kw)
            raise RuntimeError("stop here")
        monkeypatch.setattr(runner, "run_source", fake_run_source)
        assert runner.main(["--source", "synthetic"]) == 1
        assert seen["commitment"] is None and seen["projection"] is None


class TestValidActivation:
    def test_builds_both_stages(self, conn, registry) -> None:
        act = _activate(conn, _req(registry))
        assert act.commitment.budget.max_calls == 50
        assert act.projection.bank_id == "olo-biz"
        assert act.projection.limits.max_docs_per_run == 100
        assert act.projection.limits.max_prose_docs_per_run == 20
        assert type(act.projection.usage_guard).__name__ == "UsageGuard"
        assert act.projection.await_commitment is True
        assert ("email", "owner@biz.example") in act.own_identifiers
        assert _bindings(conn) == {"business_id": "biz"}

    def test_key_file_is_reused(self, conn, registry, tmp_path) -> None:
        keyfile = tmp_path / "deepseek.env"
        keyfile.write_text(f"HINDSIGHT_API_LLM_API_KEY={FAKE_KEY}\n")
        os.chmod(keyfile, 0o600)
        seen = {}

        def factory(**kw):
            seen.update(kw)
            return _model_factory()
        activate(_req(registry, deepseek_key_file=str(keyfile)), conn, NOW,
                 environ={}, model_factory=factory)
        assert seen["api_key"] == FAKE_KEY

    def test_projection_alone_does_not_await_commitments(self, conn, registry) -> None:
        act = _activate(conn, _req(registry, enable_commitments=False))
        assert act.commitment is None and act.projection.await_commitment is False


class TestRefusals:
    @pytest.mark.parametrize("change,needle", [
        ({"business_id": None}, "--business-id"),
        ({"business_id": "Biz Name!"}, "--business-id"),
        ({"bank_id": None}, "--bank-id"),
        ({"extract_max_calls": None}, "--extract-max-calls"),
        ({"extract_max_calls": -1}, "--extract-max-calls"),
        ({"projection_max_docs": None}, "--projection-max-docs"),
        ({"projection_max_prose_docs": -1}, "--projection-max-prose-docs"),
        ({"projection_budget_usd": None}, "--projection-budget-usd"),
        ({"bank_id": "olo-other"}, "registry routes"),
        ({"own_emails": ()}, "trusted business identity"),
    ])
    def test_refused_and_nothing_bound(self, conn, registry, change, needle) -> None:
        with pytest.raises(ActivationError, match=needle.replace("-", r"\-")):
            _activate(conn, _req(registry, **change))
        assert _bindings(conn) == {}

    def test_zero_calls_is_a_no_model_mode(self, conn, registry) -> None:
        """--extract-max-calls 0: calendar commitments only.  No key is read,
        no model is built (the factory would raise) and none can be called."""
        def factory(**kw):
            raise AssertionError("no model may be built in no-model mode")
        act = activate(_req(registry, extract_max_calls=0, projection_max_prose_docs=0),
                       conn, NOW, environ={}, model_factory=factory)
        assert act.commitment.budget.max_calls == 0
        assert act.commitment.budget.call_refusal(1) is not None
        with pytest.raises(RuntimeError, match="extraction is disabled"):
            act.commitment.model_fn("prompt")
        assert act.projection.limits.max_prose_docs_per_run == 0

    def test_missing_deepseek_key(self, conn, registry) -> None:
        with pytest.raises(ActivationError, match="DeepSeek key"):
            activate(_req(registry), conn, NOW, environ={}, model_factory=_model_factory)
        assert _bindings(conn) == {}

    def test_missing_registry(self, conn) -> None:
        with pytest.raises(ActivationError, match="registry"):
            _activate(conn, _req(None))

    def test_missing_tenant_key(self, conn, registry) -> None:
        (registry.parent / "businesses" / "biz" / ".env").write_text("")
        with pytest.raises(ActivationError, match="tenant API key"):
            _activate(conn, _req(registry))
        assert _bindings(conn) == {}

    def test_learned_identity_is_accepted(self, conn, registry, monkeypatch) -> None:
        from olo.adapters import contacts
        monkeypatch.setattr(contacts, "load_own_identifiers",
                            lambda c: frozenset({("email", "owner@biz.example")}))
        act = _activate(conn, _req(registry, own_emails=()))
        assert act.own_identifiers == {("email", "owner@biz.example")}

    @pytest.mark.platforms("posix")
    def test_group_readable_key_file_refused(self, conn, registry, tmp_path) -> None:
        keyfile = tmp_path / "deepseek.env"
        keyfile.write_text(f"DEEPSEEK_API_KEY={FAKE_KEY}\n")
        os.chmod(keyfile, 0o644)
        with pytest.raises(ActivationError, match="owner-only") as exc:
            _activate(conn, _req(registry, deepseek_key_file=str(keyfile)))
        assert FAKE_KEY not in str(exc.value)

    def test_runner_main_refuses_before_running(self, monkeypatch, capsys) -> None:
        from olo import runner
        called = []
        monkeypatch.setattr(runner, "run_source", lambda *a, **k: called.append(1))
        rc = runner.main(["--source", "synthetic", "--enable-commitments"])
        assert rc == 1 and called == []
        assert "activation refused" in capsys.readouterr().err


class TestIsolation:
    def test_database_stays_bound_to_its_business(self, conn, registry) -> None:
        _activate(conn, _req(registry))
        with pytest.raises(ActivationError, match="belongs to business 'biz'"):
            _activate(conn, _req(registry, business_id="other", bank_id="olo-other"))

    def test_database_stays_bound_to_its_bank(self, conn, registry) -> None:
        conn.execute("INSERT INTO olo_bindings VALUES ('hindsight_bank_id','olo-biz',?)", (NOW,))
        reg = json.loads(registry.read_text())
        reg["biz"]["bank_id"] = "olo-biz-v2"
        registry.write_text(json.dumps(reg))
        with pytest.raises(ActivationError, match="bound to bank 'olo-biz'"):
            _activate(conn, _req(registry, bank_id="olo-biz-v2"))

    def test_key_file_loader_never_echoes_key(self, tmp_path) -> None:
        keyfile = tmp_path / "k.env"
        keyfile.write_text(f"OTHER={FAKE_KEY}\n")
        os.chmod(keyfile, 0o600)
        with pytest.raises(ActivationError) as exc:
            load_key_file(str(keyfile))
        assert FAKE_KEY not in str(exc.value)
