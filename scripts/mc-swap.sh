#!/bin/bash
# Switch the live world slot. Invoked on the instance via SSM Run Command by the
# Discord bot's /wake when the server is already running.
# Usage: mc-swap.sh <category>
#
# Only ONE slot runs at a time, so this stops every minecraft@* unit before
# starting the requested category — guaranteeing 100% RAM/CPU to the live world.
set -euo pipefail

. "/mnt/minecraft-data/scripts/mc-common.sh"

CATEGORY="${1:?usage: mc-swap.sh <category>}"

# Re-sync the manifest/scripts/config so a freshly `make set` world is picked up.
echo "Swap: syncing assets from S3"
sync_assets

category_port "$CATEGORY" >/dev/null || { echo "Unknown category: $CATEGORY" >&2; exit 1; }

UUID="$(active_uuid_for "$CATEGORY")"
if [ -z "$UUID" ]; then
  echo "No active world set for category '$CATEGORY'." >&2
  exit 1
fi

# Ensure the target world is provisioned on EBS (idempotent).
"$DATA_DIR/scripts/provision-worlds.sh" "$UUID"

echo "Stopping active slots..."
systemctl stop 'minecraft@*' 2>/dev/null || true

# Record the live category so /status and the next boot agree.
ssm_put active-category "$CATEGORY"

echo "Starting minecraft@$CATEGORY (world $UUID)"
systemctl start "minecraft@$CATEGORY"
echo "Swap complete: $CATEGORY is now live."
