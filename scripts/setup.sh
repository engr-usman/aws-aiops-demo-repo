#!/bin/bash
set -e

echo "### Update & Nginx Installation ###"

sudo dnf update -y
sudo dnf install -y nginx

sudo systemctl enable --now nginx
sudo systemctl status nginx --no-pager

echo "### Testing Nginx ###"
curl -I http://localhost || true

echo "### CloudWatch Agent Installation ###"

sudo dnf install -y amazon-cloudwatch-agent

echo "### Creating CloudWatch Agent Config ###"

sudo tee /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json > /dev/null << 'EOF'
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
          },
          {
            "file_path": "/var/log/nginx/access.log",
            "log_group_name": "/aiops/ec2/nginx/access-logs",
            "log_stream_name": "{instance_id}/nginx-access",
            "retention_in_days": 7
          }
        ]
      }
    }
  }
}
EOF

echo "### Starting CloudWatch Agent ###"

sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config \
  -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
  -s

echo "### CloudWatch Agent Status ###"

sudo systemctl status amazon-cloudwatch-agent --no-pager || true

sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a status

echo "### Script Completed Successfully ###"