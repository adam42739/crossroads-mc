#!/bin/bash
# Launch the active world for a given category slot.
# Usage: start-server.sh <category>
#
# Resolves the active world UUID from the manifest (categories[].activeWorld),
# reads that world's engine/version from its baked world.json, selects the
# matching JDK, and launches the engine jar from inside the world's UUID folder.
set -euo pipefail

. "$(dirname "$0")/mc-common.sh"

CATEGORY="${1:?usage: start-server.sh <category>}"
PORT="$(category_port "$CATEGORY")" || { echo "Unknown category: $CATEGORY" >&2; exit 1; }

UUID="$(active_uuid_for "$CATEGORY")"
if [ -z "$UUID" ]; then
  echo "No active world set for category '$CATEGORY' (manifest activeWorld)." >&2
  exit 1
fi

WORLD_DIR="$WORLDS_DIR/$UUID"
if [ ! -d "$WORLD_DIR" ] || [ ! -f "$WORLD_DIR/server.jar" ]; then
  echo "World '$UUID' is not provisioned at $WORLD_DIR." >&2
  exit 1
fi

VERSION="$(jq -r '.version' "$WORLD_DIR/world.json")"
JAVA_HOME="$(java_home_for_version "$VERSION")"
JAVA_BIN="$JAVA_HOME/bin/java"
if [ ! -x "$JAVA_BIN" ]; then
  echo "JDK for version $VERSION not found at $JAVA_BIN." >&2
  exit 1
fi

HEAP_MB="$(mc_heap_mb)"

echo "Starting $CATEGORY world $UUID (version $VERSION) on port $PORT with ${HEAP_MB}M heap"
cd "$WORLD_DIR"

# Heap sized from the box's RAM (see mc_heap_mb) so it scales with instance_type
# and leaves headroom for JVM overhead + OS, clear of the OOM-killer. Xms==Xmx
# (Aikar's flags) to avoid heap-resize pauses. G1GC tuned for low pause.
exec "$JAVA_BIN" \
  -Xmx"${HEAP_MB}m" -Xms"${HEAP_MB}m" \
  -XX:+UseG1GC \
  -XX:+ParallelRefProcEnabled \
  -XX:MaxGCPauseMillis=200 \
  -XX:+UnlockExperimentalVMOptions \
  -XX:+DisableExplicitGC \
  -XX:G1NewSizePercent=30 \
  -XX:G1MaxNewSizePercent=40 \
  -XX:G1HeapRegionSize=8M \
  -XX:G1ReservePercent=20 \
  -XX:G1HeapWastePercent=5 \
  -XX:G1MixedGCCountTarget=4 \
  -XX:InitiatingHeapOccupancyPercent=15 \
  -XX:G1MixedGCLiveThresholdPercent=90 \
  -XX:G1RSetUpdatingPauseTimePercent=5 \
  -XX:SurvivorRatio=32 \
  -XX:+PerfDisableSharedMem \
  -XX:MaxTenuringThreshold=1 \
  -jar server.jar --port "$PORT" nogui
