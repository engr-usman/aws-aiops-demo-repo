#!/bin/bash
# =============================================================================
# 01-install-driver.sh
# Phase 1: NVIDIA driver install for a plain Ubuntu GPU instance (e.g. AWS EC2
# g6.xlarge with a vanilla Ubuntu AMI, NOT the Deep Learning AMI).
#
# Skip this entire script if you launched with the "Deep Learning Base OSS
# Nvidia Driver GPU AMI" — driver is already installed there.
#
# IMPORTANT: This script ends with a reboot. After reboot, SSH back in and
# run 02-setup-vllm.sh.
# =============================================================================
set -euo pipefail

echo "=== Updating package lists ==="
sudo apt update
sudo apt install nvtop

echo "=== Installing kernel headers + build tools (required for driver DKMS build) ==="
sudo apt install -y "linux-headers-$(uname -r)" build-essential

echo "=== Installing NVIDIA server driver ==="
# -server variant is correct for headless/data-center GPUs (L4, A10G, A100, H100).
# Check available versions with: apt search nvidia-driver
sudo apt install -y nvidia-utils-595-server nvidia-driver-595-server

echo "=== Driver installed. Rebooting now — reconnect in ~60 seconds and run 02-setup-vllm.sh ==="
sudo reboot
