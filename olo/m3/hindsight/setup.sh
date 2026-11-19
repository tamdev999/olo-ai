#!/usr/bin/env bash
# Olo M3: Hindsight v1.2.1 per-business environment setup.
#
# Each business gets its own named pg0 instance and isolated HOME directory,
# so pg0 stores its data under <BUSINESS_HOME>/.pg0/olo-<BUSINESS_ID>/.
# Two businesses on the same host cannot share memory (P0-1 isolation).
#
# pg0 uses initdb internally.  Run this script as a non-root user (e.g. olo_hs)
# because initdb refuses to run as root.
#
# Usage:
#   bash setup.sh <BUSINESS_ID> [--provider none|deepseek]
#
# Environment variables consumed:
#   HERMES_HOME               — the platform root (required)
#   HINDSIGHT_API_LLM_API_KEY — DeepSeek key (required for provider=deepseek)
#   HINDSIGHT_PORT            — override default port (default: 18800)
#   HF_HOME                   — HuggingFace model cache (optional override)
#
# Isolation contract:
#   Each business process runs with HOME=<BUSINESS_HOME>.
#   pg0 stores data at <BUSINESS_HOME>/.pg0/olo-<BUSINESS_ID>/ (pg0 default).
#   The database URL is pg0://olo-<BUSINESS_ID> (named instance).
#   No DATABASE_DATA_DIR override: pg0 uses HOME, not an env var.
#
# Port registry:
#   Each call registers {BUSINESS_ID: {port, bank_id}} in
#   ${HERMES_HOME}/hindsight_port_registry.json so HindsightConfig.for_business()
#   can resolve the correct endpoint.  (Audit D9 fix.)
#
# Provenance:
#   The venv must have hindsight-api-slim installed from the fork git URL with
#   the pinned SHA.  setup.sh verifies direct_url.json and fails if the URL is
#   the upstream repo or a package index.  (Audit D8 fix.)
#
# Opus architecture: §B.2, §B.9; revised spec §4 + audit corrections.
# Hindsight version: v1.2.1 commit 0eb02d8c418eac6b489bc63883c993b2257dfac4

set -euo pipefail

BUSINESS_ID="${1:-}"
PROVIDER="${2:---provider=none}"
PROVIDER="${PROVIDER#--provider=}"

# ── Validate inputs ──────────────────────────────────────────────────────────
if [[ -z "$BUSINESS_ID" ]]; then
    echo "Usage: bash setup.sh <BUSINESS_ID> [--provider none|deepseek]" >&2
    exit 1
fi

if [[ -z "${HERMES_HOME:-}" ]]; then
    echo "ERROR: HERMES_HOME is not set" >&2
    exit 1
fi

# Validate BUSINESS_ID: alphanumeric + hyphens only
if [[ ! "$BUSINESS_ID" =~ ^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$ ]]; then
    echo "ERROR: BUSINESS_ID must be lowercase alphanumeric + hyphens: ${BUSINESS_ID}" >&2
    exit 1
fi

# Per-business HOME — pg0 stores data under HOME/.pg0/olo-<BUSINESS_ID>/
BUSINESS_HOME="${HERMES_HOME}/businesses/${BUSINESS_ID}"
VENV="${HERMES_HOME}/olo/hindsight-venv"
PORT="${HINDSIGHT_PORT:-18800}"

# pg0 instance name: olo-<BUSINESS_ID> → stored at $HOME/.pg0/olo-<BUSINESS_ID>/
PG0_INSTANCE="olo-${BUSINESS_ID}"
# Database URL uses named pg0 instance for per-business isolation.
# NO override accepted — shared Postgres bypasses the isolation being tested.
DATABASE_URL="pg0://${PG0_INSTANCE}"

# ── Pinned fork ──────────────────────────────────────────────────────────────
# Fork: tamdev999/olo-persistentmemory at v1.2.1
# No upstream fallback — fork identity is part of the supply-chain guarantee.
FORK_URL="https://github.com/tamdev999/olo-persistentmemory.git"
PINNED_SHA="0eb02d8c418eac6b489bc63883c993b2257dfac4"
FORK_INSTALL="hindsight-api-slim @ git+${FORK_URL}@${PINNED_SHA}#subdirectory=hindsight-api-slim"

# ── Create per-business HOME ─────────────────────────────────────────────────
mkdir -p "${BUSINESS_HOME}"
chmod 700 "${BUSINESS_HOME}"

# ── Create venv (shared across businesses; pinned) ───────────────────────────
# Use HINDSIGHT_PYTHON if set, otherwise try python3.14, then fall back to python3.
_PYTHON="${HINDSIGHT_PYTHON:-}"
if [[ -z "$_PYTHON" ]]; then
    if command -v python3.14 &>/dev/null; then
        _PYTHON="python3.14"
    else
        _PYTHON="python3"
    fi
fi

if [[ ! -d "$VENV" ]]; then
    echo "[setup] Creating Hindsight venv at $VENV (using $_PYTHON)"
    "$_PYTHON" -m venv "$VENV"
fi

# ── Install hindsight-api-slim from pinned fork SHA ──────────────────────────
if HOME="${BUSINESS_HOME}" "$VENV/bin/pip" show hindsight-api-slim &>/dev/null; then
    INSTALLED=$(HOME="${BUSINESS_HOME}" "$VENV/bin/pip" show hindsight-api-slim | grep Version | awk '{print $2}')
    if [[ "$INSTALLED" == "1.2.1" ]]; then
        echo "[setup] hindsight-api-slim 1.2.1 already installed — checking provenance…"
    else
        echo "[setup] Upgrading hindsight-api-slim from $INSTALLED to 1.2.1"
        HOME="${BUSINESS_HOME}" "$VENV/bin/pip" install "${FORK_INSTALL}" --quiet
    fi
else
    echo "[setup] Installing hindsight-api-slim from ${FORK_URL:0:40}… SHA ${PINNED_SHA:0:12}"
    HOME="${BUSINESS_HOME}" "$VENV/bin/pip" install "${FORK_INSTALL}" --quiet
fi

# ── Verify fork provenance (D8 fix) ─────────────────────────────────────────
# After install, direct_url.json must exist and point to the fork, not upstream
# or a package index.  Fail loudly if provenance cannot be verified.
DIST_INFO=$("$VENV/bin/python3" -c "
import importlib.metadata, pathlib, json, sys
try:
    dist = importlib.metadata.Distribution.from_name('hindsight-api-slim')
    du = dist._path / 'direct_url.json'
    if not du.exists():
        print('MISSING')
        sys.exit(0)
    d = json.loads(du.read_text())
    url = d.get('url','')
    vcs = d.get('vcs_info',{})
    commit = vcs.get('commit_id','')
    print(f'URL={url}')
    print(f'COMMIT={commit}')
except Exception as e:
    print(f'ERROR={e}')
" 2>&1)

if echo "$DIST_INFO" | grep -q "MISSING"; then
    echo "ERROR: direct_url.json missing — hindsight-api-slim was installed from PyPI," >&2
    echo "       not from the fork.  Re-install with: pip install '${FORK_INSTALL}'" >&2
    exit 1
fi

ACTUAL_URL=$(echo "$DIST_INFO" | grep '^URL=' | cut -d= -f2-)
ACTUAL_COMMIT=$(echo "$DIST_INFO" | grep '^COMMIT=' | cut -d= -f2-)

if [[ "$ACTUAL_URL" != "$FORK_URL" ]]; then
    echo "ERROR: direct_url.json URL is '${ACTUAL_URL}', expected '${FORK_URL}'" >&2
    echo "       Reject substitutes — only the approved fork is accepted." >&2
    exit 1
fi

if [[ "$ACTUAL_COMMIT" != "$PINNED_SHA" ]]; then
    echo "ERROR: direct_url.json commit '${ACTUAL_COMMIT:0:12}' ≠ pinned '${PINNED_SHA:0:12}'" >&2
    exit 1
fi

echo "[setup] Provenance OK: tamdev999/olo-persistentmemory @ ${PINNED_SHA:0:12}"

# ── Write per-business .env ───────────────────────────────────────────────────
# Stored in business HOME, chmod 600.
# API key is NOT written here — injected via Hermes secret scope at runtime.
# Only variables that exist in Hindsight v1.2.1 config.py are written.
ENV_FILE="${BUSINESS_HOME}/.env"
cat > "$ENV_FILE" <<DOTENV
# Hindsight configuration for business: ${BUSINESS_ID}
# Generated by olo/m3/hindsight/setup.sh
# DO NOT COMMIT — runtime-only. API key set via Hermes secret scope.
# Opus audit: only valid v1.2.1 env vars written.

# ── Database (per-business pg0 instance, §B.9 isolation) ─────────────────────
HINDSIGHT_API_DATABASE_URL=${DATABASE_URL}

# ── Network (loopback only) ───────────────────────────────────────────────────
HINDSIGHT_API_HOST=127.0.0.1
HINDSIGHT_API_PORT=${PORT}

# ── LLM provider (none = structural; deepseek = Option B) ────────────────────
HINDSIGHT_API_LLM_PROVIDER=${PROVIDER}
# HINDSIGHT_API_LLM_API_KEY=  <-- set via Hermes secret scope, NOT here

# ── Decision model ────────────────────────────────────────────────────────────
HINDSIGHT_API_DECISION_MODEL_PROVIDER=none

# ── MCP disabled ─────────────────────────────────────────────────────────────
HINDSIGHT_API_MCP_ENABLED=false

# ── Embeddings: ONNX local CPU, pinned model (audit D12 fix) ─────────────────
HINDSIGHT_API_EMBEDDINGS_PROVIDER=onnx
HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID=intfloat/multilingual-e5-small
# Pin the ONNX model revision for reproducibility (audit D12 fix)
HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_REVISION=614241f622f53c4eeff9890bdc4f31cfecc418b3
# Disable memory arena to reduce idle RSS (audit D14 / V10 fix)
HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA=false

# ── Reranker: RRF (pure algorithmic) ─────────────────────────────────────────
HINDSIGHT_API_RERANKER_PROVIDER=rrf

# ── Document text retention ───────────────────────────────────────────────────
HINDSIGHT_API_STORE_DOCUMENT_TEXT=true

# ── LLM trace ────────────────────────────────────────────────────────────────
HINDSIGHT_API_LLM_TRACE_ENABLED=false

# ── Telemetry ────────────────────────────────────────────────────────────────
OTEL_SDK_DISABLED=true
DOTENV
chmod 600 "$ENV_FILE"

# ── Update port registry (D9 fix) ────────────────────────────────────────────
# Map business_id → {port, bank_id} in $HERMES_HOME/hindsight_port_registry.json
# so HindsightConfig.for_business(id) can resolve the correct endpoint.
REGISTRY_FILE="${HERMES_HOME}/hindsight_port_registry.json"
"$VENV/bin/python3" - <<PYEOF
import json, pathlib, os
reg_path = pathlib.Path("${REGISTRY_FILE}")
registry = {}
if reg_path.exists():
    try:
        registry = json.loads(reg_path.read_text())
    except Exception:
        registry = {}
registry["${BUSINESS_ID}"] = {"port": ${PORT}, "bank_id": "main-v1"}
reg_path.parent.mkdir(parents=True, exist_ok=True)
reg_path.write_text(json.dumps(registry, indent=2))
PYEOF

echo "[setup] Business ${BUSINESS_ID}: env → ${ENV_FILE}"
echo "[setup] Port registry → ${REGISTRY_FILE} (port=${PORT})"
echo "[setup] pg0 instance: ${PG0_INSTANCE} (data: ${BUSINESS_HOME}/.pg0/${PG0_INSTANCE}/)"
echo "[setup] Start: HOME=${BUSINESS_HOME} ${VENV}/bin/hindsight-api (env from ${ENV_FILE})"
