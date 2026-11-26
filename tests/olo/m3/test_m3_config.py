"""Olo M3 — Configuration validity tests.

Validates that every HINDSIGHT_API_* variable in committed config files
exists in the pinned Hindsight v1.2.1 config.py. Unknown variables are
silently ignored by Hindsight, so a typo or invented variable never causes
an error — only a silent misconfiguration.

Also tests:
  - HindsightConfig rejects external hosts (security guard, C2 §4.1)
  - No embedding provider setting points off-host
  - client._request uses open_credentialed_url (not urlopen directly)

Opus audit correction P1-2: validate all HINDSIGHT_API_* keys are real.
Opus audit correction P1-6: host validation at construction time.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import urllib.request
from pathlib import Path

import pytest


# ── Path constants ────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_OLO_M3 = _REPO_ROOT / "olo" / "m3" / "hindsight"
_SETUP_SH = _OLO_M3 / "setup.sh"
_CONFIG_TEMPLATE = _OLO_M3 / "config_option_b.env.template"

# Pinned Hindsight v1.2.1 config.py (fetched on first run, cached in test)
_HINDSIGHT_CONFIG_URL = (
    "https://raw.githubusercontent.com/vectorize-io/hindsight"
    "/0eb02d8c418eac6b489bc63883c993b2257dfac4"
    "/hindsight-api-slim/hindsight_api/config.py"
)


def _fetch_hindsight_config_vars() -> frozenset[str]:
    """Return every ENV_* constant from Hindsight v1.2.1 config.py.

    These are the only valid HINDSIGHT_API_* variable names.

    Tries the local vendored copy first (avoids network dependency),
    then falls back to fetching from GitHub.
    """
    _VENDORED_PATH = _OLO_M3 / "hindsight_config_vendored.py"

    # Try local vendored file first
    if _VENDORED_PATH.exists():
        source = _VENDORED_PATH.read_text(encoding="utf-8")
        return frozenset(re.findall(r'"(HINDSIGHT_API_[A-Z0-9_]+)"', source))

    # Fall back to GitHub
    try:
        import urllib.request as _ur
        with _ur.urlopen(_HINDSIGHT_CONFIG_URL, timeout=10) as resp:
            source = resp.read().decode()
    except Exception as exc:
        pytest.skip(f"Cannot fetch Hindsight config.py for validation: {exc}")
        return frozenset()

    # Extract all string values assigned to ENV_* variables
    # Pattern: ENV_FOO = "HINDSIGHT_API_BAR"
    return frozenset(re.findall(r'"(HINDSIGHT_API_[A-Z0-9_]+)"', source))


def _extract_hindsight_vars_from_file(path: Path) -> list[tuple[str, int]]:
    """Return (var_name, line_number) for every HINDSIGHT_API_* in a file."""
    if not path.exists():
        return []
    result = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        # Match bare assignments: HINDSIGHT_API_FOO=... or HINDSIGHT_API_FOO =...
        m = re.match(r'\s*(HINDSIGHT_API_[A-Z0-9_]+)\s*=', line)
        if m and not line.lstrip().startswith('#'):
            result.append((m.group(1), lineno))
    return result


#: The pinned Hindsight venv the gate tests run (see test_m3_gate).
_HINDSIGHT_PYTHON = Path("/root/olo_m3_hindsight_venv/bin/python")

_TENANT_PROBE = """
import asyncio, json, os, sys
from importlib.metadata import version
from hindsight_api.extensions.loader import load_extension
from hindsight_api.extensions.tenant import AuthenticationError, TenantExtension
from hindsight_api.models import RequestContext

os.environ.update(json.loads(sys.argv[1]))
ext = load_extension("TENANT", TenantExtension)

async def outcome(key):
    try:
        await ext.authenticate(RequestContext(api_key=key))
        return "accepted"
    except AuthenticationError:
        return "rejected"

print(json.dumps({
    "version": version("hindsight-api-slim"),
    "loaded": f"{type(ext).__module__}:{type(ext).__name__}" if ext else None,
    "right": asyncio.run(outcome(os.environ["HINDSIGHT_API_TENANT_API_KEY"])),
    "wrong": asyncio.run(outcome("another-business-key")),
}))
"""


def _proven_tenant_extension_vars() -> frozenset[str]:
    """The tenant-extension variables setup.sh sets, proven live in pinned 1.2.1.

    Hindsight's extension loader reads ``HINDSIGHT_API_<PREFIX>_EXTENSION`` and
    hands every ``HINDSIGHT_API_<PREFIX>_*`` to that extension as its config, so
    these names never appear in config.py. Instead of trusting the names, run
    the pinned server's loader with setup.sh's extension: it must load, accept
    the configured key and reject another business's key (C4).
    """
    if not _HINDSIGHT_PYTHON.exists():
        pytest.skip(f"pinned Hindsight venv not present: {_HINDSIGHT_PYTHON}")
    m = re.search(r'^TENANT_EXTENSION="([^"]+)"', _SETUP_SH.read_text(encoding="utf-8"), re.M)
    assert m, "setup.sh no longer names its tenant extension"
    env = {"HINDSIGHT_API_TENANT_EXTENSION": m.group(1),
           "HINDSIGHT_API_TENANT_API_KEY": "probe-business-key"}
    proc = subprocess.run([str(_HINDSIGHT_PYTHON), "-c", _TENANT_PROBE, json.dumps(env)],
                          capture_output=True, text=True, timeout=120,
                          env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"}, cwd="/tmp")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "version": "1.2.1", "loaded": m.group(1), "right": "accepted", "wrong": "rejected"}
    return frozenset(env)


# ── Config file validity tests ────────────────────────────────────────────────

@pytest.mark.integration
class TestConfigValidity:
    """Every HINDSIGHT_API_* var in committed files must exist in v1.2.1."""

    def test_setup_sh_vars_are_valid(self) -> None:
        """setup.sh must only use variables that v1.2.1 reads: config.py's, plus
        the tenant extension's, proven against the pinned server's loader."""
        valid = _fetch_hindsight_config_vars() | _proven_tenant_extension_vars()
        found = _extract_hindsight_vars_from_file(_SETUP_SH)
        unknown = [(v, ln) for v, ln in found if v not in valid]
        assert not unknown, (
            f"setup.sh contains unknown HINDSIGHT_API_* vars "
            f"(silently ignored by Hindsight — likely misconfiguration):\n"
            + "\n".join(f"  line {ln}: {v}" for v, ln in unknown)
        )

    def test_config_template_vars_are_valid(self) -> None:
        """config_option_b.env.template must only use v1.2.1 config vars."""
        valid = _fetch_hindsight_config_vars()
        found = _extract_hindsight_vars_from_file(_CONFIG_TEMPLATE)
        unknown = [(v, ln) for v, ln in found if v not in valid]
        assert not unknown, (
            f"config_option_b.env.template contains unknown HINDSIGHT_API_* vars:\n"
            + "\n".join(f"  line {ln}: {v}" for v, ln in unknown)
        )

    def test_no_embeddings_external_url(self) -> None:
        """No config file should point embeddings to an external URL.

        A base URL for embeddings must be absent (ONNX = local) or point
        to 127.0.0.1/localhost only. This prevents the DeepSeek key from
        accidentally being sent to an embedding endpoint.
        """
        external_url_pattern = re.compile(r'HINDSIGHT_API_EMBEDDINGS.*BASE_URL\s*=\s*https?://')
        for path in (_SETUP_SH, _CONFIG_TEMPLATE):
            if not path.exists():
                continue
            content = path.read_text()
            matches = external_url_pattern.findall(content)
            assert not matches, (
                f"{path.name} sets an external embedding URL: {matches}. "
                "ONNX embeddings are local — no network egress is correct."
            )

    def test_removed_invalid_vars_absent(self) -> None:
        """Variables known to be invalid in v1.2.1 must not appear in config files."""
        # These were in the original config and are silently ignored by v1.2.1.
        invalid_vars = {
            "HINDSIGHT_API_DATABASE_DATA_DIR",
            "HINDSIGHT_API_DEFAULT_BANK_ID",
            "HINDSIGHT_API_EMBED_PROVIDER",
            "HINDSIGHT_API_EMBEDDINGS_BASE_URL",
            "HINDSIGHT_API_TELEMETRY_ENABLED",
            "HINDSIGHT_API_CLOUD_ENABLED",
            "HINDSIGHT_API_WEBHOOKS_ENABLED",
        }
        for path in (_SETUP_SH, _CONFIG_TEMPLATE):
            if not path.exists():
                continue
            found = {v for v, _ in _extract_hindsight_vars_from_file(path)}
            present = invalid_vars & found
            assert not present, (
                f"{path.name} still contains invalid/ignored v1.2.1 vars: {present}"
            )


# ── Fork pin tests ────────────────────────────────────────────────────────────

class TestForkPin:
    """setup.sh must install from the correct fork and pinned SHA."""

    def test_fork_url_is_olo_persistentmemory(self) -> None:
        """Fork must be tamdev999/olo-persistentmemory, not upstream or olo-hindsight."""
        if not _SETUP_SH.exists():
            pytest.skip("setup.sh not found")
        content = _SETUP_SH.read_text()
        assert "tamdev999/olo-persistentmemory" in content, (
            "setup.sh must install from tamdev999/olo-persistentmemory "
            "(not upstream vectorize-io/hindsight or tamdev999/olo-hindsight)"
        )

    def test_no_upstream_fallback(self) -> None:
        """setup.sh must not fall back to the upstream vectorize-io repository."""
        if not _SETUP_SH.exists():
            pytest.skip("setup.sh not found")
        content = _SETUP_SH.read_text()
        assert "vectorize-io/hindsight" not in content, (
            "setup.sh must not reference the upstream vectorize-io/hindsight repo. "
            "Remove the upstream fallback; the fork is the only allowed install source."
        )

    def test_sha_is_pinned(self) -> None:
        """The correct commit SHA must be pinned in setup.sh."""
        expected_sha = "0eb02d8c418eac6b489bc63883c993b2257dfac4"
        if not _SETUP_SH.exists():
            pytest.skip("setup.sh not found")
        content = _SETUP_SH.read_text()
        assert expected_sha in content, (
            f"setup.sh must pin to {expected_sha} (Hindsight v1.2.1)"
        )


# ── Security guard tests ──────────────────────────────────────────────────────

class TestSecurityGuard:
    """HindsightConfig must reject non-loopback hosts (C2, §4.1 fix)."""

    def test_external_host_raises(self) -> None:
        """Construction with an external host must raise ValueError."""
        from olo.m3.hindsight.client import HindsightConfig
        with pytest.raises(ValueError, match="loopback"):
            HindsightConfig(host="api.example.com")

    def test_external_ip_raises(self) -> None:
        """Any non-loopback IP must raise ValueError."""
        from olo.m3.hindsight.client import HindsightConfig
        with pytest.raises(ValueError, match="loopback"):
            HindsightConfig(host="192.168.1.100")

    def test_loopback_accepted(self) -> None:
        """127.0.0.1 and localhost must be accepted without error."""
        from olo.m3.hindsight.client import HindsightConfig
        cfg1 = HindsightConfig(host="127.0.0.1")
        assert cfg1.host == "127.0.0.1"
        cfg2 = HindsightConfig(host="localhost")
        assert cfg2.host == "localhost"

    def test_client_uses_open_credentialed_url_not_urlopen(self) -> None:
        """client.py must use open_credentialed_url, not urllib.request.urlopen."""
        client_path = _REPO_ROOT / "olo" / "m3" / "hindsight" / "client.py"
        if not client_path.exists():
            pytest.skip("client.py not found")
        source = client_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(client_path))
        except SyntaxError:
            pytest.skip("client.py has syntax error")

        # Must NOT have direct urlopen calls
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "urlopen":
                    pytest.fail(
                        f"client.py:{node.lineno} uses urllib.request.urlopen directly. "
                        "Must use open_credentialed_url (C2 §4.1)."
                    )

        # Must reference open_credentialed_url
        assert "open_credentialed_url" in source, (
            "client.py must import and use open_credentialed_url (C2 §4.1)"
        )

    def test_no_external_host_negative_control(self) -> None:
        """Negative control: HindsightConfig('example.com') must not succeed.

        This test proves the guard is active: if someone removes the check,
        this test fails rather than silently allowing external hosts.
        """
        from olo.m3.hindsight.client import HindsightConfig
        external = "api.deepseek.com"
        try:
            cfg = HindsightConfig(host=external)
            # If we get here, the guard is not enforced
            pytest.fail(
                f"HindsightConfig(host={external!r}) should have raised ValueError "
                "but did not — the host validation guard is missing."
            )
        except ValueError:
            pass  # expected — guard is working
