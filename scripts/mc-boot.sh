#!/bin/bash
# Runs on every boot (via mc-boot.service). Re-syncs the manifest/scripts/config
# from S3, seeds the live-category pointer in SSM if unset, then starts the live
# slot's active world.
#
# This is what makes plain stop/start cycles work: EC2 user-data only runs once
# per instance, but this enabled unit runs on every boot.
set -euo pipefail

. "/mnt/minecraft-data/scripts/mc-common.sh"

# Pull the latest manifest + scripts + config (picks up `make set` + cdk deploy).
echo "Boot: syncing assets from S3"
sync_assets

# Seed the live category if unset: first category with a non-empty activeWorld.
LIVE="$(ssm_get active-category)"
if [ -z "$LIVE" ] || [ "$LIVE" = "None" ]; then
  LIVE="$(jq -r '.categories[] | select(.activeWorld != "" and .activeWorld != null) | .name' "$MANIFEST" | head -1)"
  [ -n "$LIVE" ] && ssm_put active-category "$LIVE"
fi

if [ -z "$LIVE" ] || [ "$LIVE" = "None" ]; then
  echo "No category has an active world — nothing to start." >&2
  exit 0
fi

UUID="$(active_uuid_for "$LIVE")"
if [ -z "$UUID" ]; then
  echo "Live category '$LIVE' has no active world set — nothing to start." >&2
  exit 0
fi

# Ensure the active world is provisioned on EBS (idempotent).
echo "Boot: provisioning world $UUID for $LIVE"
"$DATA_DIR/scripts/provision-worlds.sh" "$UUID"

echo "Boot: starting live slot minecraft@$LIVE"
systemctl start "minecraft@$LIVE"
