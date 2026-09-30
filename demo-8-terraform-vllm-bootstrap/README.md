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

## One script change required in the repo

`03-serve-model.sh` normally ends with `tail -f ~/vllm.log`, which blocks
forever — fine for a manual terminal session, fatal for a non-interactive
systemd service (it would hang and never mark step 3 complete). This
project's orchestrator calls it with `NO_WAIT_TAIL=true`, which requires
`03-serve-model.sh` in the repo to support that flag.

**`03-serve-model-updated.sh` in this folder is the patched version** —
copy it over the existing `03-serve-model.sh` in your repo and commit it
before applying this Terraform config:

```bash
cp 03-serve-model-updated.sh /path/to/aws-aiops-demo-repo/demo-7-vllm-with-model-installation/03-serve-model.sh
cd /path/to/aws-aiops-demo-repo
git add demo-7-vllm-with-model-installation/03-serve-model.sh
git commit -m "Add NO_WAIT_TAIL support for non-interactive automation"
git push
```

Manual usage is unchanged (`./03-serve-model.sh` still streams the log as
before) — only automated callers need to set `NO_WAIT_TAIL=true`.

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
