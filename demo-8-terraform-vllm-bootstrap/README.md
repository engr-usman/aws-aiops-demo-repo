# Terraform Bootstrap: vLLM + Qwen3-8B on EC2

Provisions a `g6.xlarge` GPU instance (1x NVIDIA L4) with Terraform, clones
[`aws-aiops-demo-repo`](https://github.com/engr-usman/aws-aiops-demo-repo),
and automatically runs, inside `demo-7-vllm-with-model-installation/`:

1. `01-install-driver.sh` (installs the NVIDIA driver, then reboots)
2. `02-setup-vllm.sh` (Python venv, vLLM, CUDA toolchain fixes)
3. starts vLLM as a supervised **systemd service** (`vllm-serve.service`)

**`04-run-benchmark.sh` is intentionally NOT automated.** SSH in and run it
yourself once the server is up.

## Project layout

```
.
├── backend.tf               # S3 remote state (native locking, no DynamoDB)
├── versions.tf              # Terraform + AWS provider constraints
├── variables.tf             # region, instance type, key name, SSH CIDR, ...
├── data.tf                  # Ubuntu AMI + default VPC/subnet lookups
├── security_group.tf        # SSH only; port 8000 is not opened
├── main.tf                  # the EC2 instance
├── outputs.tf               # IP, ssh command, helper commands
├── terraform.tfvars.example # copy to terraform.tfvars and fill in
└── templates/
    └── user_data.sh.tpl     # cloud-init: orchestrator + vLLM service
```

## Prerequisites

- Terraform >= 1.10 (needed for S3 native state locking, `use_lockfile`)
- AWS credentials configured locally (`aws sts get-caller-identity` shows
  which account Terraform will use)
- An **existing EC2 key pair** in the target region
- GPU quota: "Running On-Demand G and VT instances" must allow at least
  4 vCPUs (`g6.xlarge` uses 4)
- **Edit `backend.tf`** before `terraform init`: the bucket name and key
  there belong to the original author. Point them at your own S3 bucket
  (backend blocks cannot use variables).

## Usage

```bash
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars: key_name and allowed_ssh_cidr (your own IP/32)

terraform init
terraform plan      # check that the ingress rule shows YOUR cidr
terraform apply
```

Then watch the bootstrap:

```bash
ssh -i /path/to/your-key.pem ubuntu@$(terraform output -raw public_ip)
tail -f /var/log/bootstrap.log     # driver, reboot, setup, service start
tail -f ~/vllm.log                 # wait for "Application startup complete"
```

Allow roughly 10-15 minutes end to end (driver install and reboot, Python
and vLLM install, model download, compile and warmup). This is an
approximate figure and varies with download speed.

When you are done:

```bash
terraform destroy
```

This terminates the instance and deletes the root EBS volume
(`delete_on_termination = true`), so nothing keeps billing.

## How the bootstrap works

### Why a plain user-data script is not enough

`01-install-driver.sh` ends with `sudo reboot`. A normal cloud-init
`user_data` script dies at that point and never resumes, because bash does
not survive a reboot. So the bootstrap uses a small state machine:

1. `user_data` (runs once, first boot) clones the repo, writes the vLLM
   service files, and installs an **orchestrator script** as a systemd
   service that runs on **every boot**.
2. The orchestrator checks marker files in `/opt/bootstrap/state/`:
   - no `01-driver-done` yet: run `01-install-driver.sh` (it reboots the
     instance when finished).
   - after the reboot the orchestrator runs again, sees `nvidia-smi`
     working, writes `01-driver-done`, and runs `02-setup-vllm.sh`.
   - then it runs `systemctl start vllm-serve.service` and writes
     `03-serve-started`.
3. Once `03-serve-started` exists, the unit's
   `ConditionPathExists=!/opt/bootstrap/state/03-serve-started` makes the
   orchestrator a silent no-op on later boots.

Progress is logged to `/var/log/bootstrap.log`.

### vLLM runs as its own systemd service

An earlier version started vLLM with `nohup ... &` inside the orchestrator.
That failed: when a oneshot service finishes, systemd's default
`KillMode=control-group` kills **every** process in its cgroup, including
`nohup`'d children. `nohup` only guards against `SIGHUP`, not that cleanup,
so vLLM was killed moments after it started and never wrote a log line.

The fix is an independent unit, `vllm-serve.service`:

- `/opt/bootstrap/start-vllm.sh` resolves `CUDA_HOME` dynamically, sets
  `VLLM_USE_FLASHINFER_SAMPLER=0`, then `exec`s `vllm serve`, so systemd
  tracks the real process.
- The unit is `Type=simple` with `Restart=on-failure`, and appends its
  output to `~/vllm.log`.
- It is `enable`d, so the server also comes back after a reboot of the
  instance. It is only started once steps 1 and 2 are done.

Managing it:

```bash
systemctl status vllm-serve
sudo systemctl restart vllm-serve
sudo systemctl stop vllm-serve     # frees port 8000 for a manual run
tail -f ~/vllm.log
```

Settings (model, port, context length, GPU memory share) are the
`Environment=` lines in `/etc/systemd/system/vllm-serve.service`. After
editing: `sudo systemctl daemon-reload && sudo systemctl restart vllm-serve`.

`03-serve-model.sh` in the demo repo still works for manual, ad-hoc runs
(different model, `--enforce-eager`, ...). Stop the service first so the two
do not compete for port 8000.

## Testing the API

vLLM exposes an OpenAI-compatible API on port 8000. The commands below were
run on the instance itself (`localhost`) against `Qwen/Qwen3-8B`. They need
`jq` (`sudo apt install -y jq`).

### Health and model check

```bash
curl http://localhost:8000/health
curl -s http://localhost:8000/v1/models | jq
```

The models response lists `Qwen/Qwen3-8B` with `"max_model_len": 8192`,
which matches the `MAX_MODEL_LEN` set in the service.

### A first prompt

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [
      {"role": "user", "content": "Explain what a KV cache is in 3 sentences."}
    ],
    "max_tokens": 300
  }' | jq -r '.choices[0].message.content'
```

### Qwen3 thinking mode

Qwen3 reasons by default: it generates a `<think>...</think>` block before
the answer, which costs extra tokens and latency. For short questions turn
it off per request with `chat_template_kwargs`:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role": "user", "content": "Explain what a KV cache is in 3 sentences."}],
    "max_tokens": 300,
    "chat_template_kwargs": {"enable_thinking": false}
  }' | jq -r '.choices[0].message.content'
```

If you leave thinking on, use a large `max_tokens` (1000 or more), or the
answer can be cut off while the model is still reasoning.

### System prompt and temperature

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [
      {"role": "system", "content": "You are a senior DevOps engineer. Answer briefly and practically."},
      {"role": "user", "content": "How do I debug a pod stuck in CrashLoopBackOff?"}
    ],
    "max_tokens": 400,
    "temperature": 0.3,
    "chat_template_kwargs": {"enable_thinking": false}
  }' | jq -r '.choices[0].message.content'
```

Note: the model's own `generation_config.json` sets default sampling
(`temperature 0.6, top_k 20, top_p 0.95`), which vLLM applies unless a
request overrides it.

### Streaming

Tokens arrive one by one as server-sent events and the stream ends with
`data: [DONE]`:

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role": "user", "content": "Write a haiku about GPUs."}],
    "max_tokens": 100,
    "stream": true,
    "chat_template_kwargs": {"enable_thinking": false}
  }'
```

### Token usage

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-8B",
    "messages": [{"role": "user", "content": "List 5 Terraform best practices."}],
    "max_tokens": 300,
    "chat_template_kwargs": {"enable_thinking": false}
  }' | jq '.usage'
```

Observed output:

```json
{
  "prompt_tokens": 20,
  "total_tokens": 320,
  "completion_tokens": 300
}
```

`completion_tokens` equals `max_tokens` here, so the answer was **cut off**
rather than finished. To check, read `.choices[0].finish_reason`: `length`
means truncated, `stop` means the model finished on its own. Raise
`max_tokens` for list-style answers.

### Several prompts in a loop

```bash
for q in "What is MLOps?" "What is LLMOps?" "What is AIOps?"; do
  echo "=== $q ==="
  curl -s http://localhost:8000/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"Qwen/Qwen3-8B\",\"messages\":[{\"role\":\"user\",\"content\":\"$q Answer in 2 sentences.\"}],\"max_tokens\":150,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
    | jq -r '.choices[0].message.content'
done
```

All three returned complete two-sentence answers.

### From your laptop: SSH tunnel

Port 8000 is not opened in the security group, and vLLM has no
authentication by default. The simplest safe route is an SSH tunnel. On your
laptop (leave it running):

```bash
ssh -i /path/to/your-key.pem -N -L 8000:localhost:8000 ubuntu@$(terraform output -raw public_ip)
```

In a second terminal on the laptop, the same `curl` commands work against
`localhost:8000`. If the laptop's port 8000 is taken, use
`-L 8001:localhost:8000` and curl `localhost:8001`.

### Calling the instance's public IP directly

Running `curl http://<public-ip>:8000/...` **from inside the instance** is
not a test of outside reachability, because the request starts on the
instance itself. To check whether port 8000 is reachable from elsewhere, run
this from your laptop:

```bash
curl --max-time 5 http://<public-ip>:8000/health
```

and inspect the rules:

```bash
aws ec2 describe-security-groups --group-ids <sg-id> \
  --query "SecurityGroups[].IpPermissions"
```

If you decide to open 8000, restrict it to your own IP/32 and require an API
key (vLLM's `--api-key` option, added to `start-vllm.sh`; not exercised in
this project). An unauthenticated GPU endpoint open to the internet will be
used by strangers.

## Version note

`02-setup-vllm.sh` runs an unpinned `pip install vllm`, so the version
depends on the day you run it. The earlier benchmark in the parent project
ran vLLM 0.30.0; a later bootstrap run reported `vllm-0.31.0` in the API's
`system_fingerprint` field. If you want benchmark numbers that can be
compared across runs, pin the version (`pip install vllm==X.Y.Z`).

## Cost control

- `terraform destroy` is the real stop mechanism. Run it when the session
  ends.
- `auto_shutdown_minutes` (default 180) is a safety net only: once the
  bootstrap completes, the instance schedules `shutdown -h +N`. That stops
  the instance (EBS still bills) if you forget; it does not replace
  `terraform destroy`. Cancel it with `sudo shutdown -c` if a session needs
  to run longer.

## Troubleshooting

- **Bootstrap seems stuck:** `tail -f /var/log/bootstrap.log`. If it stopped
  right after "Step 1: starting driver install", the instance is mid-reboot;
  wait about a minute and reconnect.
- **Check the orchestrator:** `systemctl status bootstrap-orchestrator.service`.
  `inactive (dead)` with `status=0/SUCCESS` is normal for a finished oneshot
  service. For the running server use `systemctl status vllm-serve`.
- **A step failed and its marker was not written:** fix the cause, then
  `sudo systemctl start bootstrap-orchestrator.service`; it resumes at the
  failed step.
- **Errors from the demo scripts** (missing headers, `nvcc` path, FlashInfer
  JIT failures): see the parent project's README; `02-setup-vllm.sh` already
  contains those fixes.
- **`templatefile()` errors when editing `user_data.sh.tpl`:** every `${...}`
  in the file is read by Terraform. Variables meant for bash (for example
  `${MODEL:-default}`) must be written as `$${...}`.
- **AWS rejects a security group description:** these fields accept ASCII
  only; avoid em-dashes and smart quotes.
