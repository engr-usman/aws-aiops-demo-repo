# AWS AIOps Demo: AI-Powered Log Analyzer

This repository contains two AWS AI/DevOps demos built for a session on:

**From DevOps to AIOps: AI-Powered Observability and Automation on AWS**

The demos show how Amazon Bedrock can help DevOps teams analyze logs, identify issues, suggest root causes, and recommend fixes using AI.

---

## Demos Included

## Demo 1: Manual AI Log Analyzer

A browser-based frontend where users paste raw logs or select sample logs. The logs are sent to API Gateway, processed by Lambda, analyzed by Amazon Bedrock, and returned as structured AI insights.

**Flow:**

```text
Frontend → API Gateway → Lambda → Amazon Bedrock → AI Output on Frontend
```

## Demo 2: Automated AIOps Pipeline

Application logs are pushed to CloudWatch Logs. A CloudWatch Logs subscription trigger invokes Lambda automatically. Lambda sends the logs to Amazon Bedrock and prints AI-generated analysis in CloudWatch Logs.

**Flow:**

```text
Application Logs → CloudWatch Logs → Lambda Trigger → Bedrock → Auto Analysis
```

---

## AWS Services Used

- Amazon Bedrock
- AWS Lambda
- Amazon API Gateway
- Amazon CloudWatch Logs
- Amazon S3
- Amazon CloudFront
- AWS Certificate Manager
- Route 53

---

## Repository Structure

```text
.
├── demo-1-manual-ai-log-analyzer/
│   ├── frontend/
│   │   └── index.html
│   └── lambda/
│       └── lambda_function.py
├── demo-2-cloudwatch-auto-analysis/
│   └── lambda/
│       └── lambda_function.py
├── scripts/
│   ├── cloudwatch-log-commands.sh
│   └── cleanup.sh
├── README.md
└── .gitignore
```

---

## Prerequisites

- AWS account
- AWS CLI configured
- Amazon Bedrock model access enabled
- Lambda execution role with required permissions
- API Gateway HTTP API
- S3 bucket and CloudFront distribution for frontend hosting

Recommended Bedrock runtime region:

```text
us-east-1
```

Model used:

```text
global.anthropic.claude-sonnet-4-6
```

---

## Required IAM Permissions

For demo purposes, attach these to Lambda execution role:

```text
AmazonBedrockFullAccess
CloudWatchLogsFullAccess
```

For production, use least-privilege permissions.

---

# Demo 1 Setup: Manual AI Log Analyzer

## 1. Create Lambda Function

Create a Lambda function:

```text
Name: ai-log-analyzer
Runtime: Python 3.12
Timeout: 30 seconds
Memory: 512 MB
```

Paste code from:

```text
demo-1-manual-ai-log-analyzer/lambda/lambda_function.py
```

---

## 2. Create API Gateway HTTP API

Create HTTP API and integrate it with Lambda.

Required route:

```text
POST /analyze
```

Enable CORS:

```text
Access-Control-Allow-Origin: *
Access-Control-Allow-Headers: content-type
Access-Control-Allow-Methods: POST, OPTIONS
```

Endpoint format:

```text
https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com/analyze
```

---

## 3. Test API

```bash
curl -s -X POST https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com/analyze   -H "Content-Type: application/json"   -d '{"logs":"ERROR: DB connection timeout after 30 seconds"}' | jq
```

---

## 4. Configure Frontend

Open:

```text
demo-1-manual-ai-log-analyzer/frontend/index.html
```

Replace:

```javascript
const API_URL = "https://YOUR_API_ID.execute-api.YOUR_REGION.amazonaws.com/analyze";
```

with your actual API Gateway endpoint.

---

## 5. Test Frontend Locally

```bash
cd demo-1-manual-ai-log-analyzer/frontend
python3 -m http.server 3000
```

Open:

```text
http://localhost:3000
```

---

## 6. Deploy Frontend to S3

```bash
aws s3 mb s3://aiops.example.com --region eu-central-1
```

```bash
aws s3 cp index.html s3://aiops.example.com/index.html
```

```bash
aws s3 website s3://aiops.example.com/   --index-document index.html   --error-document index.html
```

---

## 7. CloudFront Deployment

Recommended setup:

```text
S3 Static Website → CloudFront → ACM Certificate → Route 53 Subdomain
```

CloudFront settings:

```text
Default root object: index.html
Viewer protocol policy: Redirect HTTP to HTTPS
Alternate domain name: aiops.example.com
Origin: S3 static website endpoint
```

Invalidate CloudFront after updating frontend:

```bash
aws cloudfront create-invalidation   --distribution-id YOUR_DISTRIBUTION_ID   --paths "/*"
```

---

# Demo 2 Setup: CloudWatch Logs Auto Analysis

## 1. Create CloudWatch Log Group

```bash
aws logs create-log-group   --log-group-name /aiops/demo-logs   --region eu-central-1
```

## 2. Create Log Stream

```bash
aws logs create-log-stream   --log-group-name /aiops/demo-logs   --log-stream-name test-stream   --region eu-central-1
```

## 3. Create Lambda Function

Create Lambda:

```text
Name: aiops-cloudwatch-auto-analyzer
Runtime: Python 3.12
Timeout: 30 seconds
Memory: 512 MB
```

Paste code from:

```text
demo-2-cloudwatch-auto-analysis/lambda/lambda_function.py
```

## 4. Add CloudWatch Logs Trigger

In Lambda:

```text
Configuration → Triggers → Add Trigger
Source: CloudWatch Logs
Log group: /aiops/demo-logs
Filter pattern: ERROR
```

## 5. Send Sample Logs

Database issue:

```bash
aws logs put-log-events   --log-group-name /aiops/demo-logs   --log-stream-name test-stream   --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: RDS connection timeout after 30 seconds"
```

Kubernetes issue:

```bash
aws logs put-log-events   --log-group-name /aiops/demo-logs   --log-stream-name test-stream   --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: Pod CrashLoopBackOff. Container restarting repeatedly"
```

IAM issue:

```bash
aws logs put-log-events   --log-group-name /aiops/demo-logs   --log-stream-name test-stream   --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: AccessDenied. IAM role does not have permission to access S3 bucket"
```

ALB issue:

```bash
aws logs put-log-events   --log-group-name /aiops/demo-logs   --log-stream-name test-stream   --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: ALB health check failed. Targets are unhealthy"
```

---

## 6. View AI Analysis

Go to:

```text
CloudWatch → Log groups → /aws/lambda/aiops-cloudwatch-auto-analyzer
```

Look for:

```text
=== AI ANALYSIS ===
```

---

## Useful Commands

Check log streams:

```bash
aws logs describe-log-streams   --log-group-name /aiops/demo-logs
```

If sequence token is required:

```bash
aws logs describe-log-streams   --log-group-name /aiops/demo-logs   --log-stream-name-prefix test-stream
```

Then use:

```bash
aws logs put-log-events   --log-group-name /aiops/demo-logs   --log-stream-name test-stream   --sequence-token YOUR_SEQUENCE_TOKEN   --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: RDS timeout"
```

---

## Demo Talking Points

### Demo 1

```text
In Demo 1, we manually paste logs into a frontend UI.
The system sends logs to API Gateway and Lambda.
Lambda calls Amazon Bedrock.
AI returns structured insights: issue, root cause, fix, severity, and prevention.
```

### Demo 2

```text
In Demo 2, logs are automatically pushed to CloudWatch.
CloudWatch triggers Lambda automatically.
Lambda sends the logs to Bedrock.
AI analysis is generated without manual input.
This is the transition from DevOps to AIOps.
```

---

## Future Improvements

- Send AI output to Slack
- Send critical alerts to SNS
- Store analysis in DynamoDB
- Build dashboard for incident history
- Add auto-remediation using Lambda, SSM, and Step Functions
- Restart EC2 services automatically when service failure is detected
- Trigger Auto Scaling for high CPU events
- Block malicious IPs using AWS WAF

---

## Cleanup Commands

Delete CloudWatch log group:

```bash
aws logs delete-log-group   --log-group-name /aiops/demo-logs
```

Delete Lambda functions:

```bash
aws lambda delete-function   --function-name ai-log-analyzer
```

```bash
aws lambda delete-function   --function-name aiops-cloudwatch-auto-analyzer
```

Delete S3 frontend files:

```bash
aws s3 rm s3://aiops.example.com --recursive
```

CloudFront, API Gateway, ACM, and Route 53 records should be reviewed and deleted manually.

---

## Author

**Usman Ahmad**  
AWS Cloud & DevOps Consultant  
Speaker | Trainer | Community Builder
