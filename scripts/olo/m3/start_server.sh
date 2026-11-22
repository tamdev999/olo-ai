#!/bin/bash
# Start Hindsight server for M3 gate testing.
# This script is idempotent — safe to source after a reboot.
set -euo pipefail

chmod 711 /root 2>/dev/null || true
chmod a+rx /root/olo_m3_hindsight_venv/bin/hindsight-api 2>/dev/null || true
chmod -R a+rX /root/olo_m3_hindsight_venv 2>/dev/null || true
chmod -R a+rwX /var/cache/huggingface/hub/.locks/ 2>/dev/null || true

source /root/olo_m3_secrets/deepseek.env 2>/dev/null || true

exec sudo -u olo_hs env \
  HOME=/home/olo_hs/hs_gate_data \
  HF_HOME=/var/cache/huggingface \
  HF_HUB_OFFLINE=1 \
  HINDSIGHT_API_DATABASE_URL=postgresql://hindsight@/hindsight?host=/var/run/postgresql \
  HINDSIGHT_API_HOST=127.0.0.1 \
  HINDSIGHT_API_PORT=18800 \
  HINDSIGHT_API_LLM_PROVIDER=deepseek \
  HINDSIGHT_API_LLM_MODEL=deepseek-chat \
  HINDSIGHT_API_LLM_API_KEY="${HINDSIGHT_API_LLM_API_KEY}" \
  HINDSIGHT_API_EMBEDDINGS_PROVIDER=onnx \
  HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_ID=ibm-granite/granite-embedding-97m-multilingual-r2 \
  HINDSIGHT_API_EMBEDDINGS_ONNX_MODEL_PATH=/var/cache/huggingface/hub/models--ibm-granite--granite-embedding-97m-multilingual-r2/snapshots/835ad14087e140460703cf0fae09f97d469d65c2/onnx/model_quint8_avx2.onnx \
  HINDSIGHT_API_EMBEDDINGS_ONNX_POOLING=cls \
  HINDSIGHT_API_EMBEDDINGS_ONNX_NORMALIZE=true \
  HINDSIGHT_API_EMBEDDINGS_ONNX_QUERY_PREFIX= \
  HINDSIGHT_API_EMBEDDINGS_ONNX_PASSAGE_PREFIX= \
  HINDSIGHT_API_EMBEDDINGS_ONNX_CPU_MEM_ARENA=false \
  HINDSIGHT_API_RERANKER_PROVIDER=onnx \
  HINDSIGHT_API_RERANKER_ONNX_MODEL_ID=hindsight-ai/hs-rerank-1 \
  HINDSIGHT_API_RERANKER_ONNX_MODEL_PATH=/var/cache/huggingface/hub/models--hindsight-ai--hs-rerank-1/snapshots/927f5be31280298a51cf29af8a29cb111ed812c5/onnx/model.onnx \
  HINDSIGHT_API_RERANKER_ONNX_TOKENIZER_NAME_OR_PATH=/var/cache/huggingface/hub/models--hindsight-ai--hs-rerank-1/snapshots/927f5be31280298a51cf29af8a29cb111ed812c5 \
  HINDSIGHT_API_RERANKER_MAX_CANDIDATES=400 \
  HINDSIGHT_API_RECENCY_DECAY_FUNCTION=none \
  HINDSIGHT_API_DECISION_MODEL_PROVIDER=none \
  HINDSIGHT_API_TELEMETRY_ENABLED=false \
  HINDSIGHT_API_MCP_ENABLED=false \
  HINDSIGHT_API_CLOUD_ENABLED=false \
  HINDSIGHT_API_WEBHOOKS_ENABLED=false \
  HINDSIGHT_API_STORE_DOCUMENT_TEXT=true \
  HINDSIGHT_API_LLM_TRACE_ENABLED=true \
  HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT=2 \
  HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT=2 \
  OTEL_SDK_DISABLED=true \
  /root/olo_m3_hindsight_venv/bin/hindsight-api
