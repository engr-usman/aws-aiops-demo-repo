#!/bin/bash
# =============================================================================
# 04-run-benchmark.sh
# Runs vLLM's built-in benchmark client against a running server at
# multiple concurrency levels, saving results to ~/bench-results/.
#
# Run this in a SEPARATE terminal/SSH session from 03-serve-model.sh,
# once the log shows "Application startup complete".
# =============================================================================
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3-8B}"
HOST="${HOST:-localhost}"
PORT="${PORT:-8000}"
CONCURRENCY_LEVELS="${CONCURRENCY_LEVELS:-1 5 10 20 50}"

source ~/vllm-env/bin/activate
OUT=~/bench-results
mkdir -p "$OUT"

echo "=== Warmup (result discarded — first requests compile/cache kernels) ==="
vllm bench serve --backend vllm --model "$MODEL" --host "$HOST" --port "$PORT" \
  --dataset-name random --random-input-len 512 --random-output-len 128 \
  --num-prompts 5 > /dev/null 2>&1 || echo "Warmup call failed — check server is up (curl http://$HOST:$PORT/health)"

for C in $CONCURRENCY_LEVELS; do
  NP=$(( C * 10 < 40 ? 40 : C * 10 ))
  echo ""
  echo "=== Concurrency $C ($NP prompts) ==="
  vllm bench serve \
    --backend vllm \
    --model "$MODEL" \
    --host "$HOST" \
    --port "$PORT" \
    --dataset-name random \
    --random-input-len 512 \
    --random-output-len 256 \
    --num-prompts "$NP" \
    --max-concurrency "$C" \
    --save-result --result-dir "$OUT" --result-filename "c${C}.json" \
    | tee "$OUT/c${C}.txt"
done

echo ""
echo "=== Done. Results in $OUT ==="
