#!/bin/bash
# =============================================================================
# cloud-init user-data (runs once, as root, on first boot)
#
# This script does NOT run 01/02/03 directly, because 01-install-driver.sh
# ends with a reboot — a plain user-data script would die mid-sequence when
# that reboot happens and never resume.
#
# Instead:
#   1. Clones the demo repo
#   2. Writes a "start-vllm.sh" wrapper + a "vllm-serve.service" systemd unit
#      that will run vLLM as a proper, supervised service (auto-restart on
#      crash, survives reboots) — defined here but not started yet, since
#      the GPU driver and Python env don't exist until steps 1/2 complete.
#   3. Writes an idempotent "orchestrator" script that checks marker files
#      to know which step is next, and installs it as a systemd service that
#      runs on EVERY boot (including after 01's reboot) and no-ops once
#      all steps are done.
#
# State markers live under /opt/bootstrap/state/. Bootstrap progress is
# logged to /var/log/bootstrap.log. vLLM server logs go to ~/vllm.log, same
# as in manual runs.
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a   # auto-restart services during apt installs, never prompt

REPO_URL="${github_repo_url}"
DEMO_DIR_NAME="${demo_dir_name}"
AUTO_SHUTDOWN_MINUTES="${auto_shutdown_minutes}"

REPO_DIR="/home/ubuntu/aws-aiops-demo-repo"
DEMO_PATH="$REPO_DIR/$DEMO_DIR_NAME"
STATE_DIR="/opt/bootstrap/state"
LOG="/var/log/bootstrap.log"

mkdir -p "$STATE_DIR"
mkdir -p /opt/bootstrap
touch "$LOG"
chmod 644 "$LOG"

echo "=== [$(date -Is)] user-data starting ===" >> "$LOG"

apt-get update >> "$LOG" 2>&1
apt-get install -y git >> "$LOG" 2>&1

if [ ! -d "$REPO_DIR" ]; then
  echo "Cloning $REPO_URL" >> "$LOG"
  sudo -u ubuntu git clone "$REPO_URL" "$REPO_DIR" >> "$LOG" 2>&1
fi
chmod +x "$DEMO_PATH"/*.sh || true

# ---------------------------------------------------------------------------
# vLLM wrapper script + systemd service (defined now, started later by the
# orchestrator in Step 3, once the driver + Python env actually exist).
# ---------------------------------------------------------------------------
cat > /opt/bootstrap/start-vllm.sh <<'STARTVLLM_EOF'
#!/bin/bash
set -euo pipefail

VENV_DIR="/home/ubuntu/vllm-env"

NVCC_PATH=$(find "$VENV_DIR" -iname "nvcc" 2>/dev/null | head -n1)
if [ -n "$NVCC_PATH" ]; then
  export CUDA_HOME
  CUDA_HOME=$(dirname "$(dirname "$NVCC_PATH")")
  export PATH="$CUDA_HOME/bin:$PATH"
fi

export VLLM_USE_FLASHINFER_SAMPLER=0

exec "$VENV_DIR/bin/vllm" serve "$${MODEL:-Qwen/Qwen3-8B}" \
  --dtype "$${DTYPE:-bfloat16}" \
  --max-model-len "$${MAX_MODEL_LEN:-8192}" \
  --gpu-memory-utilization "$${GPU_MEM_UTIL:-0.90}" \
  --port "$${PORT:-8000}"
STARTVLLM_EOF
chmod +x /opt/bootstrap/start-vllm.sh

cat > /etc/systemd/system/vllm-serve.service <<'VLLMUNIT_EOF'
[Unit]
Description=vLLM OpenAI-compatible inference server
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu
Environment=MODEL=Qwen/Qwen3-8B
Environment=DTYPE=bfloat16
Environment=MAX_MODEL_LEN=8192
Environment=GPU_MEM_UTIL=0.90
Environment=PORT=8000
ExecStart=/opt/bootstrap/start-vllm.sh
Restart=on-failure
RestartSec=15
TimeoutStartSec=900
StandardOutput=append:/home/ubuntu/vllm.log
StandardError=append:/home/ubuntu/vllm.log

[Install]
WantedBy=multi-user.target
VLLMUNIT_EOF

systemctl daemon-reload
systemctl enable vllm-serve.service
# NOT started here — the driver and Python venv don't exist yet on first
# boot. The orchestrator's Step 3 starts it once steps 1 and 2 are done.
# `enable` means it WILL auto-start on any future reboot of this instance,
# which is a nice side benefit (server comes back up after a manual reboot).

# ---------------------------------------------------------------------------
# Orchestrator script — runs 01 -> (reboot) -> 02 -> starts vllm-serve.service
# ---------------------------------------------------------------------------
cat > /opt/bootstrap/orchestrator.sh <<'ORCHESTRATOR_EOF'
#!/bin/bash
set -uo pipefail

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

STATE_DIR="/opt/bootstrap/state"
LOG="/var/log/bootstrap.log"
DEMO_PATH="__DEMO_PATH__"
AUTO_SHUTDOWN_MINUTES="__AUTO_SHUTDOWN_MINUTES__"

log() { echo "[$(date -Is)] $*" >> "$LOG"; }

log "orchestrator run starting"

# --- Step 1: NVIDIA driver (01-install-driver.sh ends with a reboot) -------
if [ ! -f "$STATE_DIR/01-driver-done" ]; then
  if [ ! -f "$STATE_DIR/01-driver-started" ]; then
    log "Step 1: starting driver install (will reboot when done)"
    touch "$STATE_DIR/01-driver-started"
    cd "$DEMO_PATH"
    sudo -u ubuntu bash -lc "cd '$DEMO_PATH' && ./01-install-driver.sh" >> "$LOG" 2>&1
    log "01-install-driver.sh returned control (reboot should be imminent) — exiting this run"
    exit 0
  else
    if nvidia-smi >/dev/null 2>&1; then
      log "Step 1: driver verified working after reboot"
      touch "$STATE_DIR/01-driver-done"
    else
      log "Step 1: driver not yet verified on this boot — nothing more to do, will re-check next boot"
      exit 0
    fi
  fi
fi

# --- Step 2: Python/vLLM environment setup ----------------------------------
if [ ! -f "$STATE_DIR/02-setup-done" ]; then
  log "Step 2: running 02-setup-vllm.sh"
  sudo -u ubuntu bash -lc "cd '$DEMO_PATH' && ./02-setup-vllm.sh" >> "$LOG" 2>&1
  if [ $? -ne 0 ]; then
    log "Step 2 FAILED — check $LOG for details. Will retry on next boot/service restart."
    exit 1
  fi
  touch "$STATE_DIR/02-setup-done"
  log "Step 2: complete"
fi

# --- Step 3: start the vLLM server as a proper systemd service -------------
if [ ! -f "$STATE_DIR/03-serve-started" ]; then
  log "Step 3: starting vllm-serve.service"
  systemctl start vllm-serve.service
  touch "$STATE_DIR/03-serve-started"
  log "Step 3: vllm-serve.service started — check ~/vllm.log or 'systemctl status vllm-serve' for progress"
  log "Bootstrap complete. Scheduling safety-net auto-shutdown in $${AUTO_SHUTDOWN_MINUTES} minutes."
  shutdown -h "+$${AUTO_SHUTDOWN_MINUTES}" || true
fi

log "orchestrator run finished (all steps done — future boots will no-op)"
ORCHESTRATOR_EOF

sed -i "s|__DEMO_PATH__|$DEMO_PATH|g" /opt/bootstrap/orchestrator.sh
sed -i "s|__AUTO_SHUTDOWN_MINUTES__|$AUTO_SHUTDOWN_MINUTES|g" /opt/bootstrap/orchestrator.sh
chmod +x /opt/bootstrap/orchestrator.sh

# ---------------------------------------------------------------------------
# systemd service for the orchestrator itself — runs on every boot, becomes
# a no-op once all 3 steps are done (ConditionPathExists, negated).
# No background child processes are spawned by this unit anymore (Step 3
# now just does `systemctl start` on an independent unit), so the earlier
# cgroup-kill-on-deactivate concern doesn't apply here.
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/bootstrap-orchestrator.service <<'UNIT_EOF'
[Unit]
Description=vLLM benchmark bootstrap orchestrator (idempotent, resumes across reboots)
After=network-online.target cloud-init.service
Wants=network-online.target
ConditionPathExists=!/opt/bootstrap/state/03-serve-started

[Service]
Type=oneshot
ExecStart=/opt/bootstrap/orchestrator.sh
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
UNIT_EOF

systemctl daemon-reload
systemctl enable bootstrap-orchestrator.service
echo "=== [$(date -Is)] user-data done, triggering first orchestrator run ===" >> "$LOG"
systemctl start bootstrap-orchestrator.service
