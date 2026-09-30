#!/bin/bash
# =============================================================================
# 03-serve-model.sh
# Starts vLLM's OpenAI-compatible API server with the environment fixes
# from 02-setup-vllm.sh applied. Edit the variables below as needed.
#
# Usage: ./03-serve-model.sh
# Logs stream to ~/vllm.log — watch with: tail -f ~/vllm.log
# Stop the server with: pkill -f "vllm serve"
# =============================================================================
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3-8B}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
PORT="${PORT:-8000}"
DTYPE="${DTYPE:-bfloat16}"
ENFORCE_EAGER="${ENFORCE_EAGER:-false}"   # set to "true" as a last-resort fallback

source ~/vllm-env/bin/activate

# Re-apply CUDA_HOME in case this is a fresh shell that hasn't sourced ~/.bashrc
NVCC_PATH=$(find ~/vllm-env -iname "nvcc" 2>/dev/null | head -n1)
if [ -n "$NVCC_PATH" ]; then
  export CUDA_HOME=$(dirname "$(dirname "$NVCC_PATH")")
  export PATH="$CUDA_HOME/bin:$PATH"
fi

# Works around the FlashInfer sampling-kernel JIT build failure seen on
# Ubuntu 26.04 + Python 3.14 + CUDA 13.4 (ninja build fails at compile time).
export VLLM_USE_FLASHINFER_SAMPLER=0

EAGER_FLAG=""
if [ "$ENFORCE_EAGER" = "true" ]; then
  EAGER_FLAG="--enforce-eager"
  echo "=== Running in --enforce-eager mode (torch.compile + CUDA graphs disabled) ==="
fi

echo "=== Starting vLLM: model=$MODEL max_len=$MAX_MODEL_LEN gpu_util=$GPU_MEM_UTIL port=$PORT ==="

nohup vllm serve "$MODEL" \
  --dtype "$DTYPE" \
  --max-model-len "$MAX_MODEL_LEN" \
  --gpu-memory-utilization "$GPU_MEM_UTIL" \
  --port "$PORT" \
  $EAGER_FLAG \
  > ~/vllm.log 2>&1 &

echo "vLLM starting in background (PID $!). Watching log — Ctrl+C to stop watching (server keeps running):"
sleep 2
tail -f ~/vllm.log
