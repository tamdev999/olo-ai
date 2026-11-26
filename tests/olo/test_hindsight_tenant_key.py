"""tests/olo/test_hindsight_tenant_key.py — the client presents the key the
Hindsight server enforces.

Hindsight 1.2.1's ApiKeyTenantExtension checks HINDSIGHT_API_TENANT_API_KEY.
setup.sh writes that variable; businesses set up before the fix have their
key under HINDSIGHT_API_TENANT_EXTENSION_API_KEY, and setup.sh re-run carries
it over, so the client accepts either and prefers the enforced name.
"""
from __future__ import annotations

import json

import pytest

from olo.m3.hindsight.client import HindsightConfig

pytestmark = pytest.mark.not_integration


def _business(tmp_path, env: str) -> str:
    (tmp_path / "hindsight_port_registry.json").write_text(
        json.dumps({"biz": {"port": 18801, "bank_id": "olo-biz"}}))
    home = tmp_path / "businesses" / "biz"
    home.mkdir(parents=True)
    (home / ".env").write_text(env)
    return str(tmp_path / "hindsight_port_registry.json")


@pytest.mark.parametrize("env, key", [
    ("HINDSIGHT_API_TENANT_EXTENSION=x:Y\nHINDSIGHT_API_TENANT_API_KEY=new-key\n", "new-key"),
    ("HINDSIGHT_API_TENANT_EXTENSION_API_KEY=old-key\n", "old-key"),
    ("HINDSIGHT_API_TENANT_EXTENSION_API_KEY=old-key\nHINDSIGHT_API_TENANT_API_KEY=new-key\n",
     "new-key"),
    ("HINDSIGHT_API_PORT=18801\n", ""),
])
def test_business_key_is_loaded(tmp_path, env, key) -> None:
    cfg = HindsightConfig.for_business("biz", registry_path=_business(tmp_path, env))
    assert (cfg.api_key, cfg.port, cfg.bank_id) == (key, 18801, "olo-biz")
