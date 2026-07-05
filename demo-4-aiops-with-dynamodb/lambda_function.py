import json
import boto3
import base64
import gzip
import re
import time
import os
import uuid
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIGURATION — loaded from environment variables
# ─────────────────────────────────────────────
AWS_REGION         = os.environ.get('AWS_REGION', 'eu-central-1')
ANALYSIS_LOG_GROUP = os.environ.get('ANALYSIS_LOG_GROUP', '/aiops/lambda/nginx-analysis')
MODEL_ID           = os.environ.get('MODEL_ID', 'global.anthropic.claude-sonnet-4-6')
SNS_TOPIC_ARN      = os.environ.get('SNS_TOPIC_ARN', '')
SLACK_WEBHOOK_URL  = os.environ.get('SLACK_WEBHOOK_URL', '')
DYNAMODB_TABLE     = os.environ.get('DYNAMODB_TABLE', 'aiops-incidents')
TTL_DAYS           = int(os.environ.get('TTL_DAYS', '90'))

# ─────────────────────────────────────────────
# AWS CLIENTS
# Note: Bedrock stays in us-east-1 (cross-region inference profile).
#       All other clients use the Lambda function's own region.
# ─────────────────────────────────────────────
bedrock  = boto3.client('bedrock-runtime', region_name='us-east-1')
ssm      = boto3.client('ssm',             region_name=AWS_REGION)
logs     = boto3.client('logs',            region_name=AWS_REGION)
ec2      = boto3.client('ec2',             region_name=AWS_REGION)
sns      = boto3.client('sns',             region_name=AWS_REGION)
dynamodb = boto3.resource('dynamodb',      region_name=AWS_REGION)

# ─────────────────────────────────────────────
# REMEDIATION ACTION MAP
# ─────────────────────────────────────────────
REMEDIATION_ACTIONS = {
    "nginx_service_stopped": {
        "command": "sudo systemctl start nginx && sudo systemctl status nginx",
        "description": "Starting Nginx service"
    },
    "nginx_service_failed": {
        "command": "sudo systemctl restart nginx && sudo systemctl status nginx",
        "description": "Restarting failed Nginx service"
    },
    "nginx_config_error": {
        "command": "sudo nginx -t && sudo systemctl reload nginx",
        "description": "Testing and reloading Nginx config"
    },
    "nginx_port_conflict": {
        "command": "sudo systemctl stop nginx && sudo fuser -k 80/tcp && sudo systemctl start nginx",
        "description": "Resolving port conflict and restarting Nginx"
    },
    "disk_full": {
        "command": "sudo journalctl --vacuum-size=100M && sudo find /var/log/nginx -name '*.log' -mtime +7 -delete",
        "description": "Clearing old logs to free disk space"
    },
    "permission_error": {
        "command": "sudo chown -R nginx:nginx /var/log/nginx && sudo chmod 755 /var/log/nginx",
        "description": "Fixing Nginx file permissions"
    },
    "general_restart": {
        "command": "sudo systemctl restart nginx && sudo systemctl status nginx",
        "description": "General Nginx service restart"
    }
}

# Severity → alert metadata
SEVERITY_META = {
    "CRITICAL": {"emoji": "🔴", "label": "CRITICAL", "color": "#C0392B"},
    "HIGH":     {"emoji": "🟠", "label": "HIGH",     "color": "#E67E22"},
    "MEDIUM":   {"emoji": "🟡", "label": "MEDIUM",   "color": "#F1C40F"},
    "LOW":      {"emoji": "🟢", "label": "LOW",      "color": "#27AE60"},
}


# ═════════════════════════════════════════════
# MAIN HANDLER
# ═════════════════════════════════════════════
def lambda_handler(event, context):
    print("🚀 AIOps Self-Healing Lambda triggered")
    print(f"📥 Event received: {json.dumps(event)[:200]}")

    # Track pipeline start time for MTTR calculation
    pipeline_start = time.time()

    try:
        # Guard: only process CloudWatch Logs subscription filter events
        if 'awslogs' not in event:
            print("⚠️  Not a CloudWatch Logs event — possibly manual test or wrong trigger")
            return {
                "statusCode": 400,
                "body": "Event does not contain 'awslogs' data."
            }

        # ── Step 1: Decode CloudWatch log payload ──
        log_data   = decode_cloudwatch_logs(event)
        log_events = log_data.get('logEvents', [])
        log_group  = log_data.get('logGroup', 'unknown')
        log_stream = log_data.get('logStream', 'unknown')

        print(f"📋 Log Group  : {log_group}")
        print(f"📋 Log Stream : {log_stream}")
        print(f"📋 Events received: {len(log_events)}")

        if not log_events:
            return {"statusCode": 200, "body": "No log events to process"}

        combined_logs = "\n".join([e.get('message', '') for e in log_events])

        # ── Step 2: Extract EC2 instance ID ──
        instance_id = extract_instance_id(log_stream, log_group)
        print(f"🖥️  EC2 Instance ID: {instance_id}")

        # ── Step 2.5: Pre-check — startup-only logs? Skip Bedrock ──
        if is_startup_only_logs(combined_logs):
            print("✅ Pre-check 2.5: Startup-only logs — skipping Bedrock")
            return {
                "statusCode": 200,
                "body": json.dumps({"status": "healthy", "reason": "Startup-only logs"})
            }

        # ── Step 2.6: Pre-check — maintenance mode? Skip Bedrock ──
        maintenance_env      = os.environ.get('MAINTENANCE_MODE_SERVICES', '').strip()
        maintenance_services = []
        if maintenance_env and maintenance_env.lower() != 'none':
            maintenance_services = [s.strip().lower() for s in maintenance_env.split(',') if s.strip()]

        if maintenance_services:
            detected_service = detect_service_from_logs(combined_logs)
            print(f"🔍 Detected service: {detected_service} | Maintenance list: {maintenance_services}")

            if detected_service and detected_service in maintenance_services:
                print(f"🔶 MAINTENANCE MODE: '{detected_service}' — skipping Bedrock entirely")

                maintenance_analysis = {
                    "issue":              f"{detected_service.capitalize()} stopped during planned maintenance",
                    "severity":           "LOW",
                    "root_cause":         f"Service '{detected_service}' was intentionally stopped.",
                    "fix":                "No action taken — maintenance window is active.",
                    "remediation_action": "none",
                    "prevention":         "Remove service from MAINTENANCE_MODE_SERVICES when done.",
                    "estimated_impact":   "Planned downtime during maintenance window."
                }
                maintenance_remediation = {
                    "status": "maintenance_mode",
                    "reason": f"'{detected_service}' is under planned maintenance."
                }

                # Save to DynamoDB
                save_incident_to_dynamodb(
                    analysis=maintenance_analysis,
                    remediation_result=maintenance_remediation,
                    instance_id=instance_id,
                    log_group=log_group,
                    pipeline_start=pipeline_start
                )

                send_incident_alert(maintenance_analysis, maintenance_remediation, instance_id, log_group)
                send_slack_alert(maintenance_analysis, maintenance_remediation, instance_id, log_group)

                return {
                    "statusCode": 200,
                    "body": json.dumps({"status": "maintenance_mode"})
                }

        # ── Step 3: Bedrock AI analysis ──
        print("🤖 Sending logs to Bedrock for analysis...")
        analysis = analyze_with_bedrock(combined_logs, instance_id)
        print(f"✅ Bedrock Analysis: {json.dumps(analysis, indent=2)}")

        # ── Step 4: Persist analysis to CloudWatch ──
        log_analysis_to_cloudwatch(analysis, instance_id, combined_logs)

        # ── Step 5: Remediation decision ──
        remediation_action = analysis.get('remediation_action', 'none')
        affected_service   = detect_affected_service(remediation_action)
        remediation_result = {}

        print(f"🔧 Remediation Action : {remediation_action}")
        print(f"🛠️  Affected Service   : {affected_service}")
        print(f"📋 Maintenance List   : {maintenance_services or 'Empty'}")

        if affected_service and affected_service.lower() in maintenance_services:
            print(f"🔶 MAINTENANCE MODE: '{affected_service}' — skipping remediation")
            remediation_result = {
                "status": "maintenance_mode",
                "reason": f"'{affected_service}' is under planned maintenance.",
                "action_would_have_been": remediation_action
            }
            log_remediation_result(remediation_result, instance_id)

        elif remediation_action == 'none':
            print("ℹ️  No remediation needed — service is healthy")
            remediation_result = {
                "status": "skipped",
                "reason": "Bedrock determined no remediation required"
            }

        elif instance_id is None:
            print("⚠️  Cannot remediate — instance ID not found")
            remediation_result = {
                "status": "failed",
                "reason": "EC2 instance ID could not be determined"
            }

        else:
            print(f"🔧 Starting auto-remediation for: {instance_id}")
            remediation_result      = perform_remediation(instance_id, analysis)
            analysis['remediation'] = remediation_result
            log_remediation_result(remediation_result, instance_id)

        # ── Step 5.5: Save incident to DynamoDB ──
        save_incident_to_dynamodb(
            analysis=analysis,
            remediation_result=remediation_result,
            instance_id=instance_id,
            log_group=log_group,
            pipeline_start=pipeline_start
        )

        # ── Step 6: Send alerts ──
        send_incident_alert(analysis, remediation_result, instance_id, log_group)
        send_slack_alert(analysis, remediation_result, instance_id, log_group)

        analysis['remediation'] = remediation_result
        return {"statusCode": 200, "body": json.dumps(analysis)}

    except Exception as e:
        print(f"❌ Lambda error: {str(e)}")
        raise


# ═════════════════════════════════════════════
# DYNAMODB INCIDENT STORAGE
# ═════════════════════════════════════════════

def save_incident_to_dynamodb(analysis, remediation_result, instance_id, log_group, pipeline_start):
    """
    Persist a structured incident record to DynamoDB for historical
    tracking, MTTR calculation, and trend analysis.

    Schema:
      Partition key : instance_id (groups all incidents per server)
      Sort key      : timestamp   (enables time-range queries)

    TTL field automatically removes records after TTL_DAYS days,
    keeping the table clean without manual maintenance.

    Never raises — storage failure must not crash the pipeline.
    """
    try:
        table = dynamodb.Table(DYNAMODB_TABLE)

        # Calculate MTTR in milliseconds (pipeline start → now)
        pipeline_end    = time.time()
        mttr_ms         = int((pipeline_end - pipeline_start) * 1000)

        # TTL — Unix timestamp TTL_DAYS from now
        ttl_timestamp   = int(pipeline_end) + (TTL_DAYS * 24 * 60 * 60)

        # Unique incident ID
        incident_id     = str(uuid.uuid4())

        # ISO timestamp for sort key
        timestamp       = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        # Detect service name
        service = detect_affected_service(analysis.get('remediation_action', 'none')) or 'unknown'

        item = {
            # Keys
            'instance_id':         instance_id or 'unknown',
            'timestamp':           timestamp,

            # Identifiers
            'incident_id':         incident_id,
            'service':             service,
            'log_group':           log_group,

            # Bedrock analysis
            'issue':               analysis.get('issue', 'N/A'),
            'severity':            analysis.get('severity', 'UNKNOWN'),
            'root_cause':          analysis.get('root_cause', 'N/A'),
            'fix':                 analysis.get('fix', 'N/A'),
            'prevention':          analysis.get('prevention', 'N/A'),
            'estimated_impact':    analysis.get('estimated_impact', 'N/A'),
            'remediation_action':  analysis.get('remediation_action', 'none'),
            'model_used':          analysis.get('model_used', MODEL_ID),

            # Remediation outcome
            'remediation_status':  remediation_result.get('status', 'unknown'),
            'ssm_command_id':      remediation_result.get('ssm_command_id', 'N/A'),
            'exit_code':           str(remediation_result.get('exit_code', 'N/A')),

            # Performance
            'mttr_ms':             mttr_ms,

            # TTL — auto-delete after TTL_DAYS days
            'ttl':                 ttl_timestamp,
        }

        table.put_item(Item=item)
        print(f"💾 Incident saved to DynamoDB | ID: {incident_id} | MTTR: {mttr_ms}ms | Severity: {item['severity']}")

    except Exception as e:
        # Storage failure must never crash the main pipeline
        print(f"⚠️  Failed to save incident to DynamoDB: {str(e)}")


# ═════════════════════════════════════════════
# SNS INCIDENT ALERT (unchanged from Demo 4)
# ═════════════════════════════════════════════

def send_incident_alert(analysis, remediation_result, instance_id, log_group):
    """Publish a formatted plain-text incident alert to SNS/email."""
    if not SNS_TOPIC_ARN:
        print("⚠️  SNS_TOPIC_ARN not set — skipping email alert")
        return

    severity   = analysis.get('severity', 'UNKNOWN')
    meta       = SEVERITY_META.get(severity, {"emoji": "⚪", "label": severity})
    rem_status = remediation_result.get('status', 'unknown')
    timestamp  = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    status_lines = {
        "success":          "✅ Auto-remediated — service restarted successfully",
        "maintenance_mode": "🔶 Maintenance mode — auto-remediation skipped",
        "skipped":          "ℹ️  No action needed — service is healthy",
        "failed":           "❌ Remediation FAILED — manual intervention required",
        "timeout":          "⏰ Remediation timed out — please check manually",
    }
    status_line = status_lines.get(rem_status, f"Status: {rem_status}")
    subject     = f"{meta['emoji']} AIOps {meta['label']}: {analysis.get('issue', 'Incident Detected')[:60]}"

    body = f"""
{'='*60}
  AIOps INCIDENT ALERT — {meta['label']}
{'='*60}

INCIDENT SUMMARY
  Issue       : {analysis.get('issue', 'N/A')}
  Severity    : {severity}
  Timestamp   : {timestamp}
  Instance ID : {instance_id or 'unknown'}
  Log Group   : {log_group}

AI ANALYSIS (Amazon Bedrock — Claude Sonnet)
  Root Cause  : {analysis.get('root_cause', 'N/A')}
  Fix         : {analysis.get('fix', 'N/A')}
  Impact      : {analysis.get('estimated_impact', 'N/A')}
  Prevention  : {analysis.get('prevention', 'N/A')}

REMEDIATION
  Action      : {analysis.get('remediation_action', 'none')}
  {status_line}
{f"  SSM Cmd ID  : {remediation_result.get('ssm_command_id', '')}" if remediation_result.get('ssm_command_id') else ''}
{f"  Exit Code   : {remediation_result.get('exit_code', '')}" if 'exit_code' in remediation_result else ''}

{'='*60}
  AIOps Self-Healing Pipeline | AWS | Amazon Bedrock
  Powered by: EC2 → CloudWatch → Lambda → Bedrock → SSM
{'='*60}
    """.strip()

    try:
        response = sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],
            Message=body
        )
        print(f"📧 Email alert sent | MessageId: {response['MessageId']} | Severity: {severity}")
    except Exception as e:
        print(f"⚠️  Failed to send SNS alert: {str(e)}")


# ═════════════════════════════════════════════
# SLACK INCIDENT ALERT (unchanged from Demo 5)
# ═════════════════════════════════════════════

def send_slack_alert(analysis, remediation_result, instance_id, log_group):
    """Post a Block Kit formatted alert to Slack via Incoming Webhook."""
    import urllib.request

    if not SLACK_WEBHOOK_URL:
        print("⚠️  SLACK_WEBHOOK_URL not set — skipping Slack alert")
        return

    severity   = analysis.get('severity', 'UNKNOWN')
    rem_status = remediation_result.get('status', 'unknown')
    timestamp  = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

    severity_config = {
        "CRITICAL": {"color": "#C0392B", "emoji": ":red_circle:"},
        "HIGH":     {"color": "#E67E22", "emoji": ":large_orange_circle:"},
        "MEDIUM":   {"color": "#F1C40F", "emoji": ":large_yellow_circle:"},
        "LOW":      {"color": "#27AE60", "emoji": ":large_green_circle:"},
    }
    config = severity_config.get(severity, {"color": "#95A5A6", "emoji": ":white_circle:"})

    status_map = {
        "success":          ":white_check_mark: Auto-remediated — service restarted successfully",
        "maintenance_mode": ":large_orange_diamond: Maintenance mode — auto-remediation skipped",
        "skipped":          ":information_source: No action needed — service is healthy",
        "failed":           ":x: Remediation FAILED — manual intervention required",
        "timeout":          ":hourglass: Remediation timed out — please check manually",
    }
    status_line = status_map.get(rem_status, f"Status: {rem_status}")

    payload = {
        "attachments": [
            {
                "color": config["color"],
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"{config['emoji']} AIOps {severity}: {analysis.get('issue', 'Incident Detected')}"
                        }
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Instance ID*\n`{instance_id or 'unknown'}`"},
                            {"type": "mrkdwn", "text": f"*Timestamp*\n{timestamp}"},
                            {"type": "mrkdwn", "text": f"*Log Group*\n`{log_group}`"},
                            {"type": "mrkdwn", "text": f"*Model Used*\n{analysis.get('model_used', MODEL_ID)}"}
                        ]
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Root Cause*\n{analysis.get('root_cause', 'N/A')}"}
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Recommended Fix*\n`{analysis.get('fix', 'N/A')}`"},
                            {"type": "mrkdwn", "text": f"*Estimated Impact*\n{analysis.get('estimated_impact', 'N/A')}"}
                        ]
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": f"*Remediation Action*\n`{analysis.get('remediation_action', 'none')}`"},
                            {"type": "mrkdwn", "text": f"*Result*\n{status_line}"}
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f"AIOps Pipeline | EC2 → CloudWatch → Lambda → Bedrock → SSM | {timestamp}"}
                        ]
                    }
                ]
            }
        ]
    }

    try:
        data    = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            SLACK_WEBHOOK_URL,
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            print(f"💬 Slack alert sent | Status: {resp.status} | Severity: {severity}")
    except Exception as e:
        print(f"⚠️  Failed to send Slack alert: {str(e)}")


# ═════════════════════════════════════════════
# HELPER FUNCTIONS (unchanged from Demo 3)
# ═════════════════════════════════════════════

def decode_cloudwatch_logs(event):
    """Decode the base64-encoded, gzip-compressed CloudWatch log payload."""
    encoded      = event['awslogs']['data']
    compressed   = base64.b64decode(encoded)
    decompressed = gzip.decompress(compressed)
    return json.loads(decompressed)


def extract_instance_id(log_stream, log_group):
    """
    Extract EC2 instance ID from log stream name.
    Falls back to EC2 describe by Name tag 'aiops-nginx-demo'.
    """
    match = re.search(r'(i-[a-f0-9]{8,17})', log_stream)
    if match:
        return match.group(1)

    try:
        response = ec2.describe_instances(
            Filters=[
                {'Name': 'instance-state-name', 'Values': ['running']},
                {'Name': 'tag:Name',            'Values': ['aiops-nginx-demo']}
            ]
        )
        reservations = response.get('Reservations', [])
        if reservations:
            return reservations[0]['Instances'][0]['InstanceId']
    except Exception as e:
        print(f"⚠️  Could not determine instance ID: {e}")

    return None


def is_startup_only_logs(log_text):
    """
    Return True if logs contain only Nginx startup messages.
    Used to skip Bedrock on post-remediation startup noise.
    """
    shutdown_signals = [
        'sigquit', 'sigterm', 'shutting down', 'gracefully shutting down',
        'worker process exited', 'exited with code',
        '[error]', '[crit]', '[alert]',
        'connection refused', 'no space left', 'bind() failed', 'open() failed',
    ]
    log_lower = log_text.lower()
    for signal in shutdown_signals:
        if signal in log_lower:
            print(f"🔍 Pre-check 2.5: signal found → '{signal}'")
            return False
    print("✅ Pre-check 2.5: startup-only logs detected")
    return True


def detect_service_from_logs(log_text):
    """
    Identify the affected service from log content.
    Generic — extend with apache, mysql, node etc. as needed.
    """
    log_lower     = log_text.lower()
    nginx_signals = ['nginx/', 'nginx:', 'nginx[',
                     'signal 3 (sigquit)', 'signal 15 (sigterm)',
                     'gracefully shutting down', 'worker process exited', 'exited with code']
    for signal in nginx_signals:
        if signal in log_lower:
            return 'nginx'
    return None


def detect_affected_service(remediation_action):
    """Map Bedrock remediation action key to the service name."""
    service_map = {
        'nginx_service_stopped': 'nginx',
        'nginx_service_failed':  'nginx',
        'nginx_config_error':    'nginx',
        'nginx_port_conflict':   'nginx',
        'general_restart':       'nginx',
        'disk_full':             None,
        'permission_error':      None,
        'none':                  None
    }
    return service_map.get(remediation_action, None)


# ═════════════════════════════════════════════
# BEDROCK ANALYSIS (unchanged from Demo 3)
# ═════════════════════════════════════════════

def analyze_with_bedrock(log_text, instance_id):
    """Send Nginx logs to Bedrock (Claude) for AI root-cause analysis."""

    prompt = f"""You are an automated self-healing infrastructure system analyzing Nginx logs.
Your job is to detect if Nginx is DOWN and prescribe the correct remediation action.

EC2 Instance: {instance_id or 'unknown'}

Nginx Logs to Analyze:
<logs>
{log_text}
</logs>

─── MANDATORY DECISION RULES ───

RULE 1 — Nginx is STOPPED → remediation_action = "nginx_service_stopped"
  These log signals ALWAYS mean Nginx has stopped — no exceptions:
  • "signal 3 (SIGQUIT) received"
  • "signal 15 (SIGTERM) received"
  • "gracefully shutting down"
  • "worker process exited with code 0"
  • "worker process N exited"
  • "exit" appearing after any shutdown signal

  ⚠️  Do NOT consider whether shutdown was intentional.
  If Nginx has stopped → always use "nginx_service_stopped".

RULE 2 — Nginx FAILED → remediation_action = "nginx_service_failed"
  • "failed to start" / "start request repeated too quickly"

RULE 3 — Config Error → remediation_action = "nginx_config_error"
  • "nginx: configuration file ... test failed"

RULE 4 — Port Conflict → remediation_action = "nginx_port_conflict"
  • "bind() to 0.0.0.0:80 failed (98: Address already in use)"

RULE 5 — Disk Full → remediation_action = "disk_full"
  • "No space left on device"

RULE 6 — Permission Error → remediation_action = "permission_error"
  • "permission denied" on log or pid files

RULE 7 — remediation_action = "none" ONLY when Nginx is confirmed running with zero shutdown signals.

─── OUTPUT FORMAT ───

Respond ONLY with valid JSON (no markdown):
{{
  "issue": "one line summary",
  "root_cause": "technical explanation",
  "severity": "LOW|MEDIUM|HIGH|CRITICAL",
  "fix": "exact fix command",
  "remediation_action": "nginx_service_stopped|nginx_service_failed|nginx_config_error|nginx_port_conflict|disk_full|permission_error|general_restart|none",
  "prevention": "prevention steps",
  "estimated_impact": "affected users/services"
}}"""

    try:
        response = bedrock.invoke_model(
            modelId=MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1000,
                "messages": [{"role": "user", "content": prompt}]
            }),
            contentType='application/json',
            accept='application/json'
        )

        result   = json.loads(response['body'].read())
        raw_text = result['content'][0]['text'].strip()
        raw_text = re.sub(r'```json|```', '', raw_text).strip()
        analysis = json.loads(raw_text)
        analysis['analyzed_at'] = datetime.utcnow().isoformat()
        analysis['model_used']  = MODEL_ID
        return analysis

    except json.JSONDecodeError as e:
        print(f"⚠️  Bedrock JSON parse error: {e}")
        return {
            "issue":              "Log analysis completed — JSON parse error",
            "root_cause":         raw_text[:500],
            "severity":           "MEDIUM",
            "fix":                "Manual review required",
            "remediation_action": "general_restart",
            "prevention":         "Review logs manually",
            "estimated_impact":   "Unknown",
            "analyzed_at":        datetime.utcnow().isoformat()
        }


# ═════════════════════════════════════════════
# SSM AUTO-REMEDIATION (unchanged from Demo 3)
# ═════════════════════════════════════════════

def perform_remediation(instance_id, analysis):
    """Execute the fix on EC2 via SSM Run Command — no SSH required."""
    action_key  = analysis.get('remediation_action', 'general_restart')

    if action_key == 'none':
        return {"status": "skipped", "reason": "Bedrock determined no remediation needed"}

    action      = REMEDIATION_ACTIONS.get(action_key, REMEDIATION_ACTIONS['general_restart'])
    command     = action['command']
    description = action['description']

    print(f"🔧 Action : {action_key}")
    print(f"💻 Command: {command}")

    try:
        if not is_instance_ssm_ready(instance_id):
            return {"status": "failed", "reason": "Instance not reachable via SSM", "action_attempted": action_key}

        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName='AWS-RunShellScript',
            Parameters={
                'commands': [
                    command,
                    'echo "--- Post-Fix Status ---"',
                    'sudo systemctl is-active nginx && echo "NGINX_STATUS: RUNNING" || echo "NGINX_STATUS: STOPPED"',
                    'echo "--- Nginx Process ---"',
                    'ps aux | grep nginx | grep -v grep || echo "No nginx process found"'
                ],
                'executionTimeout': ['120']
            },
            Comment=f"AIOps Auto-Remediation: {description}",
            TimeoutSeconds=300
        )

        command_id = response['Command']['CommandId']
        print(f"✅ SSM Command sent: {command_id}")
        result = wait_for_ssm_command(command_id, instance_id)

        return {
            "status":           "success",
            "action_taken":     action_key,
            "description":      description,
            "command_executed": command,
            "ssm_command_id":   command_id,
            "output":           result.get('output', '')[:500],
            "exit_code":        result.get('exit_code', 'unknown'),
            "executed_at":      datetime.utcnow().isoformat()
        }

    except Exception as e:
        print(f"❌ SSM failed: {str(e)}")
        return {"status": "failed", "action_attempted": action_key, "error": str(e)}


def is_instance_ssm_ready(instance_id):
    """Check if the EC2 instance is online and reachable via SSM."""
    try:
        response  = ssm.describe_instance_information(
            Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
        )
        instances = response.get('InstanceInformationList', [])
        if instances:
            status = instances[0].get('PingStatus', '')
            print(f"📡 SSM Ping: {status}")
            return status == 'Online'
        return False
    except Exception as e:
        print(f"⚠️  SSM check error: {e}")
        return False


def wait_for_ssm_command(command_id, instance_id, max_wait=60):
    """Poll SSM until command reaches a terminal state or times out."""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            response = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            status   = response['Status']
            if status in ['Success', 'Failed', 'Cancelled', 'TimedOut']:
                return {
                    "status":    status,
                    "output":    response.get('StandardOutputContent', ''),
                    "error":     response.get('StandardErrorContent', ''),
                    "exit_code": response.get('ResponseCode', -1)
                }
            print(f"⏳ SSM status: {status}")
            time.sleep(5)
        except ssm.exceptions.InvocationDoesNotExist:
            time.sleep(3)
    return {"status": "timeout", "output": "Command timed out", "exit_code": -1}


# ═════════════════════════════════════════════
# CLOUDWATCH LOGGING HELPERS (unchanged from Demo 3)
# ═════════════════════════════════════════════

def log_analysis_to_cloudwatch(analysis, instance_id, original_logs):
    """Persist Bedrock analysis to dedicated CloudWatch log group."""
    log_stream = f"{instance_id or 'unknown'}/{datetime.utcnow().strftime('%Y/%m/%d')}"

    for create_fn, kwargs in [
        (logs.create_log_group,  {'logGroupName': ANALYSIS_LOG_GROUP}),
        (logs.create_log_stream, {'logGroupName': ANALYSIS_LOG_GROUP, 'logStreamName': log_stream}),
    ]:
        try:
            create_fn(**kwargs)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass

    log_entry = {
        "timestamp":           datetime.utcnow().isoformat(),
        "instance_id":         instance_id,
        "analysis":            analysis,
        "original_log_sample": original_logs[:300]
    }

    try:
        logs.put_log_events(
            logGroupName=ANALYSIS_LOG_GROUP,
            logStreamName=log_stream,
            logEvents=[{
                'timestamp': int(datetime.utcnow().timestamp() * 1000),
                'message':   json.dumps(log_entry, default=str)
            }]
        )
        print(f"📝 Analysis logged to: {ANALYSIS_LOG_GROUP}/{log_stream}")
    except Exception as e:
        print(f"⚠️  CloudWatch write error: {e}")


def log_remediation_result(remediation_result, instance_id):
    """Print structured remediation summary for Lambda logs."""
    print("📊 Remediation Summary:")
    print(f"   Status    : {remediation_result.get('status')}")
    print(f"   Action    : {remediation_result.get('action_taken', remediation_result.get('action_attempted', 'N/A'))}")
    print(f"   Exit Code : {remediation_result.get('exit_code', 'N/A')}")
    if remediation_result.get('output'):
        print(f"   Output    : {remediation_result['output'][:200]}")