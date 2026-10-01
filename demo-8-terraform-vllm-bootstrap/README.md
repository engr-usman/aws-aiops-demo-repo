# Terraform Bootstrap: vLLM + Qwen3-8B on EC2

Provisions a `g6.xlarge` GPU instance, clones
[`aws-aiops-demo-repo`](https://github.com/engr-usman/aws-aiops-demo-repo),
and automatically runs `01-install-driver.sh` → (reboot) → `02-setup-vllm.sh`
→ `03-serve-model.sh` (backgrounded) inside
`demo-7-vllm-with-model-installation/`.

**`04-run-benchmark.sh` is intentionally NOT automated** — SSH in and run it
yourself once the server is confirmed up.

## Why this needed more than a plain user-data script

`01-install-driver.sh` ends with `sudo reboot`. A normal cloud-init
`user_data` script dies right there and never resumes — bash doesn't survive
a reboot. This project works around that with a small state machine:

1. `user_data` (runs once, first boot) clones the repo and installs an
   **orchestrator script** as a `systemd` service that's set to run **on
   every boot**.
2. The orchestrator checks marker files under `/opt/bootstrap/state/` to
   know which step is next:
   - No `01-driver-done` marker yet → run `01-install-driver.sh` (which
     reboots the instance when it finishes installing the driver).
   - After the reboot, the orchestrator runs again automatically (systemd,
     `WantedBy=multi-user.target`), sees the driver now loads
     (`nvidia-smi` succeeds), marks step 1 done, and proceeds to
     `02-setup-vllm.sh`.
   - Then `03-serve-model.sh` is launched in the background
     (`NO_WAIT_TAIL=true`, see below) and its own marker is set.
3. Once the step-3 marker exists, the systemd unit's
   `ConditionPathExists=!/opt/bootstrap/state/03-serve-started` makes it a
   no-op on every future boot — it doesn't error, it just skips.

All progress is logged to `/var/log/bootstrap.log` on the instance.

## vLLM runs as a proper systemd service, not a backgrounded script

An earlier version of this bootstrap launched vLLM via `nohup ... &` inside
the orchestrator. That approach had a real bug: when the oneshot
`bootstrap-orchestrator.service` finishes and deactivates, systemd's default
`KillMode=control-group` kills **every** process in that service's cgroup —
including `nohup`'d background children. `nohup` only protects against
`SIGHUP`, not systemd's cgroup cleanup. The result was a vLLM process that
got silently killed moments after starting, before it even wrote its first
log line.

**Fix: vLLM is defined as its own independent systemd unit,
`vllm-serve.service`**, entirely separate from the orchestrator's cgroup:

- `user_data` writes `/opt/bootstrap/start-vllm.sh` (resolves `CUDA_HOME`
  dynamically, then `exec`s `vllm serve` so systemd tracks the real process)
  and `/etc/systemd/system/vllm-serve.service` (a normal `Type=simple`
  service — `Restart=on-failure`, logs appended to `~/vllm.log`, same file
  you'd tail in a manual run).
- The unit is `enable`d immediately (so it also auto-starts after any future
  reboot of the instance — a nice side effect), but not started yet, since
  the driver and Python venv don't exist on first boot.
- The orchestrator's Step 3 is now just `systemctl start vllm-serve.service`
  — no backgrounding, no `nohup`, no cgroup-kill risk.

Managing the server once it's running:
```bash
systemctl status vllm-serve      # is it up, how long, recent restarts
sudo systemctl stop vllm-serve     # stop it (e.g. to free port 8000 for a
                                    # manual run with different flags)
sudo systemctl restart vllm-serve
tail -f ~/vllm.log                  # same log file as before
```

`03-serve-model.sh` in the repo is unchanged and still useful for manual,
ad-hoc runs (different model, `--enforce-eager`, etc.) — just
`sudo systemctl stop vllm-serve` first so it isn't competing for port 8000.

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: your key_name and allowed_ssh_cidr (your own IP/32)

terraform init
terraform plan
terraform apply
```

Then:

```bash
# Watch bootstrap progress (driver install, reboot, setup, server start):
ssh -i /path/to/your-key.pem ubuntu@$(terraform output -raw public_ip)
tail -f /var/log/bootstrap.log

# Once you see "Bootstrap complete" in that log, check the server itself:
tail -f ~/vllm.log
# wait for "Application startup complete"

# Run the benchmark yourself:
cd ~/aws-aiops-demo-repo/demo-7-vllm-with-model-installation
./04-run-benchmark.sh
```

**When done:**
```bash
terraform destroy
```
This terminates the instance and deletes the root EBS volume
(`delete_on_termination = true`), so nothing is left billing.

## Cost control notes

- `terraform destroy` is the real stop mechanism — always run it when
  you're done for the session.
- `auto_shutdown_minutes` (default 180) is a **safety net only**: once
  bootstrap fully completes, the instance schedules its own shutdown via
  `shutdown -h +N`. This stops the instance (EBS still bills) if you forget
  — it does not replace `terraform destroy`.
- Expect roughly 5-10 minutes total for driver install + reboot + vLLM
  setup + model download before the server is ready, based on the timings
  seen in earlier manual runs of these same scripts.

## Troubleshooting

- **Bootstrap seems stuck:** SSH in, `tail -f /var/log/bootstrap.log`. If
  it stopped right after "Step 1: starting driver install", the instance
  is mid-reboot or the reboot hasn't happened yet — wait ~60 seconds and
  reconnect.
- **`systemctl status bootstrap-orchestrator.service`** shows the last run's
  outcome (success / failure / skipped-due-to-condition).
- **A step failed and the marker wasn't created:** re-run manually with
  `sudo systemctl start bootstrap-orchestrator.service`, then check the log.
- Deeper toolchain issues (missing headers, `nvcc` path, FlashInfer JIT
  failures) are already documented and fixed in the parent repo's
  `README.md` — the scripts here are the same ones, unchanged apart from
  the `NO_WAIT_TAIL` addition.
