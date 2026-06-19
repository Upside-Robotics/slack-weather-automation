#!/bin/bash
set -euo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin

exec 9>/tmp/rain-alert-3h.lock
flock -n 9 || { echo "$(date): Rain alert 3h already running, skipping"; exit 0; }

docker run --rm \
  --log-driver awslogs \
  --log-opt awslogs-region=ca-central-1 \
  --log-opt awslogs-group=/etl/slack-weather-automation \
  --log-opt awslogs-create-group=true \
  --log-opt "awslogs-stream=rain-alert-3h-$(date +%Y-%m-%d)" \
  --env-file /home/ec2-user/.env \
  slack-weather-automation:latest \
  python rain_alert_3h.py
