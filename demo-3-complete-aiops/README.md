# AIOps Self-Healing Infrastructure on AWS

An automated self-healing infrastructure pipeline built on AWS that detects Nginx service failures in real time, performs AI-powered root-cause analysis using Amazon Bedrock (Claude), and automatically remediates the issue via AWS Systems Manager — all without any human intervention or SSH access.

This project was originally demonstrated at an AWS Community event as part of a two-demo AIOps series.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Demo Series](#demo-series)
- [How It Works](#how-it-works)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [AWS Services Used](#aws-services-used)
- [Setup Guide](#setup-guide)
  - [Step 1 — Launch EC2 Instance](#step-1--launch-ec2-instance)
  - [Step 2 — Install and Configure Nginx](#step-2--install-and-configure-nginx)
  - [Step 3 — Install CloudWatch Agent](#step-3--install-cloudwatch-agent)
  - [Step 4 — Configure IAM Roles](#step-4--configure-iam-roles)
  - [Step 5 — Deploy the Lambda Function](#step-5--deploy-the-lambda-function)
  - [Step 6 — Configure CloudWatch Subscription Filter](#step-6--configure-cloudwatch-subscription-filter)
- [Environment Variables](#environment-variables)
- [Maintenance Mode](#maintenance-mode)
- [Testing](#testing)
- [Lambda Execution Flow](#lambda-execution-flow)
- [Log Groups](#log-groups)
- [Project Structure](#project-structure)
- [Key Design Decisions](#key-design-decisions)
- [Lessons Learned](#lessons-learned)

---

## Architecture Overview

```
EC2 Instance (Nginx)
        │
        │  error logs written to disk
        ▼
CloudWatch Agent
        │
        │  ships logs in real time
        ▼
CloudWatch Log Group: /aiops/ec2/nginx/error-logs
        │
        │  Subscription Filter (blank pattern — all events)
        ▼
AWS Lambda Function
        │
        ├─ Pre-check 2.5: Startup-only logs? ──► Early exit (no Bedrock call)
        │
        ├─ Pre-check 2.6: Service in maintenance? ──► Early exit (no Bedrock call)
        │
        ├─ Amazon Bedrock (Claude Sonnet) ──► AI root-cause analysis + action key
        │
        ├─ CloudWatch Log Group: /aiops/lambda/nginx-analysis ──► Persist analysis
        │
        └─ AWS SSM Run Command ──► Execute fix on EC2 (no SSH)
                │
                ▼
        Nginx: active (running) ✅
```

---

## Demo Series

This repository contains **Demo 2** of a two-part AIOps series presented at an AWS Community event.

| Demo | Title | Description |
|------|-------|-------------|
| Demo 1 | Manual AI Log Analyzer | User pastes logs into a web UI → API Gateway → Lambda → Bedrock → structured analysis displayed on screen |
| Demo 2 | Automated AIOps Pipeline *(this repo)* | Logs flow automatically from EC2 → CloudWatch → Lambda → Bedrock → SSM auto-remediation |

---

## How It Works

1. **Nginx** runs on an EC2 instance and writes error logs to `/var/log/nginx/error.log`.
2. The **CloudWatch Agent** ships those logs to the `/aiops/ec2/nginx/error-logs` log group in near real time.
3. A **CloudWatch Subscription Filter** forwards every new log batch to the Lambda function.
4. The **Lambda function** runs a two-stage pre-check pipeline before calling Bedrock:
   - **Pre-check 2.5** — if logs contain only Nginx startup messages (no shutdown or error signals), the function exits early. This prevents a remediation loop after Nginx is restarted.
   - **Pre-check 2.6** — if the detected service appears in the `MAINTENANCE_MODE_SERVICES` environment variable, the function exits early and skips both Bedrock and SSM. This supports planned maintenance windows.
5. If both pre-checks pass, logs are sent to **Amazon Bedrock (Claude Sonnet)** for AI-powered root-cause analysis. Bedrock returns a structured JSON response containing the issue, severity, root cause, fix, and a `remediation_action` key.
6. The analysis result is written to a dedicated **CloudWatch log group** (`/aiops/lambda/nginx-analysis`) for audit and observability.
7. Based on the `remediation_action` key, the Lambda function sends a shell command to the EC2 instance via **AWS SSM Run Command** — no SSH, no key pairs required.
8. Nginx is restarted automatically. The post-fix status is captured and logged.

---

## Features

- **Fully automated** — zero human intervention required for common Nginx failures
- **AI-powered analysis** — Amazon Bedrock (Claude) identifies root cause, severity, fix, and prevention steps
- **SSH-less remediation** — AWS SSM Run Command eliminates key pair management
- **Maintenance mode** — set an environment variable to prevent auto-remediation during planned maintenance windows; supports multiple services
- **Startup log filtering** — intelligent pre-check avoids unnecessary Bedrock calls after successful remediation
- **Structured audit logs** — every analysis and remediation is persisted to CloudWatch for review
- **Cost-efficient** — Bedrock is only called when genuinely needed
- **Extensible** — the service detection and remediation action map are designed to support additional services (Apache, MySQL, Node.js, etc.)

---

## Prerequisites

- An AWS account with permissions to create EC2, Lambda, IAM, CloudWatch, SSM, and Bedrock resources
- Amazon Bedrock model access enabled for **Claude Sonnet** (`global.anthropic.claude-sonnet-4-6`) in `us-east-1`
- AWS CLI configured (optional, for scripted setup)

---

## AWS Services Used

| Service | Purpose |
|---------|---------|
| Amazon EC2 | Hosts the Nginx web server |
| Amazon CloudWatch Agent | Ships Nginx logs from EC2 to CloudWatch |
| Amazon CloudWatch Logs | Stores Nginx error logs and Lambda analysis results |
| CloudWatch Subscription Filter | Triggers Lambda on new log events |
| AWS Lambda | Orchestrates the analysis and remediation pipeline |
| Amazon Bedrock (Claude Sonnet) | AI-powered log analysis and remediation recommendation |
| AWS SSM Run Command | Executes remediation commands on EC2 without SSH |
| AWS IAM | Grants least-privilege permissions to EC2 and Lambda |

---

## Setup Guide

### Step 1 — Launch EC2 Instance

Launch an EC2 instance with the following configuration:

| Setting | Value |
|---------|-------|
| Name | `aiops-nginx-demo` |
| AMI | Amazon Linux 2023 |
| Instance Type | `t2.micro` (free tier eligible) |
| Security Group | Inbound: HTTP (80), HTTPS (443) |
| Key Pair | Optional — SSM is used for remote access |

> **Note:** The instance Name tag (`aiops-nginx-demo`) is used as a fallback when the Lambda function cannot extract the instance ID from the log stream name.

---

### Step 2 — Install and Configure Nginx

Connect to the instance using EC2 Instance Connect or SSM Session Manager.

```bash
# Update packages
sudo dnf update -y

# Install Nginx
sudo dnf install nginx -y

# Start and enable Nginx
sudo systemctl start nginx
sudo systemctl enable nginx

# Verify
sudo systemctl status nginx
curl http://localhost
```

---

### Step 3 — Install CloudWatch Agent

```bash
# Install the CloudWatch Agent
sudo dnf install amazon-cloudwatch-agent -y
```

Create the agent configuration file:

```bash
sudo nano /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
```

Paste the following configuration:

```json
{
  "agent": {
    "metrics_collection_interval": 60,
    "run_as_user": "root"
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/nginx/error.log",
            "log_group_name": "/aiops/ec2/nginx/error-logs",
            "log_stream_name": "{instance_id}/nginx-error",
            "retention_in_days": 7,
            "timestamp_format": "%Y/%m/%d %H:%M:%S"
          }
        ]
      }
    }
  }
}
```

Start the agent:

```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config \
    -m ec2 \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
    -s

# Verify
sudo systemctl status amazon-cloudwatch-agent
```

> **Note:** The log stream name `{instance_id}/nginx-error` is important — the Lambda function uses the instance ID embedded in the stream name to target SSM Run Command at the correct EC2 instance.

---

### Step 4 — Configure IAM Roles

#### EC2 IAM Role

Create an IAM role for EC2 with the following managed policies attached:

| Policy | Purpose |
|--------|---------|
| `CloudWatchAgentServerPolicy` | Allows the CloudWatch Agent to publish logs |
| `AmazonSSMManagedInstanceCore` | Allows SSM to reach the instance for Run Command |

Attach the role to the EC2 instance:
```
EC2 Console → Select Instance → Actions → Security → Modify IAM Role
```

#### Lambda IAM Role

Create an IAM role for Lambda with the following managed policies:

| Policy | Purpose |
|--------|---------|
| `AWSLambdaBasicExecutionRole` | Allows Lambda to write its own logs to CloudWatch |
| `CloudWatchLogsFullAccess` | Allows Lambda to write analysis results to CloudWatch |
| `AmazonBedrockFullAccess` | Allows Lambda to invoke Bedrock models |

Add the following **inline policy** for SSM and EC2 access:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SSMRunCommand",
      "Effect": "Allow",
      "Action": [
        "ssm:SendCommand",
        "ssm:GetCommandInvocation",
        "ssm:ListCommandInvocations",
        "ssm:DescribeInstanceInformation"
      ],
      "Resource": "*"
    },
    {
      "Sid": "EC2Describe",
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceStatus"
      ],
      "Resource": "*"
    }
  ]
}
```

---

### Step 5 — Deploy the Lambda Function

1. Go to **AWS Lambda Console → Create Function**
2. Configure the function:

| Setting | Value |
|---------|-------|
| Runtime | Python 3.10+ |
| Architecture | x86_64 |
| Timeout | 5 minutes |
| Memory | 256 MB |
| IAM Role | The Lambda role created in Step 4 |

3. Upload `lambda_function.py` as the function code.
4. Set the required environment variables (see [Environment Variables](#environment-variables)).

---

### Step 6 — Configure CloudWatch Subscription Filter

1. Go to **CloudWatch Console → Log Groups → `/aiops/ec2/nginx/error-logs`**
2. Select **Actions → Subscription Filters → Create Lambda Subscription Filter**
3. Configure:

| Setting | Value |
|---------|-------|
| Lambda Function | Select your deployed Lambda function |
| Filter Name | `nginx-all-events` |
| Filter Pattern | *(leave blank — match all events)* |

> **Why a blank filter pattern?** Nginx shutdown events are logged at `[notice]` level, not `[error]`. A filter pattern of `ERROR` would miss service stop events entirely. A blank pattern ensures every log batch is evaluated.

---

## Environment Variables

Configure these in **Lambda Console → Configuration → Environment Variables**:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MAINTENANCE_MODE_SERVICES` | No | *(empty)* | Comma-separated list of services under planned maintenance (e.g. `nginx` or `nginx,mysql`). When a service in this list is detected in incoming logs, Bedrock analysis and SSM remediation are both skipped. Set to empty to disable maintenance mode. |
| `ANALYSIS_LOG_GROUP` | No | `/aiops/lambda/nginx-analysis` | CloudWatch log group where Bedrock analysis results are persisted. |
| `MODEL_ID` | No | `global.anthropic.claude-sonnet-4-6` | Amazon Bedrock model ID to use for log analysis. |
| `AWS_REGION` | Auto | Set by Lambda runtime | The region in which SSM, CloudWatch, and EC2 clients operate. Automatically injected by the Lambda runtime — no manual configuration needed. |

> **Note:** The Bedrock client is hardcoded to `us-east-1` regardless of `AWS_REGION` because the cross-region inference profile (`global.*`) is hosted in `us-east-1`.

---

## Maintenance Mode

Maintenance mode allows operators to stop a service intentionally without triggering auto-remediation.

### Enable Maintenance Mode

```
Lambda Console → Configuration → Environment Variables → Edit

MAINTENANCE_MODE_SERVICES = nginx
```

For multiple services:

```
MAINTENANCE_MODE_SERVICES = nginx,mysql,apache2
```

### Disable Maintenance Mode

Set the value to empty (delete all text from the value field) and save.

### How It Works

When `MAINTENANCE_MODE_SERVICES` is set, the Lambda function scans the incoming log content for service-specific signals before calling Bedrock. If the detected service matches an entry in the maintenance list, the function returns immediately with a `maintenance_mode` status — **no Bedrock call, no SSM command**.

This design keeps Bedrock cost at zero during maintenance windows while still logging that a maintenance-mode event was detected.

---

## Testing

### Test 1 — Auto-Remediation (Normal Mode)

Ensure `MAINTENANCE_MODE_SERVICES` is empty, then stop Nginx:

```bash
sudo systemctl stop nginx
```

**Expected Lambda log output (within ~60 seconds):**

```
🚀 AIOps Self-Healing Lambda triggered
📋 Log Group  : /aiops/ec2/nginx/error-logs
📋 Events received: 11
🖥️  EC2 Instance ID: i-0xxxxxxxxxxxxxxxxx
🔍 Pre-check 2.5: shutdown/error signal detected → 'sigquit' — proceeding to Bedrock
🤖 Sending logs to Bedrock for analysis...
✅ Bedrock Analysis Complete: { "severity": "CRITICAL", "remediation_action": "nginx_service_stopped", ... }
📋 Maintenance List: Empty (no maintenance)
🔧 Starting auto-remediation for instance: i-0xxxxxxxxxxxxxxxxx
📡 SSM Ping Status: Online
✅ SSM Command sent: cmd-xxxxxxxxxxxxxxxxx
📊 Remediation Summary:
   Status    : success
   Action    : nginx_service_stopped
   Exit Code : 0
   Output    : ● nginx.service - The nginx HTTP and reverse proxy server ... Active: active (running)
```

Verify on the EC2 instance:

```bash
sudo systemctl status nginx
# Expected: active (running)
```

---

### Test 2 — Maintenance Mode

Set `MAINTENANCE_MODE_SERVICES = nginx` in the Lambda environment variables, then stop Nginx:

```bash
sudo systemctl stop nginx
```

**Expected Lambda log output:**

```
🚀 AIOps Self-Healing Lambda triggered
🔍 Detected service from logs : nginx
📋 Maintenance list           : ['nginx']
🔶 MAINTENANCE MODE (pre-Bedrock): 'nginx' is in the maintenance list — skipping Bedrock entirely
```

Nginx will **not** be restarted — the service stop is treated as intentional.

---

### Test 3 — Startup Log Filtering

After Test 1 completes, Lambda is triggered a second time by Nginx's own startup logs. Verify that the second invocation exits early without calling Bedrock:

```
✅ Pre-check 2.5 passed: startup-only logs, no shutdown signals detected
✅ Pre-check 2.5: Startup-only logs detected — service is healthy, skipping Bedrock
```

---

## Lambda Execution Flow

```
Event received
      │
      ├─ No 'awslogs' key? ──► Return 400 (not a CloudWatch event)
      │
      ▼
Decode CloudWatch payload (base64 + gzip)
      │
      ▼
Extract EC2 Instance ID from log stream name
      │
      ▼
Pre-check 2.5 — is_startup_only_logs()
      │
      ├─ True  ──► Return 200 { status: "healthy" }   (no Bedrock call)
      │
      └─ False ──► continue
                    │
                    ▼
            Pre-check 2.6 — maintenance mode check
                    │
                    ├─ Service in MAINTENANCE_MODE_SERVICES?
                    │         └─ Yes ──► Return 200 { status: "maintenance_mode" }
                    │
                    └─ No ──► continue
                                │
                                ▼
                    Bedrock analysis (Claude Sonnet)
                                │
                                ▼
                    Log analysis to /aiops/lambda/nginx-analysis
                                │
                                ▼
                    Evaluate remediation_action
                                │
                    ┌───────────┼───────────────────┐
                    │           │                   │
              maintenance    action=none      proceed to SSM
                    │           │                   │
              log + skip    log + skip        SSM Run Command
                                                    │
                                              log result
```

---

## Log Groups

| Log Group | Description |
|-----------|-------------|
| `/aiops/ec2/nginx/error-logs` | Nginx error logs shipped from EC2 by the CloudWatch Agent |
| `/aws/lambda/ai-cloudwatch-log-analyzer` | Lambda function execution logs (auto-created by Lambda) |
| `/aiops/lambda/nginx-analysis` | Structured Bedrock analysis results, persisted per instance per day |

---

## Project Structure

```
.
├── lambda_function.py      # Main Lambda function — analysis and remediation pipeline
└── README.md               # This file
```

---

## Key Design Decisions

**SSH-less remediation via SSM**
AWS Systems Manager Run Command is used instead of SSH to execute remediation commands on the EC2 instance. This eliminates key pair management, improves security posture, and is consistent with AWS best practices for operational access.

**Blank CloudWatch subscription filter pattern**
Nginx service stop events are logged at `[notice]` level, not `[error]`. Using a filter pattern of `ERROR` would silently miss all service stop events. A blank pattern ensures the Lambda function receives every log batch and applies its own logic to determine whether action is required.

**Two-stage pre-check before Bedrock**
Bedrock is an external AI service — calling it unnecessarily adds latency and cost. Pre-check 2.5 (startup log detection) and Pre-check 2.6 (maintenance mode) act as fast gates that short-circuit the pipeline before any API call to Bedrock is made.

**Generic service detection design**
The `detect_service_from_logs()` function is designed to be extended with additional services (Apache, MySQL, Node.js, PM2, Java, etc.) without modifying the main handler logic. The maintenance mode pattern works for any service name added to both the detection function and the `MAINTENANCE_MODE_SERVICES` environment variable.

**Explicit Bedrock prompt rules**
LLM-based automation requires unambiguous prompts. Early iterations showed that without explicit rules, Bedrock would correctly interpret a graceful Nginx shutdown as intentional and return `remediation_action: "none"`. The prompt was updated with mandatory, signal-based rules to ensure deterministic output for all scenarios.

---

## Lessons Learned

- **Log level assumptions are a common failure point.** Always verify what level a service actually logs at before setting subscription filter patterns. Nginx shutdown events are `[notice]`, not `[error]`.
- **LLM remediation logic requires explicit rules.** Nuanced scenarios (such as graceful shutdowns) must be explicitly handled with prescribed outputs. Leaving them to model inference produces correct but operationally unhelpful responses.
- **Bedrock's "correct" answer is not always the operationally correct answer.** A graceful shutdown is technically fine from Nginx's perspective, but unacceptable from a self-healing infrastructure perspective. The prompt must encode operational intent, not just technical correctness.
- **Region mismatches are a silent failure mode.** SSM and EC2 clients must operate in the same region as the target instance. The Bedrock cross-region inference profile requires `us-east-1` regardless of the Lambda function's region.