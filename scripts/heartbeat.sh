#!/bin/bash
# Checks if the server is idle (low network traffic) and shuts down if so.
# Designed to run every 20 minutes via cron (/etc/cron.d/mc-heartbeat).
# Only shuts down if a world slot is running — avoids killing a
# stopped-but-not-yet-terminated instance.

THRESHOLD=100    # packets in the sampling window
SAMPLE_SECS=300  # 5-minute window aligns with CloudWatch period

# Only act if a world slot is actually running (any minecraft@<category> unit).
systemctl list-units --state=active --plain --no-legend 'minecraft@*' \
  | grep -q . || exit 0

IFACE=$(ip route get 1.1.1.1 2>/dev/null | awk '{print $5; exit}')
[ -z "$IFACE" ] && exit 1

RX_BEFORE=$(cat "/sys/class/net/$IFACE/statistics/rx_packets")
sleep "$SAMPLE_SECS"
RX_AFTER=$(cat "/sys/class/net/$IFACE/statistics/rx_packets")
DELTA=$(( RX_AFTER - RX_BEFORE ))

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) iface=$IFACE delta=$DELTA threshold=$THRESHOLD"

if [ "$DELTA" -lt "$THRESHOLD" ]; then
  echo "Server idle — initiating shutdown"
  /sbin/shutdown -h +1 "Minecraft server idle shutdown"
fi
