#!/bin/bash
# Publish the data volume's used-space percentage as a CloudWatch custom metric
# (CrossroadsMC/DiskUsedPercent, dimensioned by InstanceId). The CDK
# `minecraft-disk-usage` alarm watches this metric. Runs every 5 minutes via cron.
set -euo pipefail

. "/mnt/minecraft-data/scripts/mc-common.sh"

REGION="$(mc_region)"
TOKEN=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60") || exit 0
INSTANCE_ID=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/instance-id) || exit 0

# Used percentage of the data volume filesystem (integer, no '%').
USED_PCT=$(df --output=pcent "$DATA_DIR" | tail -1 | tr -dc '0-9')
[ -n "$USED_PCT" ] || exit 0

aws cloudwatch put-metric-data \
  --region "$REGION" \
  --namespace CrossroadsMC \
  --metric-name DiskUsedPercent \
  --unit Percent \
  --value "$USED_PCT" \
  --dimensions "InstanceId=$INSTANCE_ID"
