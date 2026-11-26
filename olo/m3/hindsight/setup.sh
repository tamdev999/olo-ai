#!/usr/bin/env bash
# Olo M3: Hindsight v1.2.1 per-business environment setup.
#
# Each business gets its own named pg0 instance and isolated HOME directory.
# Two businesses on the same host cannot share memory (P0-1 isolation).
#
# pg0 uses initdb internally.  Run as a non-root user (e.g. olo_hs).
#
# Usage:
#   bash setup.sh <BUSINESS_ID> [--provider none|deepseek]
#
# Environment variables consumed:
#   HERMES_HOME               — the platform root (required)
#   HINDSIGHT_API_LLM_API_KEY — DeepSeek key (required for provider=deepseek)
#   HINDSIGHT_PORT            — explicit port override; collisions fail safely
#   HINDSIGHT_PYTHON          — Python interpreter to create venv with
#   HF_HOME                   — HuggingFace model cache (optional override)
#
# Port registry:
#   ${HERMES_HOME}/hindsight_port_registry.json  maps  biz_id → {port, bank_id}
#   Ports are allocated once per business; re-running setup.sh reuses the
#   existing port unless HINDSIGHT_PORT explicitly overrides it.
#   Collision detection: if the requested port is already claimed by ANOTHER
#   business the script exits with an error.
#
# Provenance:
#   The venv must have hindsight-api-slim installed from the fork git URL with
#   the pinned SHA.  setup.sh verifies direct_url.json and fails otherwise.
#
# Opus architecture: §B.2, §B.9; revised spec §4.
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

if [[ ! "$BUSINESS_ID" =~ ^[a-z0-9][a-z0-9\-]{0,62}[a-z0-9]$ ]]; then
    echo "ERROR: BUSINESS_ID must be lowercase alphanumeric + hyphens: ${BUSINESS_ID}" >&2
    exit 1
fi

# ── Per-business paths ───────────────────────────────────────────────────────
BUSINESS_HOME="${HERMES_HOME}/businesses/${BUSINESS_ID}"
VENV="${HERMES_HOME}/olo/hindsight-venv"
REGISTRY_FILE="${HERMES_HOME}/hindsight_port_registry.json"
REGISTRY_LOCK="${REGISTRY_FILE}.lock"

# pg0 instance name; stored at $HOME/.pg0/olo-<BUSINESS_ID>/
PG0_INSTANCE="olo-${BUSINESS_ID}"
# C4: add fsync + synchronous_commit for OS-crash durability (pg0 runs with
# -F by default; this parameter re-enables fsync for the specific instance).
DATABASE_URL="pg0://${PG0_INSTANCE}?fsync=on&synchronous_commit=on"

# C4: per-business bank ID — a mis-routed client gets 404, not another
# business's data.  Format: olo-<business_id>
BANK_ID="olo-${BUSINESS_ID}"

# ── Pinned fork ──────────────────────────────────────────────────────────────
FORK_URL="https://github.com/tamdev999/olo-persistentmemory.git"
PINNED_SHA="0eb02d8c418eac6b489bc63883c993b2257dfac4"
FORK_INSTALL="hindsight-api-slim @ git+${FORK_URL}@${PINNED_SHA}#subdirectory=hindsight-api-slim"

# ── Port allocation and registry update (C4: one flock covers both steps) ───
# Keeping allocation and registration under the same flock eliminates the race
# where two concurrent setup.sh runs could both select the same port before
# either writes the registry.
_allocate_and_register() {
    local requested="${HINDSIGHT_PORT:-}"
    (
        flock -x 9

        # Read existing registry
        local existing="{}"
        if [[ -f "$REGISTRY_FILE" ]]; then
            existing=$(cat "$REGISTRY_FILE")
        fi

        # Allocate / validate port, then immediately write the registry —
        # all within the same lock.
        "${_PYTHON:-python3}" - <<PYEOF
import json, socket, sys

try:
    reg = json.loads('''${existing}''')
except Exception:
    reg = {}

requested = '${requested}'
biz_id    = '${BUSINESS_ID}'

if requested:
    # Explicit port: fail if another business already owns it
    for biz, cfg in reg.items():
        if isinstance(cfg, dict) and str(cfg.get('port','')) == requested and biz != biz_id:
            print(f'ERROR: port {requested} already allocated to {biz!r}', file=sys.stderr)
            sys.exit(1)
    port = int(requested)
else:
    # Reuse existing registration for this business
    biz_cfg = reg.get(biz_id, {})
    if isinstance(biz_cfg, dict) and biz_cfg.get('port'):
        port = int(biz_cfg['port'])
    else:
        # Auto-allocate the next free port not already in the registry
        allocated = {int(cfg['port']) for cfg in reg.values()
                     if isinstance(cfg, dict) and cfg.get('port')}
        port = 18800
        while True:
            if port not in allocated:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind(('127.0.0.1', port))
                    s.close()
                    break
                except OSError:
                    pass
            port += 1
            if port > 19900:
                print('ERROR: no free ports available', file=sys.stderr)
                sys.exit(1)

# Write the registry entry atomically (still inside the flock)
import pathlib
reg[biz_id] = {"port": port, "bank_id": '${BANK_ID}'}
reg_path = pathlib.Path('${REGISTRY_FILE}')
reg_path.parent.mkdir(parents=True, exist_ok=True)
reg_path.write_text(json.dumps(reg, indent=2))
print(port)
PYEOF
    ) 9>"${REGISTRY_LOCK}"
}

# ── Python interpreter ───────────────────────────────────────────────────────
_PYTHON="${HINDSIGHT_PYTHON:-}"
if [[ -z "$_PYTHON" ]]; then
    if command -v python3.14 &>/dev/null; then
        _PYTHON="python3.14"
    else
        _PYTHON="python3"
    fi
fi

PORT="$(_allocate_and_register)"
echo "[setup] Business ${BUSINESS_ID} using port ${PORT}"

# ── Create per-business HOME ─────────────────────────────────────────────────
mkdir -p "${BUSINESS_HOME}"
chmod 700 "${BUSINESS_HOME}"

# ── Create venv (shared across businesses; pinned) ───────────────────────────
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

# ── Verify fork provenance ───────────────────────────────────────────────────
DIST_INFO=$("$VENV/bin/python3" - <<PYEOF
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
PYEOF
)

if echo "$DIST_INFO" | grep -q "MISSING"; then
    echo "ERROR: direct_url.json missing — not installed from fork" >&2; exit 1
fi

ACTUAL_URL=$(echo "$DIST_INFO" | grep '^URL=' | cut -d= -f2-)
ACTUAL_COMMIT=$(echo "$DIST_INFO" | grep '^COMMIT=' | cut -d= -f2-)

if [[ "$ACTUAL_URL" != "$FORK_URL" ]]; then
    echo "ERROR: direct_url URL '${ACTUAL_URL}' ≠ fork '${FORK_URL}'" >&2; exit 1
fi
if [[ "$ACTUAL_COMMIT" != "$PINNED_SHA" ]]; then
    echo "ERROR: direct_url commit '${ACTUAL_COMMIT:0:12}' ≠ pinned '${PINNED_SHA:0:12}'" >&2; exit 1
fi
echo "[setup] Provenance OK: tamdev999/olo-persistentmemory @ ${PINNED_SHA:0:12}"

# ── Resolve pinned embedding-model path ─────────────────────────────────────
# Pin via ONNX_MODEL_PATH (local path) rather than a nonexistent REVISION var.
# This guarantees reproducibility without network access after first download.
_HF_HOME="${HF_HOME:-${HOME:-/root}/.cache/huggingface}"
E5_SMALL_PATH="${_HF_HOME}/hub/models--intfloat--multilingual-e5-small/snapshots/614241f622f53c4eeff9890bdc4f31cfecc418b3/onnx/model.onnx"
if [[ -f "$E5_SMALL_PATH" ]]; then
    ONNX_MODEL_PATH_LINE="HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH=${E5_SMALL_PATH}"
    echo "[setup] Pinned ONNX model path: ${E5_SMALL_PATH}"
else
    # Fall back to auto-download by model ID
    ONNX_MODEL_PATH_LINE="# HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH not set (model will be downloaded on first run)"
    echo "[setup] WARN: local e5-small model not found; server will download on first start"
fi

# ── Generate per-business API key (C4: tenant authentication) ───────────────
# A unique API key per business means a mis-routed client gets 401, not data.
# Hindsight 1.2.1 enforces it only through its tenant extension:
#   HINDSIGHT_API_TENANT_EXTENSION names the ApiKeyTenantExtension class and
#   HINDSIGHT_API_TENANT_API_KEY is the key every request must present
#   (Authorization: Bearer <key>).  Without the extension the server accepts
#   unauthenticated requests.
TENANT_EXTENSION="hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension"
ENV_FILE="${BUSINESS_HOME}/.env"
BUSINESS_API_KEY=$("$_PYTHON" -c "import secrets; print(secrets.token_hex(32))")

# If a key already exists (re-run), preserve it.  Earlier versions of this
# script wrote it as HINDSIGHT_API_TENANT_EXTENSION_API_KEY, which Hindsight
# never read; that key is carried over so existing clients keep working.
EXISTING_KEY=""
if [[ -f "$ENV_FILE" ]]; then
    for _var in HINDSIGHT_API_TENANT_API_KEY HINDSIGHT_API_TENANT_EXTENSION_API_KEY; do
        EXISTING_KEY=$(grep "^${_var}=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true)
        [[ -n "$EXISTING_KEY" ]] && break
    done
fi
if [[ -n "$EXISTING_KEY" ]]; then
    BUSINESS_API_KEY="$EXISTING_KEY"
fi

# ── LLM model (provider deepseek) ────────────────────────────────────────────
# One model for every LLM operation: retain, consolidation and reflect fall
# back to HINDSIGHT_API_LLM_MODEL and HINDSIGHT_API_LLM_EXTRA_BODY when their
# own variables are unset.  deepseek-chat was discontinued on 2026-07-24 and
# Hindsight's deepseek default (deepseek-v4-flash) is a retired legacy name;
# deepseek-flash thinks by default, so thinking is disabled explicitly.
LLM_MODEL_LINES=""
if [[ "$PROVIDER" == "deepseek" ]]; then
    LLM_MODEL_LINES="HINDSIGHT_API_LLM_MODEL=deepseek-flash
HINDSIGHT_API_LLM_EXTRA_BODY='{\"thinking\":{\"type\":\"disabled\"}}'"
fi

# ── Write per-business .env ───────────────────────────────────────────────────
# Created 0600 from the start: the file holds the tenant key.
umask 077
cat > "$ENV_FILE" <<DOTENV
# Hindsight configuration for business: ${BUSINESS_ID}
# Generated by olo/m3/hindsight/setup.sh
# DO NOT COMMIT — runtime-only. DeepSeek API key set via Hermes secret scope.
# Only valid Hindsight v1.2.1 env vars are written here.

HINDSIGHT_API_DATABASE_URL=${DATABASE_URL}
HINDSIGHT_API_HOST=127.0.0.1
HINDSIGHT_API_PORT=${PORT}
HINDSIGHT_API_LLM_PROVIDER=${PROVIDER}
${LLM_MODEL_LINES}
# HINDSIGHT_API_LLM_API_KEY=  <-- set via Hermes secret scope, NOT here

# C4: Per-business tenant authentication (enforced by the tenant extension).
# A request without this key, or with another business's key, gets HTTP 401.
HINDSIGHT_API_TENANT_EXTENSION=${TENANT_EXTENSION}
HINDSIGHT_API_TENANT_API_KEY=${BUSINESS_API_KEY}

# C4: Per-business bank ID — a mis-routed client gets 404, not data.
# HINDSIGHT_API_DEFAULT_BANK_ID removed: silently ignored in v1.2.1 (bank_id is
# passed per-request; Opus audit TestConfigValidity.test_removed_invalid_vars_absent){BANK_ID}

HINDSIGHT_API_DECISION_MODEL_PROVIDER=none
HINDSIGHT_API_MCP_ENABLED=false
HINDSIGHT_API_EMBEDDINGS_PROVIDER=onnx
HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID=intfloat/multilingual-e5-small
${ONNX_MODEL_PATH_LINE}
# HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_REVISION is NOT a valid v1.2.1 config var
# and is intentionally omitted.  Use ONNX_MODEL_PATH for reproducibility.
HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA=false
HINDSIGHT_API_RERANKER_PROVIDER=rrf
HINDSIGHT_API_STORE_DOCUMENT_TEXT=true
# C6: Enable LLM trace so the budget guard can see token counts.
# Content capture is off — only token counts are stored.
HINDSIGHT_API_LLM_TRACE_ENABLED=true
# HINDSIGHT_API_LLM_TRACE_STORE_CONTENT is not a valid v1.2.1 config var; omitted.
# C7: Disable bank-wide automatic consolidation.
# The Olo projection stage (olo/projection.py) prepares the bank (registers
# the structured-no-llm strategy: chunks extraction, observations off) and
# triggers consolidation itself once prose retains have completed, as a
# tracked, budget-guarded operation.  Structured documents are excluded from
# observations by their strategy.
HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION=false
OTEL_SDK_DISABLED=true
DOTENV
chmod 600 "$ENV_FILE"

# (Port registry was written atomically by _allocate_and_register above.)

echo "[setup] Business ${BUSINESS_ID}: env → ${ENV_FILE}"
echo "[setup] Port registry → ${REGISTRY_FILE} (port=${PORT})"
echo "[setup] pg0: ${PG0_INSTANCE} (data: ${BUSINESS_HOME}/.pg0/${PG0_INSTANCE}/)"
echo "[setup] Bank ID: ${BANK_ID}"
echo "[setup] LLM trace: enabled (content capture: off)"
echo "[setup] Tenant authentication: ApiKeyTenantExtension (key in ${ENV_FILE})"
echo "[setup] Start: HOME=${BUSINESS_HOME} ${VENV}/bin/hindsight-api (env from ${ENV_FILE})"
echo ""
echo "[setup] ARCHITECTURE NOTE (C7): Hindsight 1.2.1 treats structured memories"
echo "[setup] as consolidation candidates despite their strategy.  The Olo projection"
echo "[setup] stage therefore scopes every consolidation to prose documents (tag"
echo "[setup] olo:prose); never trigger a bank-wide consolidation on an Olo bank."
