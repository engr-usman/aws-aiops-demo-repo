#!/bin/bash
# =============================================================================
# 02-setup-vllm.sh
# Phase 2: Python toolchain + vLLM install, with fixes baked in for issues
# hit on a fresh Ubuntu 26.04 + Python 3.14 + CUDA 13 GPU instance:
#
#   1. "fatal error: stdlib.h: No such file or directory"
#      -> missing libc6-dev / build-essential (C compiler couldn't find
#         basic C headers during Triton/torch.compile JIT).
#   2. "fatal error: Python.h: No such file or directory"
#      -> missing python3-dev / python3.X-dev (Python C extension headers).
#   3. "RuntimeError: Could not find nvcc and default cuda_home=
#      '/usr/local/cuda' doesn't exist"
#      -> nvcc ships inside the pip-installed nvidia-cuda-nvcc package, but
#         nothing points CUDA_HOME/PATH at it.
#   4. FlashInfer sampling kernel JIT build fails at ninja/subprocess level
#      (environment-specific toolchain incompatibility, e.g. bleeding-edge
#      Python 3.14 + very new CUDA 13.4). Worked around by disabling the
#      FlashInfer sampler and falling back to vLLM's native PyTorch sampler.
#
# Run this AFTER 01-install-driver.sh + reboot (or directly, if your AMI
# already has the NVIDIA driver, e.g. the Deep Learning AMI).
# =============================================================================
set -euo pipefail

echo "=== Verifying GPU driver ==="
nvidia-smi || { echo "nvidia-smi failed — driver not loaded. Run 01-install-driver.sh first and reboot."; exit 1; }

echo "=== Installing matching python3-dev for $(python3 --version) ==="
sudo apt update
sudo apt install -y python3.14-venv
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
sudo apt install -y python3-dev "python${PYVER}-dev" build-essential

echo "=== Creating virtual environment ==="
python3 -m venv ~/vllm-env
source ~/vllm-env/bin/activate
pip install --upgrade pip

echo "=== Installing vLLM (pulls in PyTorch, CUDA libs, FlashAttention, FlashInfer, etc.) ==="
pip install vllm

echo "=== Locating pip-installed nvcc and wiring CUDA_HOME/PATH ==="
NVCC_PATH=$(find ~/vllm-env -iname "nvcc" 2>/dev/null | head -n1)
if [ -z "$NVCC_PATH" ]; then
  echo "WARNING: nvcc not found under vllm-env. torch.compile / FlashInfer JIT paths may fail."
else
  CUDA_HOME_DIR=$(dirname "$(dirname "$NVCC_PATH")")
  echo "Found nvcc at: $NVCC_PATH"
  echo "Setting CUDA_HOME=$CUDA_HOME_DIR"

  # Persist for future shells
  if ! grep -q "CUDA_HOME=$CUDA_HOME_DIR" ~/.bashrc 2>/dev/null; then
    {
      echo "export CUDA_HOME=$CUDA_HOME_DIR"
      echo 'export PATH=$CUDA_HOME/bin:$PATH'
    } >> ~/.bashrc
  fi
  export CUDA_HOME="$CUDA_HOME_DIR"
  export PATH="$CUDA_HOME/bin:$PATH"
fi

echo "=== Verifying nvcc ==="
nvcc --version || echo "WARNING: nvcc still not on PATH — check manually."

echo ""
echo "=== Setup complete. ==="
echo "Before serving a model, also run:"
echo "  export VLLM_USE_FLASHINFER_SAMPLER=0   # works around FlashInfer JIT build failure"
echo ""
echo "Then use 03-serve-model.sh to start vLLM."
