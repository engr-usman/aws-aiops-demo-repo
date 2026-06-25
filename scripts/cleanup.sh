#!/bin/bash

echo "Cleanup helper. Review resources before deleting."

aws logs delete-log-group --log-group-name /aiops/demo-logs
aws lambda delete-function --function-name ai-log-analyzer
aws lambda delete-function --function-name aiops-cloudwatch-auto-analyzer

echo "Manual cleanup still required for API Gateway, CloudFront, S3 bucket, Route53 record, and ACM certificate if created."
