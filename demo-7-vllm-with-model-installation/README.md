# vLLM + Qwen3-8B Benchmark on AWS EC2 (L4 GPU)

Hands-on MLOps/LLMOps learning project: deploy Qwen3-8B on vLLM from scratch
on a bare AWS EC2 GPU instance, and benchmark throughput/latency across
concurrency levels. Part of an AWS Learning / AIOps portfolio series.

## What this covers

- Deploying an OpenAI-compatible LLM inference server (vLLM) from a plain
  Ubuntu EC2 instance with no pre-baked ML tooling
- Diagnosing and fixing real toolchain/driver issues (not idealized happy-path
  instructions — this is what actually broke and how it was fixed)
- Load-testing an LLM server at multiple concurrency levels and interpreting
  the results (throughput, TTFT, TPOT, latency percentiles)
- Cost-controlled experimentation: every step assumes you terminate the
  instance when done

## Environment used

| Component | Value |
|---|---|
| Instance | AWS EC2 `g6.xlarge` (1x NVIDIA L4, 24 GB VRAM) |
| Base OS | Ubuntu 26.04 (plain AMI, driver installed manually) |
| Driver | `nvidia-driver-595-server` |
| CUDA | 13.4 (via pip `nvidia-cuda-nvcc`) |
| Python | 3.14.4 |
| vLLM | 0.30.0 |
| Model | `Qwen/Qwen3-8B`, bfloat16 |

**Note:** using the AWS "Deep Learning Base OSS Nvidia Driver GPU AMI"
instead of a plain Ubuntu AMI skips the entire driver-install step (script
`01`) and avoids some of the toolchain gaps below.

## Quick start

```bash
# 1. Only if your AMI does NOT already have the NVIDIA driver.
#    This script ends in a reboot.
./scripts/01-install-driver.sh

# 2. After reconnecting via SSH:
./scripts/02-setup-vllm.sh

# 3. Start the server (defaults to Qwen/Qwen3-8B on port 8000):
./scripts/03-serve-model.sh
# watch for "Application startup complete" in the log

# 4. In a separate terminal/SSH session:
./scripts/04-run-benchmark.sh
```

Results land in `~/bench-results/` as `c<concurrency>.json` / `.txt`.

## Gotchas hit on this environment (and why)

This combination (fresh Ubuntu 26.04 + Python 3.14 + very recent CUDA/vLLM)
is bleeding-edge, so several C/CUDA toolchain pieces that a mature AMI would
already have were missing. Each was a progressively deeper failure — the
process kept failing further into vLLM's startup sequence as each layer got
fixed. Documenting the sequence because this progression pattern is a useful
debugging signal in general, not just for this stack.

1. **`fatal error: stdlib.h: No such file or directory`**
   Basic C standard library headers missing. `build-essential` alone wasn't
   enough — needed `libc6-dev` reinstalled/verified.
   *Fix:* `sudo apt install --reinstall -y build-essential libc6-dev`

2. **`fatal error: Python.h: No such file or directory`**
   Python C-extension headers missing (needed for Triton/torch.compile JIT).
   *Fix:* `sudo apt install -y python3-dev python3.14-dev` (match your exact
   `python3 --version`).

3. **`RuntimeError: Could not find nvcc and default cuda_home='/usr/local/cuda' doesn't exist`**
   `nvcc` was present (installed as part of `pip install vllm`'s
   `nvidia-cuda-nvcc` dependency) but nothing pointed `CUDA_HOME`/`PATH` at
   it — vLLM/FlashInfer look in `/usr/local/cuda` by default, which doesn't
   exist when CUDA came from pip rather than a system install.
   *Fix:*
   ```bash
   export CUDA_HOME=$(dirname $(dirname $(find ~/vllm-env -iname nvcc)))
   export PATH=$CUDA_HOME/bin:$PATH
   ```

4. **FlashInfer sampling kernel JIT build fails** (`ninja` subprocess returns
   non-zero, deep in `flashinfer/jit/cpp_ext.py`)
   Even with `nvcc` resolvable, FlashInfer's own JIT-compiled top-k/top-p
   sampling kernel failed to build on this Python 3.14 + CUDA 13.4
   combination — likely a toolchain compatibility gap not yet ironed out for
   this very new combination.
   *Fix (workaround, not a real fix):* disable the FlashInfer sampler and
   fall back to vLLM's native PyTorch sampler:
   ```bash
   export VLLM_USE_FLASHINFER_SAMPLER=0
   ```

**If you hit a further JIT/compile error beyond these**, the guaranteed
fallback is `--enforce-eager`, which disables `torch.compile` and CUDA graph
capture entirely (slower, but removes the whole JIT compilation path as a
source of failures). `03-serve-model.sh` supports this via
`ENFORCE_EAGER=true ./scripts/03-serve-model.sh`.

## Interpreting the numbers

- **KV cache size** (log line `GPU KV cache size: N tokens`): on this
  instance, actual came out to **19,776 tokens** — noticeably below a naive
  weights-and-formula estimate (~34K tokens). The gap is CUDA graph capture
  memory (~0.38 GiB) and peak activation memory (~1.6 GiB) that a simple
  calculation doesn't account for. Treat theoretical VRAM/KV-cache math as an
  **upper bound**, not an exact prediction — real overhead typically eats
  30-40% of the naive estimate.
- **Maximum concurrency** for the configured `--max-model-len`: reported
  directly in the same log line (`2.41x` at 8192 tokens/request on this
  setup) — this is the practical ceiling for full-context concurrent
  requests before requests start queueing.
- Full throughput/latency numbers per concurrency level: see
  `bench-results/*.txt` after running `04-run-benchmark.sh`.

## Cost control

- Every script assumes you'll **terminate** (not just stop) the instance
  when finished — stopping still bills for attached EBS storage.
- `01-install-driver.sh` and the model download in `03-serve-model.sh` are
  the two slowest steps (~2-3 min and ~2 min respectively on this instance
  type/model size); budget for that when estimating session cost.
- Recommended: set a CloudWatch billing alarm and an EC2 user-data
  auto-shutdown (`shutdown -h +180`) as a safety net before launching.

## Next steps (planned)

- Compare against Triton Inference Server and TensorRT-LLM on the same
  hardware/model
- Benchmark FP8 vs AWQ-INT4 quantized variants
- GPU-aware autoscaling (KEDA / GKE-native) for bursty traffic
