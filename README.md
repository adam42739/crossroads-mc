# Crossroads MC

AWS-hosted multi-world Minecraft Java server. Infrastructure is a single CDK stack (`us-west-2`): one EC2 instance, one persistent EBS data volume, one Elastic IP, and a Discord-bot Lambda. Game data survives instance replacements; the server IP never changes.

Worlds use a **"Cartridge & Slot"** model: category *slots* (e.g. Hardcore → `25565`, Creative → `25566`, Survival → `25567`), each backed by a swappable *world*. Categories and worlds are plain JSON files under `infra/manifests/` — adding one needs **no code changes**, just a `make` command and a `cdk deploy`. Only one slot runs at a time so the live world gets the full instance. All world data lives on the EBS volume (no S3 tiering). See `CLAUDE.md` for the full architecture.

---

## Prerequisites

| Tool | Minimum version | Install |
|---|---|---|
| Node.js | 20 (LTS) | `nvm install 20` |
| AWS CDK | 2 | `npm install -g aws-cdk` |
| AWS CLI | 2 | [docs.aws.amazon.com/cli](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) |
| Docker | 20+ | Required at deploy time — CDK builds the Discord-bot Lambda image |
| make | any | Drives the manifest + command-registration targets |
| uv | latest | Runs the Python helpers behind `make` ([astral.sh/uv](https://docs.astral.sh/uv/)) |

---

## AWS Configuration

### 1. Configure credentials

```bash
aws login
```

### 2. SSM Parameter Store

Three runtime values live in SSM and are **not** created by CDK. Create them once before the first deploy:

```bash
aws ssm put-parameter --name /crossroads-mc/discord/public-key    --type String       --value <ed25519-public-key-hex> --region us-west-2
aws ssm put-parameter --name /crossroads-mc/discord/admin-role-id --type String       --value <discord-role-id>       --region us-west-2
aws ssm put-parameter --name /crossroads-mc/rcon-password         --type SecureString --value <rcon-password>         --region us-west-2
```

To change a value later, re-run the command with `--overwrite`.

---

## Deploy

SSH and RCON access is restricted to a single CIDR. Get your current public IP:

```bash
curl -s https://checkip.amazonaws.com
```

Use it as `YOUR_IP/32` in the deploy command:

```bash
cd infra
npm install          # first time only
cdk deploy --context sshCidr=YOUR_IP/32
```

## Managing Worlds & Categories

Categories and worlds are JSON files under `infra/manifests/`. The `make` targets
create/edit them; nothing else changes. After any of these, run `cd infra && cdk deploy`.

```bash
# New category slot (validates name + port are free). Route 53 SRV record is
# created from the manifest on deploy — no manual DNS step.
make category CATEGORY=skyblock PORT=25568

# New (blank) world in a category — prints a generated UUID.
make world CATEGORY=skyblock NAME="Sky Islands"

# Fill in engine/version/settings in infra/manifests/worlds/<uuid>.json, then:
make set CATEGORY=skyblock UUID=<uuid>
```

`make set` requires the world to be fully configured (engine + version + settings).
A running server picks up the new active world on its next `/wake` or stop/start —
no instance replacement needed.

### Plugins (Paper only) — e.g. cross-version client support

A Paper world can declare a `plugins` list in its `settings` — Modrinth project
slugs that are downloaded into the world's `plugins/` folder on first provision.
The common case is letting clients on **different Minecraft versions** connect via
the ViaVersion family:

```jsonc
// infra/manifests/worlds/<uuid>.json
{
  "uuid": "…", "category": "test", "name": "Paper world",
  "engine": "paper", "version": "1.21",
  "settings": {
    "gamemode": "survival", "difficulty": "normal",
    "hardcore": false, "levelType": "default",
    "plugins": ["viaversion", "viabackwards", "viarewind"]
  }
}
```

- **viaversion** — lets *newer* clients join an older server.
- **viabackwards** — lets *older* clients join.
- **viarewind** — extends support to very old clients (1.8.x / 1.7).

Together, a recent Paper server accepts clients from roughly **1.8 → latest**, at the
same address/port — no client mods. Notes:

- Plugins require `engine: "paper"` (vanilla/forge/fabric can't load them) — the
  manifest validator enforces this at synth.
- Downloads happen on **first provision only** (a slug whose `<slug>.jar` already
  exists is skipped), so routine boots don't depend on Modrinth. To update a plugin,
  delete its jar from `worlds/<uuid>/plugins/` on the EBS volume and re-provision
  (`/wake` or reboot). A Modrinth miss for one slug is logged and skipped, not fatal.

---

## Whitelist Players

Add players to `config/whitelist.json` in the repo:

```json
[
  { "uuid": "player-uuid-here", "name": "PlayerName" }
]
```

The list is union-merged into each world's whitelist when it is provisioned — existing players on the EBS are never removed. To apply immediately on the live server, restart the slot or reload the whitelist via RCON.

---

## Day-to-Day Operations

### Check server status

```bash
aws ssm start-session --target INSTANCE_ID --region us-west-2
systemctl list-units 'minecraft@*'        # which slot is live
journalctl -u 'minecraft@*' -f            # live logs
```

### Idle auto-shutdown

`heartbeat.sh` runs every 20 minutes. If a `minecraft@*` slot is running and fewer than 500 network packets are received in a 5-minute window, it issues `shutdown -h +1`. The CloudWatch `minecraft-idle` alarm provides a separate signal for monitoring but does not trigger shutdown on its own.

### Disk-usage alarm

`disk-check.sh` runs every 5 minutes and publishes the data volume's used percentage as the `CrossroadsMC/DiskUsedPercent` custom metric. The `minecraft-disk-usage` CloudWatch alarm fires when the EBS volume passes ~85% full (all worlds live on EBS, so this is the signal to grow the volume).

### Tear down

```bash
cdk destroy
```

The EBS data volume has `RemovalPolicy.RETAIN` — it is **not** deleted on `cdk destroy`. Delete it manually (EC2 console) if you no longer need the world data.

---

## Redeployment Workflow

For everyday changes — **adding worlds, editing `scripts/`, or editing `config/`** —
redeploying is routine and does **not** replace the EC2 instance.

**Golden rule:** the instance is replaced *only* when the **user-data** changes
(i.e. you edit `buildUserData` in `infra/lib/minecraft-stack.ts`). Worlds, scripts,
config, and manifests bundle as S3 assets and are picked up by the running instance
via `sync_assets` on its next boot or slot swap — no replacement. Only the
replacement path triggers the volume-attachment deadlock (see below).

**Always diff first — it tells you which path you're on:**

```bash
cd infra
cdk diff --context sshCidr=YOUR_IP/32     # scan for "McServer" being REPLACED
cdk deploy --context sshCidr=YOUR_IP/32    # safe when the instance is not replacing
```

If `cdk diff` does **not** show `McServer` replacing, it's a clean deploy. (`cdk deploy`
requires Docker running — it rebuilds the Discord-bot Lambda image, even for a
scripts-only change.)

### Adding a world

```bash
make world CATEGORY=<name> NAME="<world>"   # prints a UUID; file left blank
# fill in engine/version/settings in infra/manifests/worlds/<uuid>.json
make set CATEGORY=<name> UUID=<uuid>        # only if making it the active world
cd infra && cdk diff && cdk deploy
```

The world is provisioned **lazily** the first time its category starts or swaps. To
make a newly-`set` world live on a running server, run `/wake <category>` from the
Discord bot (re-syncs, provisions, restarts the slot). On a stopped server it comes
up on next boot.

### Updating scripts or config

```bash
# edit scripts/*.sh or config/*
cd infra && cdk diff && cdk deploy
```

The new files are now in S3, but the running instance is still executing the old
copy. **Apply them** by triggering a re-sync, which restarts the slot (brief player
disconnect — there is no hot reload):

- `/wake <category>` from the bot → `mc-swap.sh` re-syncs + restarts, **or**
- `aws ec2 reboot-instances --instance-ids INSTANCE_ID --region us-west-2` → `mc-boot.sh` re-syncs + restarts.

If the server is idle/stopped, it just picks them up on the next boot — nothing to do.

### User-data changes (rare — replaces the instance)

Editing `buildUserData` changes the user-data text, and `userDataCausesReplacement`
forces a new instance. CloudFormation replaces create-before-delete, so the new
instance can't attach the `RETAIN` data volume while the old one holds it — the
deploy fails on `McDataAttachment` ("already attached"). Recovery (data volume is
`RETAIN`, so no data loss):

```bash
# 1. Wait for UPDATE_ROLLBACK_COMPLETE
aws cloudformation describe-stacks --stack-name CrossroadsMcStack --region us-west-2 \
  --query 'Stacks[0].StackStatus' --output text
# 2. Stop the instance (clean unmount), then detach the data volume
aws ec2 stop-instances  --instance-ids INSTANCE_ID --region us-west-2
aws ec2 wait instance-stopped --instance-ids INSTANCE_ID --region us-west-2
aws ec2 detach-volume   --volume-id DATA_VOLUME_ID --region us-west-2
aws ec2 wait volume-available --volume-ids DATA_VOLUME_ID --region us-west-2
# 3. Deploy — the replacement instance now attaches the available volume
cd infra && cdk deploy
```
