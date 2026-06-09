#!/bin/bash
# Shared helpers for the Crossroads multi-world scripts.
# Source this file: . "$(dirname "$0")/mc-common.sh"

DATA_DIR="${DATA_DIR:-/mnt/minecraft-data}"
WORLDS_DIR="$DATA_DIR/worlds"
# Combined manifest: { "categories": [...], "worlds": [...] }. Re-synced from S3
# on every boot/swap by sync_assets, so `make set` + cdk deploy takes effect
# without an instance replacement.
MANIFEST="$DATA_DIR/manifest.json"
# Persistent copy of the repo config templates (eula/server.properties/whitelist).
CONFIG_DIR="$DATA_DIR/config-template"
SSM_PREFIX="${SSM_PREFIX:-/crossroads-mc}"

# category_port <category> → prints the slot's public port from the manifest.
category_port() {
  local port
  port="$(jq -r --arg c "$1" '.categories[] | select(.name == $c) | .port' "$MANIFEST" 2>/dev/null)"
  [ -n "$port" ] && [ "$port" != "null" ] || return 1
  echo "$port"
}

# active_uuid_for <category> → prints the category's active world UUID (empty if unset).
active_uuid_for() {
  jq -r --arg c "$1" '.categories[] | select(.name == $c) | .activeWorld // empty' \
    "$MANIFEST" 2>/dev/null
}

# Heap size (MiB) for the JVM, derived from the box's actual RAM so it scales
# with instance_type instead of being hardcoded. The JVM's total RSS runs well
# beyond -Xmx (Metaspace, thread stacks, Netty direct buffers, GC structures —
# ~1-1.5 GB), so we reserve the larger of 2 GiB or 20% of RAM for the OS + that
# overhead to stay clear of the Linux OOM-killer. On an 8 GB box this yields
# ~5.8 GB (vs. the old fixed 7 GB).
mc_heap_mb() {
  local total reserve
  total="$(awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo)"
  reserve=$(( total / 5 ))
  [ "$reserve" -lt 2048 ] && reserve=2048
  echo $(( total - reserve ))
}

# Map a Minecraft version to a system JDK home. 1.20.5+ requires Java 21.
# The JVM dir is arch-suffixed (amd64 on x86_64, arm64 on Graviton), so derive
# it from dpkg rather than hardcoding — keeps this portable across instance archs.
java_home_for_version() {
  local arch
  arch="$(dpkg --print-architecture)"
  case "$1" in
    1.20.1) echo "/usr/lib/jvm/java-17-openjdk-${arch}" ;;
    *)      echo "/usr/lib/jvm/java-21-openjdk-${arch}" ;;
  esac
}

# Region from instance metadata (IMDSv2).
mc_region() {
  local token
  token=$(curl -sf -X PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60" 2>/dev/null) || return 1
  curl -sf -H "X-aws-ec2-metadata-token: $token" \
    http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null
}

# ssm_get <param-name-under-prefix>  → prints value, empty on miss.
# --with-decryption is harmless for String params and required for SecureString
# (e.g. the RCON password).
#
# A missing parameter makes the AWS CLI exit 254 (service error). Callers use
# `VAR="$(ssm_get ...)"` under `set -e`, so without the `|| true` an unset
# parameter (e.g. active-category on first boot, or an unconfigured
# rcon-password) would abort the whole script before it could act on the miss.
ssm_get() {
  local name="$SSM_PREFIX/$1"
  aws ssm get-parameter --name "$name" --with-decryption --region "$(mc_region)" \
    --query 'Parameter.Value' --output text 2>/dev/null || true
}

# ssm_put <param-name-under-prefix> <value>
ssm_put() {
  aws ssm put-parameter --name "$SSM_PREFIX/$1" --value "$2" \
    --type String --overwrite --region "$(mc_region)" >/dev/null
}

# Look up a world object in the manifest by uuid → prints the JSON object.
world_by_uuid() {
  jq -e --arg u "$1" '.worlds[] | select(.uuid == $u)' "$MANIFEST" 2>/dev/null
}

# Pull the latest manifest, scripts, and config templates from S3. Asset URLs are
# published to SSM by CDK and change on every deploy, so reading them here is what
# lets a plain stop/start (or a swap) pick up new worlds/config without a full
# instance replacement.
#
# Staging lives on the data volume so files move into place via same-filesystem
# rename — a currently-running script keeps executing its original inode safely.
sync_assets() {
  local region scripts_url config_url manifest_url stage f
  region="$(mc_region)"
  scripts_url="$(ssm_get asset/scripts)"
  config_url="$(ssm_get asset/config)"
  manifest_url="$(ssm_get asset/manifest)"

  stage="$(mktemp -d "$DATA_DIR/.sync.XXXXXX")" || return 1

  # Manifest — standalone JSON file; copy straight into place.
  if [ -n "$manifest_url" ] && [ "$manifest_url" != "None" ]; then
    aws s3 cp "$manifest_url" "$stage/manifest.json" --region "$region" >/dev/null \
      && mv -f "$stage/manifest.json" "$MANIFEST"
  fi

  # Scripts — zipped asset (flat directory).
  if [ -n "$scripts_url" ] && [ "$scripts_url" != "None" ]; then
    if aws s3 cp "$scripts_url" "$stage/scripts.zip" --region "$region" >/dev/null \
        && unzip -oq "$stage/scripts.zip" -d "$stage/scripts"; then
      mkdir -p "$DATA_DIR/scripts"
      for f in "$stage/scripts"/*; do mv -f "$f" "$DATA_DIR/scripts/"; done
      chmod +x "$DATA_DIR/scripts/"*.sh
    fi
  fi

  # Config templates — zipped asset.
  if [ -n "$config_url" ] && [ "$config_url" != "None" ]; then
    if aws s3 cp "$config_url" "$stage/config.zip" --region "$region" >/dev/null \
        && unzip -oq "$stage/config.zip" -d "$stage/config"; then
      mkdir -p "$CONFIG_DIR"
      cp -f "$stage/config/eula.txt" "$stage/config/server.properties" \
        "$stage/config/whitelist.json" "$CONFIG_DIR/"
    fi
  fi

  rm -rf "$stage"
}

# Read a property value from a world's server.properties.
prop_get() {
  local file="$1" key="$2"
  sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -1
}
