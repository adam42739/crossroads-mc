#!/bin/bash
# Provision world cartridges from the manifest: create the UUID folder, download
# the engine jar, bake server.properties from the world's settings, and write
# world.json. Property baking + jar download happen on FIRST provision only;
# existing world.properties and world data are preserved. The RCON password is
# the exception — it is re-fetched from SSM and re-applied on EVERY provision.
#
# Usage:
#   provision-worlds.sh           # provision every world in the manifest
#   provision-worlds.sh <uuid>    # provision a single world (lazy swap path)
set -euo pipefail

. "$(dirname "$0")/mc-common.sh"

ONLY_UUID="${1:-}"

# ── level-type mapping (manifest → server.properties value) ─────────────────────
# Colons don't need escaping in a property *value*, so keep these sed-safe.
level_type_value() {
  case "$1" in
    flat)         echo 'minecraft:flat' ;;
    large_biomes) echo 'minecraft:large_biomes' ;;
    *)            echo 'minecraft:normal' ;;
  esac
}

# set_prop <file> <key> <value> — replace key=... in place, or append if absent.
set_prop() {
  local file="$1" key="$2" val="$3"
  if grep -q "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$file"
  else
    echo "${key}=${val}" >> "$file"
  fi
}

# ── Engine jar download ─────────────────────────────────────────────────────────
download_vanilla() {
  local version="$1" dest="$2"
  local manifest_url='https://launchermeta.mojang.com/mc/game/version_manifest_v2.json'
  local version_url server_url
  version_url=$(curl -sf "$manifest_url" | jq -r --arg v "$version" '.versions[] | select(.id == $v) | .url')
  [ -n "$version_url" ] || { echo "Vanilla version $version not found." >&2; return 1; }
  server_url=$(curl -sf "$version_url" | jq -r '.downloads.server.url')
  wget -qO "$dest" "$server_url"
}

download_paper() {
  local version="$1" dest="$2"
  local api='https://api.papermc.io/v2/projects/paper'
  local builds build jar
  builds=$(curl -sf "$api/versions/$version/builds") \
    || { echo "Paper has no builds for version $version." >&2; return 1; }
  # Prefer the latest STABLE build; fall back to the latest build of any channel.
  build=$(jq -r '([.builds[] | select(.channel == "STABLE")] | last | .build) // (.builds[-1].build) // empty' <<<"$builds")
  [ -n "$build" ] || { echo "No Paper build for $version." >&2; return 1; }
  jar=$(curl -sf "$api/versions/$version/builds/$build" | jq -r '.downloads.application.name')
  wget -qO "$dest" "$api/versions/$version/builds/$build/downloads/$jar"
}

download_engine() {
  local engine="$1" version="$2" dest="$3"
  case "$engine" in
    vanilla) download_vanilla "$version" "$dest" ;;
    paper)   download_paper "$version" "$dest" ;;
    forge|fabric)
      echo "Engine '$engine' is not yet supported (phase 2). Provision skipped." >&2
      return 2 ;;
    *) echo "Unknown engine '$engine'." >&2; return 1 ;;
  esac
}

# ── Plugin downloads (Paper only) ───────────────────────────────────────────────
# Fetch Bukkit/Paper plugins from Modrinth by project slug into plugins/. Like the
# engine jar, this is FIRST-PROVISION ONLY (skips a slug whose <slug>.jar already
# exists) so repeated boots/swaps don't depend on Modrinth being reachable. To
# update a plugin, delete its jar from the world's plugins/ and re-provision.
#
# Usage: download_plugins <mc_version> <plugins_dir> <slug> [<slug> ...]
download_plugins() {
  local version="$1" pdir="$2"; shift 2
  local slug url
  mkdir -p "$pdir"
  for slug in "$@"; do
    [ -f "$pdir/$slug.jar" ] && continue
    # Newest RELEASE build for this MC version + a Bukkit-family loader; fall back
    # to the newest build of any type. Pick the primary file (else the first).
    url=$(curl -sf -G "https://api.modrinth.com/v2/project/$slug/version" \
            --data-urlencode 'loaders=["paper","spigot","bukkit"]' \
            --data-urlencode "game_versions=[\"$version\"]" 2>/dev/null \
          | jq -r 'map(select(.version_type=="release")) as $rel
                   | (($rel[0] // .[0]).files) as $files
                   | (($files | map(select(.primary)))[0] // $files[0]).url // empty' \
              2>/dev/null) || url=""
    if [ -z "$url" ]; then
      echo "  plugin '$slug': no $version build found on Modrinth — skipped." >&2
      continue
    fi
    echo "  downloading plugin '$slug' for $version"
    wget -qO "$pdir/$slug.jar" "$url" \
      || { echo "  plugin '$slug': download failed — skipped." >&2; rm -f "$pdir/$slug.jar"; }
  done
}

# ── Provision a single world from its manifest JSON object (on stdin) ────────────
provision_one() {
  local world_json="$1"
  local uuid category name engine version gamemode difficulty hardcore level_type seed
  uuid=$(jq -r '.uuid' <<<"$world_json")
  category=$(jq -r '.category' <<<"$world_json")
  name=$(jq -r '.name' <<<"$world_json")
  engine=$(jq -r '.engine' <<<"$world_json")
  version=$(jq -r '.version' <<<"$world_json")
  gamemode=$(jq -r '.settings.gamemode' <<<"$world_json")
  difficulty=$(jq -r '.settings.difficulty' <<<"$world_json")
  hardcore=$(jq -r '.settings.hardcore' <<<"$world_json")
  level_type=$(jq -r '.settings.levelType' <<<"$world_json")
  seed=$(jq -r '.settings.seed // empty' <<<"$world_json")

  local dir="$WORLDS_DIR/$uuid"
  mkdir -p "$dir/mods" "$dir/plugins"

  # world.json — read by start-server.sh for engine/version/java selection.
  echo "$world_json" | jq '{uuid, category, name, engine, version}' > "$dir/world.json"

  # eula — always refreshed from repo template.
  cp "$CONFIG_DIR/eula.txt" "$dir/eula.txt"

  # Engine jar — first provision only.
  if [ ! -f "$dir/server.jar" ]; then
    echo "Downloading $engine $version for world '$name' ($uuid)"
    download_engine "$engine" "$version" "$dir/server.jar" || return $?
  fi

  # Plugins — Paper only (vanilla/forge/fabric can't load Bukkit jars). Slugs come
  # from settings.plugins; download_plugins skips any already-present jar.
  if [ "$engine" = "paper" ]; then
    local plugins
    plugins=$(jq -r '.settings.plugins // [] | .[]' <<<"$world_json")
    if [ -n "$plugins" ]; then
      echo "Installing plugins for '$name': $(echo $plugins | tr '\n' ' ')"
      download_plugins "$version" "$dir/plugins" $plugins
    fi
  fi

  # server.properties — baked from settings on first provision only; preserved
  # afterwards so manual changes survive.
  if [ ! -f "$dir/server.properties" ]; then
    cp "$CONFIG_DIR/server.properties" "$dir/server.properties"
    # gamemode comes straight from the world manifest (settings.gamemode); falls
    # back to survival only if a world predates the field.
    [ -n "$gamemode" ] && [ "$gamemode" != 'null' ] || gamemode='survival'
    set_prop "$dir/server.properties" 'server-port'  "$(category_port "$category")"
    set_prop "$dir/server.properties" 'level-name'   'world'
    set_prop "$dir/server.properties" 'difficulty'   "$difficulty"
    set_prop "$dir/server.properties" 'hardcore'     "$hardcore"
    set_prop "$dir/server.properties" 'gamemode'     "$gamemode"
    set_prop "$dir/server.properties" 'level-type'   "$(level_type_value "$level_type")"
    [ -n "$seed" ] && set_prop "$dir/server.properties" 'level-seed' "$seed"
  fi

  # RCON password — refreshed from the SSM SecureString
  # (/crossroads-mc/rcon-password) on EVERY provision, not just the first. Since
  # provisioning runs on every boot (mc-boot.sh) and swap (mc-swap.sh), rotating
  # the SSM value and rebooting/swapping re-bakes it into server.properties — no
  # need to edit the world's file by hand.
  local rcon_pw
  rcon_pw="$(ssm_get rcon-password)"
  if [ -n "$rcon_pw" ] && [ "$rcon_pw" != "None" ]; then
    set_prop "$dir/server.properties" 'rcon.password' "$rcon_pw"
  fi

  # whitelist — union-merge repo admins with any existing per-world list.
  [ -f "$dir/whitelist.json" ] || echo '[]' > "$dir/whitelist.json"
  jq -s 'add | unique_by(.uuid)' "$CONFIG_DIR/whitelist.json" "$dir/whitelist.json" \
    > "$dir/whitelist.json.tmp" && mv "$dir/whitelist.json.tmp" "$dir/whitelist.json"

  # The server runs as `ubuntu`; provisioning may run as root (boot/swap).
  chown -R ubuntu:ubuntu "$dir" 2>/dev/null || true

  echo "Provisioned world '$name' ($uuid) [$category/$engine/$version]"
}

# ── Main ────────────────────────────────────────────────────────────────────────
mkdir -p "$WORLDS_DIR"

if [ -n "$ONLY_UUID" ]; then
  WORLD=$(world_by_uuid "$ONLY_UUID") || { echo "World $ONLY_UUID not in manifest." >&2; exit 1; }
  provision_one "$WORLD"
else
  while IFS= read -r world; do
    provision_one "$world" || echo "  (skipped — see message above)"
  done < <(jq -c '.worlds[]' "$MANIFEST")
fi
