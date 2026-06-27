import json
import boto3
import base64
import gzip
import re
import time
import os
from datetime import datetime

# ─────────────────────────────────────────────
# CONFIGURATION — loaded from environment variables
# ─────────────────────────────────────────────
AWS_REGION          = os.environ.get('AWS_REGION', 'eu-central-1')
ANALYSIS_LOG_GROUP  = os.environ.get('ANALYSIS_LOG_GROUP', '/aiops/lambda/nginx-analysis')
MODEL_ID            = os.environ.get('MODEL_ID', 'global.anthropic.claude-sonnet-4-6')

# ─────────────────────────────────────────────
# AWS CLIENTS
# Note: Bedrock client stays in us-east-1 because the cross-region
#       inference profile (global.*) is hosted there.
#       All other clients use the Lambda function's own region.
# ─────────────────────────────────────────────
bedrock = boto3.client('bedrock-runtime', region_name='us-east-1')
ssm     = boto3.client('ssm',             region_name=AWS_REGION)
logs    = boto3.client('logs',            region_name=AWS_REGION)
ec2     = boto3.client('ec2',             region_name=AWS_REGION)

# ─────────────────────────────────────────────
# REMEDIATION ACTION MAP
# Maps Bedrock-returned action keys to shell commands executed
# on the EC2 instance via SSM Run Command (no SSH required).
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


# ═════════════════════════════════════════════
# MAIN HANDLER
# ═════════════════════════════════════════════
def lambda_handler(event, context):
    print("🚀 AIOps Self-Healing Lambda triggered")
    print(f"📥 Event received: {json.dumps(event)[:200]}")

    try:
        # ── Guard: only process CloudWatch Logs subscription filter events ──
        if 'awslogs' not in event:
            print("⚠️  Not a CloudWatch Logs event — possibly a manual test or wrong trigger source")
            print(f"   Received event keys: {list(event.keys())}")
            return {
                "statusCode": 400,
                "body": "Event does not contain 'awslogs' data. Trigger this function via a CloudWatch Logs subscription filter."
            }

        # ── Step 1: Decode the compressed CloudWatch log payload ──
        log_data   = decode_cloudwatch_logs(event)
        log_events = log_data.get('logEvents', [])
        log_group  = log_data.get('logGroup', 'unknown')
        log_stream = log_data.get('logStream', 'unknown')

        print(f"📋 Log Group  : {log_group}")
        print(f"📋 Log Stream : {log_stream}")
        print(f"📋 Events received: {len(log_events)}")

        if not log_events:
            return {"statusCode": 200, "body": "No log events to process"}

        # Combine all log messages into a single string for analysis
        combined_logs = "\n".join([e.get('message', '') for e in log_events])

        # ── Step 2: Extract the EC2 instance ID from the log stream name ──
        # Stream name format: i-<instance-id>/nginx-error
        instance_id = extract_instance_id(log_stream, log_group)
        print(f"🖥️  EC2 Instance ID: {instance_id}")

        # ── Step 2.5: Pre-check — skip Bedrock if logs are startup-only ──
        # When Nginx starts after auto-remediation it writes startup [notice]
        # logs. These do not require analysis. Early-exiting here saves cost
        # and avoids triggering a second remediation cycle.
        if is_startup_only_logs(combined_logs):
            print("✅ Pre-check 2.5: Startup-only logs detected — service is healthy, skipping Bedrock")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "healthy",
                    "reason": "Startup-only logs detected — no analysis needed",
                    "log_group": log_group,
                    "events_count": len(log_events)
                })
            }

        # ── Step 2.6: Pre-check — skip Bedrock if service is under maintenance ──
        # Operators set MAINTENANCE_MODE_SERVICES env var (e.g. "nginx" or
        # "nginx,mysql") to signal a planned maintenance window. When a service
        # in that list is detected in the incoming logs, Bedrock is bypassed
        # entirely — avoiding unnecessary analysis cost and preventing
        # auto-remediation from restarting a service that was intentionally stopped.
        maintenance_env      = os.environ.get('MAINTENANCE_MODE_SERVICES', '').strip()
        maintenance_services = []
        if maintenance_env and maintenance_env.lower() != 'none':
            maintenance_services = [s.strip().lower() for s in maintenance_env.split(',') if s.strip()]

        if maintenance_services:
            detected_service = detect_service_from_logs(combined_logs)
            print(f"🔍 Detected service from logs : {detected_service}")
            print(f"📋 Maintenance list           : {maintenance_services}")

            if detected_service and detected_service in maintenance_services:
                print(f"🔶 MAINTENANCE MODE (pre-Bedrock): '{detected_service}' is in the maintenance list — skipping Bedrock entirely")
                return {
                    "statusCode": 200,
                    "body": json.dumps({
                        "status": "maintenance_mode",
                        "reason": f"Service '{detected_service}' is under planned maintenance",
                        "maintenance_list": maintenance_services,
                        "message": "Bedrock analysis skipped. Remove the service from MAINTENANCE_MODE_SERVICES to re-enable auto-remediation.",
                        "log_group": log_group,
                        "instance_id": instance_id
                    })
                }

        # ── Step 3: Send logs to Amazon Bedrock (Claude) for AI analysis ──
        print("🤖 Sending logs to Bedrock for analysis...")
        analysis = analyze_with_bedrock(combined_logs, instance_id)
        print(f"✅ Bedrock Analysis Complete: {json.dumps(analysis, indent=2)}")

        # ── Step 4: Persist the analysis result to a dedicated CloudWatch log group ──
        log_analysis_to_cloudwatch(analysis, instance_id, combined_logs)

        # ── Step 5: Evaluate remediation action ──
        remediation_action = analysis.get('remediation_action', 'none')
        affected_service   = detect_affected_service(remediation_action)

        print(f"🔧 Remediation Action : {remediation_action}")
        print(f"🛠️  Affected Service   : {affected_service}")
        print(f"📋 Maintenance List   : {maintenance_services if maintenance_services else 'Empty (no maintenance)'}")

        if affected_service and affected_service.lower() in maintenance_services:
            # Service is in maintenance — log the event but take no action
            print(f"🔶 MAINTENANCE MODE: '{affected_service}' is in the maintenance list — skipping remediation")
            analysis['remediation'] = {
                "status": "maintenance_mode",
                "reason": f"Service '{affected_service}' is under planned maintenance",
                "maintenance_list": maintenance_services,
                "action_would_have_been": remediation_action,
                "message": "Remove the service from MAINTENANCE_MODE_SERVICES to re-enable auto-remediation."
            }
            log_remediation_result(analysis['remediation'], instance_id)

        elif remediation_action == 'none':
            # Bedrock determined the service is healthy — no action needed
            print("ℹ️  No remediation required — Bedrock confirmed the service is healthy")
            analysis['remediation'] = {
                "status": "skipped",
                "reason": "Bedrock analysis determined no remediation is required"
            }

        elif instance_id is None:
            # Cannot remediate without knowing which EC2 instance to target
            print("⚠️  Cannot remediate — EC2 instance ID could not be determined")
            analysis['remediation'] = {
                "status": "failed",
                "reason": "EC2 instance ID could not be determined from the log stream name"
            }

        else:
            # All checks passed — trigger SSM Run Command for auto-remediation
            print(f"🔧 Starting auto-remediation for instance: {instance_id}")
            remediation_result      = perform_remediation(instance_id, analysis)
            analysis['remediation'] = remediation_result
            log_remediation_result(remediation_result, instance_id)

        return {
            "statusCode": 200,
            "body": json.dumps(analysis)
        }

    except Exception as e:
        print(f"❌ Lambda error: {str(e)}")
        raise


# ═════════════════════════════════════════════
# HELPER FUNCTIONS
# ═════════════════════════════════════════════

def decode_cloudwatch_logs(event):
    """
    Decode the base64-encoded, gzip-compressed payload delivered by
    a CloudWatch Logs subscription filter.
    """
    encoded      = event['awslogs']['data']
    compressed   = base64.b64decode(encoded)
    decompressed = gzip.decompress(compressed)
    return json.loads(decompressed)


def extract_instance_id(log_stream, log_group):
    """
    Extract the EC2 instance ID from the CloudWatch log stream name.

    Primary  — regex match on the stream name (format: i-<id>/nginx-error).
    Fallback — describe EC2 instances filtered by Name tag 'aiops-nginx-demo'
               in case the stream name does not follow the expected pattern.
    """
    match = re.search(r'(i-[a-f0-9]{8,17})', log_stream)
    if match:
        return match.group(1)

    # Fallback: look up the instance by tag
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
        print(f"⚠️  Could not determine instance ID via EC2 describe: {e}")

    return None


def is_startup_only_logs(log_text):
    """
    Return True if the log batch contains only Nginx startup messages
    and no shutdown, error, or crash signals.

    Used as a fast pre-check (Step 2.5) to skip Bedrock calls when
    Nginx has just been started by a previous remediation cycle —
    avoiding unnecessary cost and preventing remediation loops.
    """
    shutdown_signals = [
        'sigquit',
        'sigterm',
        'shutting down',
        'gracefully shutting down',
        'worker process exited',    # specific — avoids matching startup "start worker process" lines
        'exited with code',
        '[error]',
        '[crit]',
        '[alert]',
        'connection refused',
        'no space left',
        'bind() failed',
        'open() failed',
    ]
    log_lower = log_text.lower()

    for signal in shutdown_signals:
        if signal in log_lower:
            print(f"🔍 Pre-check 2.5: shutdown/error signal detected → '{signal}' — proceeding to Bedrock")
            return False

    print("✅ Pre-check 2.5 passed: startup-only logs, no shutdown signals detected")
    return True


def detect_service_from_logs(log_text):
    """
    Identify which service is referenced in the log content.

    This function is intentionally generic so it can be extended to
    support additional services (apache2, mysql, node, pm2, java, etc.)
    without modifying the main handler.

    Returns the service name as a lowercase string, or None if unknown.
    """
    log_lower = log_text.lower()

    # Nginx — matched via process signatures and shutdown keywords
    nginx_signals = [
        'nginx/',                    # e.g. "nginx/1.30.2"
        'nginx:',                    # e.g. "nginx: configuration file"
        'nginx[',                    # e.g. "nginx[12345]"
        'signal 3 (sigquit)',
        'signal 15 (sigterm)',
        'gracefully shutting down',
        'worker process exited',
        'exited with code'
    ]
    for signal in nginx_signals:
        if signal in log_lower:
            return 'nginx'

    # ── Add additional service detectors below as needed ──
    # Example:
    # apache_signals = ['apache2', 'httpd', 'apachectl']
    # for signal in apache_signals:
    #     if signal in log_lower:
    #         return 'apache2'

    return None


def detect_affected_service(remediation_action):
    """
    Map a Bedrock-returned remediation action key to the name of the
    affected service. Used to cross-check the maintenance list after
    Bedrock analysis (Step 5).

    Extend this map when new service action keys are added to
    REMEDIATION_ACTIONS.
    """
    service_map = {
        'nginx_service_stopped': 'nginx',
        'nginx_service_failed':  'nginx',
        'nginx_config_error':    'nginx',
        'nginx_port_conflict':   'nginx',
        'general_restart':       'nginx',
        'disk_full':             None,   # not service-specific
        'permission_error':      None,   # not service-specific
        'none':                  None
    }
    return service_map.get(remediation_action, None)


# ═════════════════════════════════════════════
# BEDROCK ANALYSIS
# ═════════════════════════════════════════════

def analyze_with_bedrock(log_text, instance_id):
    """
    Send Nginx error logs to Amazon Bedrock (Claude) for AI-powered
    root-cause analysis and remediation recommendation.

    The prompt uses explicit, rule-based instructions to ensure
    deterministic JSON output suitable for automated processing.
    """
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
  • Any combination of the above

  ⚠️  IMPORTANT: Do NOT consider whether the shutdown was graceful, intentional,
  or triggered by systemd/init. If Nginx has stopped → always use "nginx_service_stopped".
  This is an automated system. Nginx must always be running.

RULE 2 — Nginx FAILED or CRASHED → remediation_action = "nginx_service_failed"
  • "failed to start" / "start request repeated too quickly"
  • Process exited with non-zero code

RULE 3 — Config Error → remediation_action = "nginx_config_error"
  • "nginx: configuration file ... test failed"
  • "unknown directive" / "invalid parameter"

RULE 4 — Port Conflict → remediation_action = "nginx_port_conflict"
  • "bind() to 0.0.0.0:80 failed (98: Address already in use)"

RULE 5 — Disk Full → remediation_action = "disk_full"
  • "No space left on device"

RULE 6 — Permission Error → remediation_action = "permission_error"
  • "permission denied" on log files or pid files

RULE 7 — remediation_action = "none" ONLY when ALL of these are true:
  • Nginx startup lines present (e.g. "start worker process", "using the epoll event method")
  • ZERO shutdown/exit/SIGQUIT/SIGTERM signals in logs
  • Service is confirmed running with active worker processes

─── OUTPUT FORMAT ───

Respond ONLY with this exact JSON (no markdown, no explanation, no extra text):
{{
  "issue": "one line summary of what happened",
  "root_cause": "technical explanation of the root cause",
  "severity": "LOW|MEDIUM|HIGH|CRITICAL",
  "fix": "exact command or steps to fix this",
  "remediation_action": "nginx_service_stopped|nginx_service_failed|nginx_config_error|nginx_port_conflict|disk_full|permission_error|general_restart|none",
  "prevention": "steps to prevent this in future",
  "estimated_impact": "which users or services are affected"
}}

Severity guide:
  CRITICAL = complete outage, no recovery possible without intervention
  HIGH     = service is down, auto-remediation required immediately
  MEDIUM   = degraded performance or partial failure
  LOW      = informational only, service is healthy"""

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

        # Strip markdown code fences if present (defensive parsing)
        raw_text = re.sub(r'```json|```', '', raw_text).strip()
        analysis = json.loads(raw_text)
        analysis['analyzed_at'] = datetime.utcnow().isoformat()
        analysis['model_used']  = MODEL_ID
        return analysis

    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parse error from Bedrock response: {e}")
        # Return a safe fallback so the pipeline can still attempt remediation
        return {
            "issue": "Log Analysis Completed — JSON parse error",
            "root_cause": raw_text[:500],
            "severity": "MEDIUM",
            "fix": "Manual review required",
            "remediation_action": "general_restart",
            "prevention": "Review logs manually",
            "estimated_impact": "Unknown",
            "analyzed_at": datetime.utcnow().isoformat()
        }


# ═════════════════════════════════════════════
# SSM AUTO-REMEDIATION
# ═════════════════════════════════════════════

def perform_remediation(instance_id, analysis):
    """
    Execute the recommended fix on the target EC2 instance using
    AWS Systems Manager (SSM) Run Command — no SSH or key pairs required.

    Steps:
      1. Validate that the instance is reachable via SSM.
      2. Send the shell command from REMEDIATION_ACTIONS.
      3. Wait up to 60 s for the command to complete.
      4. Return a structured result including exit code and stdout.
    """
    action_key  = analysis.get('remediation_action', 'general_restart')

    if action_key == 'none':
        return {
            "status": "skipped",
            "reason": "Bedrock determined no remediation is needed"
        }

    action      = REMEDIATION_ACTIONS.get(action_key, REMEDIATION_ACTIONS['general_restart'])
    command     = action['command']
    description = action['description']

    print(f"🔧 Remediation action : {action_key}")
    print(f"💻 Command            : {command}")

    try:
        if not is_instance_ssm_ready(instance_id):
            return {
                "status": "failed",
                "reason": "Instance is not reachable via SSM",
                "action_attempted": action_key
            }

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
            "status": "success",
            "action_taken": action_key,
            "description": description,
            "command_executed": command,
            "ssm_command_id": command_id,
            "output": result.get('output', '')[:500],
            "exit_code": result.get('exit_code', 'unknown'),
            "executed_at": datetime.utcnow().isoformat()
        }

    except Exception as e:
        print(f"❌ SSM remediation failed: {str(e)}")
        return {
            "status": "failed",
            "action_attempted": action_key,
            "error": str(e),
            "executed_at": datetime.utcnow().isoformat()
        }


def is_instance_ssm_ready(instance_id):
    """
    Check whether the target EC2 instance is registered with SSM
    and currently reachable (PingStatus == 'Online').
    """
    try:
        response  = ssm.describe_instance_information(
            Filters=[{'Key': 'InstanceIds', 'Values': [instance_id]}]
        )
        instances = response.get('InstanceInformationList', [])
        if instances:
            status = instances[0].get('PingStatus', '')
            print(f"📡 SSM Ping Status: {status}")
            return status == 'Online'
        return False
    except Exception as e:
        print(f"⚠️  SSM readiness check failed: {e}")
        return False


def wait_for_ssm_command(command_id, instance_id, max_wait=60):
    """
    Poll SSM for the result of a Run Command invocation.
    Returns as soon as a terminal status is reached or max_wait seconds elapse.
    """
    start = time.time()

    while time.time() - start < max_wait:
        try:
            response = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id
            )
            status = response['Status']

            if status in ['Success', 'Failed', 'Cancelled', 'TimedOut']:
                return {
                    "status":    status,
                    "output":    response.get('StandardOutputContent', ''),
                    "error":     response.get('StandardErrorContent', ''),
                    "exit_code": response.get('ResponseCode', -1)
                }

            print(f"⏳ SSM command status: {status} — waiting...")
            time.sleep(5)

        except ssm.exceptions.InvocationDoesNotExist:
            # Command not yet registered on the SSM side — retry shortly
            time.sleep(3)

    return {"status": "timeout", "output": "Command timed out", "exit_code": -1}


# ═════════════════════════════════════════════
# CLOUDWATCH LOGGING HELPERS
# ═════════════════════════════════════════════

def log_analysis_to_cloudwatch(analysis, instance_id, original_logs):
    """
    Persist the Bedrock analysis result to a dedicated CloudWatch log group
    (/aiops/lambda/nginx-analysis) for audit, dashboarding, and future review.

    Log stream naming convention: <instance-id>/YYYY/MM/DD
    """
    log_stream = f"{instance_id or 'unknown'}/{datetime.utcnow().strftime('%Y/%m/%d')}"

    # Create log group and stream if they do not already exist
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
        print(f"⚠️  Failed to write analysis to CloudWatch: {e}")


def log_remediation_result(remediation_result, instance_id):
    """
    Print a structured remediation summary to the Lambda log stream
    for observability and debugging.
    """
    print("📊 Remediation Summary:")
    print(f"   Status    : {remediation_result.get('status')}")
    print(f"   Action    : {remediation_result.get('action_taken', remediation_result.get('action_attempted', 'N/A'))}")
    print(f"   Exit Code : {remediation_result.get('exit_code', 'N/A')}")
    if remediation_result.get('output'):
        print(f"   Output    : {remediation_result['output'][:200]}")