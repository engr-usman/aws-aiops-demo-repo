#!/bin/bash

LOG_GROUP="/aiops/demo-logs"
LOG_STREAM="test-stream"

aws logs put-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: RDS connection timeout after 30 seconds"

sleep 2

aws logs put-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: Pod CrashLoopBackOff. Container restarting repeatedly"

sleep 2

aws logs put-log-events \
  --log-group-name "$LOG_GROUP" \
  --log-stream-name "$LOG_STREAM" \
  --log-events timestamp=$(($(date +%s) * 1000)),message="ERROR: AccessDenied. IAM role does not have permission to access S3 bucket"
