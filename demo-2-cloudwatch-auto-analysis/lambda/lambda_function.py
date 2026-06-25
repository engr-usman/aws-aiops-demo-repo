import json
import boto3
import base64
import gzip

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")

MODEL_ID = "global.anthropic.claude-sonnet-4-6"

def lambda_handler(event, context):

    # Step 1: Decode CloudWatch Logs
    compressed_payload = base64.b64decode(event['awslogs']['data'])
    uncompressed_payload = gzip.decompress(compressed_payload)
    logs_data = json.loads(uncompressed_payload)

    log_events = logs_data.get("logEvents", [])

    # Combine logs into one string
    logs = "\n".join([log["message"] for log in log_events])

    # Step 2: Create prompt
    prompt = f"""
You are an expert DevOps engineer.

Analyze the following logs and return ONLY valid JSON:

{{
  "Issue": "...",
  "Root Cause": "...",
  "Fix": "...",
  "Severity": "Low | Medium | High | Critical",
  "Prevention": "..."
}}

Logs:
{logs}
"""

    # Step 3: Call Bedrock
    response = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}]
                }
            ],
            "max_tokens": 300
        })
    )

    result = json.loads(response["body"].read())
    output_text = result["content"][0]["text"]

    # Step 4: Safe JSON parse
    try:
        structured_output = json.loads(output_text)
    except:
        structured_output = {"raw_output": output_text}

    # Step 5: Print result (visible in CloudWatch logs)
    print("=== AI ANALYSIS ===")
    print(json.dumps(structured_output, indent=2))

    return {"status": "processed"}