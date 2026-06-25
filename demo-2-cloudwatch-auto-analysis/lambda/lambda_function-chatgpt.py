import json
import boto3
import base64
import gzip
import re
from botocore.config import Config

MODEL_ID = "global.anthropic.claude-sonnet-4-6"

config = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2})

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1", config=config)

def clean_logs(logs):
    logs = logs.strip()
    logs = re.sub(
        r'(?i)(password|passwd|secret|token|api_key|apikey|authorization)[=:]\s*\S+',
        r'\1=***MASKED***',
        logs
    )
    return logs[-4000:] if len(logs) > 4000 else logs

def analyze_logs_with_bedrock(logs):
    prompt = f"""
You are a senior DevOps engineer.

Analyze the logs and return only valid JSON in this exact format:

{{
  "Issue": "one short sentence",
  "Root Cause": "one short sentence",
  "Fix": "maximum two practical actions",
  "Severity": "Low, Medium, High, or Critical",
  "Prevention": "one short sentence"
}}

Rules:
- Do not use emojis
- Do not use markdown
- Do not use code blocks
- Keep it short and human-readable
- Return only JSON, no extra text

Logs:
{logs}
"""

    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            "max_tokens": 220,
            "temperature": 0.2
        })
    )

    result = json.loads(response["body"].read())
    analysis_text = result["content"][0]["text"]

    try:
        return json.loads(analysis_text)
    except Exception:
        return {
            "Issue": "Unable to parse AI response.",
            "Root Cause": "The model returned a non-JSON response.",
            "Fix": analysis_text,
            "Severity": "Medium",
            "Prevention": "Keep the prompt strict and validate model output."
        }

def lambda_handler(event, context):
    compressed_payload = base64.b64decode(event["awslogs"]["data"])
    uncompressed_payload = gzip.decompress(compressed_payload)
    logs_data = json.loads(uncompressed_payload)

    log_group = logs_data.get("logGroup")
    log_stream = logs_data.get("logStream")
    log_events = logs_data.get("logEvents", [])

    logs = "\n".join([log["message"] for log in log_events])
    logs = clean_logs(logs)

    if not logs:
        print("No log messages found.")
        return {"status": "no_logs_found"}

    analysis = analyze_logs_with_bedrock(logs)

    print("=== AI ANALYSIS ===")
    print(json.dumps({
        "logGroup": log_group,
        "logStream": log_stream,
        "analysis": analysis
    }, indent=2))

    return {
        "status": "processed",
        "logGroup": log_group,
        "logStream": log_stream,
        "analysis": analysis
    }
