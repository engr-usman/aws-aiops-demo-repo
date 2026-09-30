#!/bin/bash
# =============================================================================
# 03-serve-model.sh
# Starts vLLM's OpenAI-compatible API server with the environment fixes
# from 02-setup-vllm.sh applied. Edit the variables below as needed.
#
# Manual usage (unchanged): ./03-serve-model.sh
#   -> streams the log to your terminal (tail -f), same as before.
#
# Automated/background usage (NEW): NO_WAIT_TAIL=true ./03-serve-model.sh
#   -> starts the server and returns immediately, without blocking on tail -f.
#      Used by the Terraform bootstrap orchestrator, which runs this
#      non-interactively and cannot sit blocked on a foreground tail.
#
# Logs always stream to ~/vllm.log regardless of mode — watch anytime with:
#   tail -f ~/vllm.log
# Stop the server with: pkill -f "vllm serve"
# =============================================================================
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3-8B}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
PORT="${PORT:-8000}"
DTYPE="${DTYPE:-bfloat16}"
ENFORCE_EAGER="${ENFORCE_EAGER:-false}"   # set to "true" as a last-resort fallback
NO_WAIT_TAIL="${NO_WAIT_TAIL:-false}"     # set to "true" for non-interactive/automated runs

source ~/vllm-env/bin/activate

# Re-apply CUDA_HOME in case this is a fresh shell that hasn't sourced ~/.bashrc
NVCC_PATH=$(find ~/vllm-env -iname "nvcc" 2>/dev/null | head -n1)
if [ -n "$NVCC_PATH" ]; then
  export CUDA_HOME=$(dirname "$(dirname "$NVCC_PATH")")
  export PATH="$CUDA_HOME/bin:$PATH"
fi

# Works around the FlashInfer sampling-kernel JIT build failure seen on
# Ubuntu 26.04 + Python 3.14 + CUDA 13.4 (ninja build fails at compile time).
# Harmless to set even on environments where the issue doesn't occur.
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

VLLM_PID=$!
echo "vLLM starting in background (PID $VLLM_PID). Log: ~/vllm.log"

if [ "$NO_WAIT_TAIL" = "true" ]; then
  echo "NO_WAIT_TAIL=true — returning immediately without following the log."
  echo "Check progress with: tail -f ~/vllm.log"
  exit 0
fi

sleep 2
tail -f ~/vllm.log
