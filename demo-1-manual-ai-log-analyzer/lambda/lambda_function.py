import json
import re
import boto3
from botocore.config import Config

MODEL_ID = "global.anthropic.claude-sonnet-4-6"

config = Config(
    connect_timeout=10,
    read_timeout=60,
    retries={"max_attempts": 2}
)

bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
    config=config
)

def clean_logs(logs):
    logs = logs.strip()

    logs = re.sub(
        r'(?i)(password|passwd|secret|token|api_key|apikey|authorization)[=:]\s*\S+',
        r'\1=***MASKED***',
        logs
    )

    max_chars = 4000
    if len(logs) > max_chars:
        logs = logs[-max_chars:]

    return logs

def lambda_handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))
        logs = body.get("logs", "")

        if not logs:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "error": "Please provide logs in the request body."
                })
            }

        logs = clean_logs(logs)

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
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt}
                        ]
                    }
                ],
                "max_tokens": 220,
                "temperature": 0.2
            })
        )

        result = json.loads(response["body"].read())
        analysis_text = result["content"][0]["text"]

        try:
            analysis_json = json.loads(analysis_text)
        except Exception:
            analysis_json = {
                "Issue": "Unable to parse AI response.",
                "Root Cause": "The model returned a non-JSON response.",
                "Fix": analysis_text,
                "Severity": "Medium",
                "Prevention": "Keep the prompt strict and validate model output."
            }

        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json"
            },
            "body": json.dumps(analysis_json)
        }

    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "error": str(e)
            })
        }