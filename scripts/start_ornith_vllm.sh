#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VLLM_DIR="${VLLM_DIR:-/Volumes/MacDataSSDPro/development/ornith-official-runtime/vllm-metal}"
HF_HOME="${HF_HOME:-/Volumes/MacDataSSDPro/.cache/huggingface}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8001}"
API_KEY="${API_KEY:-vllm}"
MODEL_ID="${MODEL_ID:-deepreinforce-ai/Ornith-1.0-9B}"
SERVED_MODEL_NAME="${SERVED_MODEL_NAME:-ornith-vllm-metal}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-44352}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-1}"
ENABLE_AUTO_TOOL_CHOICE="${ENABLE_AUTO_TOOL_CHOICE:-1}"
TOOL_CALL_PARSER="${TOOL_CALL_PARSER:-qwen3_xml}"
ALLOW_DOWNLOAD=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      shift
      ;;
    --allow-download)
      ALLOW_DOWNLOAD=1
      shift
      ;;
    *)
      break
      ;;
  esac
done

mkdir -p "$HF_HOME"
export HF_HOME
export HF_HUB_CACHE="${HF_HUB_CACHE:-$HF_HOME/hub}"
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
export VLLM_METAL_MEMORY_FRACTION="${VLLM_METAL_MEMORY_FRACTION:-0.97}"
export VLLM_METAL_USE_PAGED_ATTENTION="${VLLM_METAL_USE_PAGED_ATTENTION:-1}"

if [[ "$ALLOW_DOWNLOAD" != "1" ]]; then
  export HF_HUB_OFFLINE=1
  export TRANSFORMERS_OFFLINE=1
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/check_ornith_cache.py" \
    --hf-home "$HF_HOME" \
    --repo-id "$MODEL_ID" \
    --require-complete
else
  echo "WARNING: --allow-download was passed; vLLM may download missing Hugging Face model files." >&2
  "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/scripts/check_ornith_cache.py" \
    --hf-home "$HF_HOME" \
    --repo-id "$MODEL_ID" || true
fi

cd "$VLLM_DIR"
EXTRA_ARGS=(--max-num-seqs "$MAX_NUM_SEQS")
if [[ "$ENABLE_AUTO_TOOL_CHOICE" == "1" ]]; then
  EXTRA_ARGS+=(--enable-auto-tool-choice --tool-call-parser "$TOOL_CALL_PARSER")
fi

exec .venv-vllm-metal/bin/vllm serve "$MODEL_ID" \
  --served-model-name "$SERVED_MODEL_NAME" \
  --host "$HOST" \
  --port "$PORT" \
  --max-model-len "$MAX_MODEL_LEN" \
  --api-key "$API_KEY" \
  "${EXTRA_ARGS[@]}" \
  "$@"
