#!/bin/bash
# =============================================================================
# cloud-init user-data (runs once, as root, on first boot)
#
# This script does NOT run 01/02/03 directly, because 01-install-driver.sh
# ends with a reboot — a plain user-data script would die mid-sequence when
# that reboot happens and never resume.
#
# Instead, this script:
#   1. Clones the demo repo
#   2. Writes an idempotent "orchestrator" script that checks marker files
#      to know which step is next
#   3. Installs the orchestrator as a systemd service that runs on EVERY
#      boot (including after 01's reboot) and no-ops once all steps are done
#
# State markers live under /opt/bootstrap/state/. Bootstrap progress is
# logged to /var/log/bootstrap.log.
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
# Orchestrator script — this is what actually runs 01 -> (reboot) -> 02 -> 03
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
    # We already started step 1 on a previous boot; check if the driver is
    # now actually loaded (i.e. we're past the reboot).
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

# --- Step 3: start the vLLM server in the background ------------------------
if [ ! -f "$STATE_DIR/03-serve-started" ]; then
  log "Step 3: starting 03-serve-model.sh (background, non-interactive mode)"
  sudo -u ubuntu bash -lc "cd '$DEMO_PATH' && export VLLM_USE_FLASHINFER_SAMPLER=0 && NO_WAIT_TAIL=true nohup ./03-serve-model.sh > /home/ubuntu/vllm-serve-bootstrap.out 2>&1 &"
  touch "$STATE_DIR/03-serve-started"
  log "Step 3: server launch triggered — check ~/vllm.log on the instance for startup progress"
  log "Bootstrap complete. Scheduling safety-net auto-shutdown in $${AUTO_SHUTDOWN_MINUTES} minutes."
  shutdown -h "+$${AUTO_SHUTDOWN_MINUTES}" || true
fi

log "orchestrator run finished (all steps done — future boots will no-op)"
ORCHESTRATOR_EOF

# Fill in the placeholders (done here in user-data, not in the heredoc above,
# so Terraform's template variables interpolate correctly and shell $ vars
# inside the orchestrator itself stay literal).
sed -i "s|__DEMO_PATH__|$DEMO_PATH|g" /opt/bootstrap/orchestrator.sh
sed -i "s|__AUTO_SHUTDOWN_MINUTES__|$AUTO_SHUTDOWN_MINUTES|g" /opt/bootstrap/orchestrator.sh
chmod +x /opt/bootstrap/orchestrator.sh

# ---------------------------------------------------------------------------
# systemd service — runs the orchestrator on every boot. Once step 3's
# marker exists, ConditionPathExists (negated) makes systemd skip the run
# entirely, so this safely does nothing on later boots.
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
