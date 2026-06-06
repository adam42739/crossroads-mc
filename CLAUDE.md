# Crossroads MC — Architecture Reference

## Repository Layout

```
crossroads-mc/
├── Makefile                   # make category|world|set|register-commands
├── infra/
│   ├── bin/crossroads-mc.ts   # CDK app entry; loads config, scans + validates manifests, wires the stack
│   ├── config.json            # Global settings: {domain_name, instance_type, ebs_size}
│   ├── lib/
│   │   ├── minecraft-stack.ts # Single stack — all AWS resources defined here
│   │   └── manifests.ts       # Manifest + config loaders/validators + types
│   └── manifests/
│       ├── categories/<name>.json  # one file per category: {name, port, activeWorld}
│       └── worlds/<uuid>.json       # one file per world: {uuid, category, name, engine, version, settings}
├── scripts/                   # Instance-side shell (synced to EBS as a CDK asset)
│   ├── mc-common.sh           # Shared helpers (ports, JDK map, SSM, manifest, sync_assets)
│   ├── start-server.sh        # Per-category JVM launch (resolves active world from manifest)
│   ├── minecraft@.service     # Templated systemd unit (one instance per category)
│   ├── mc-swap.sh             # Live-slot switch (re-sync + stop/start), invoked by the bot
│   ├── provision-worlds.sh    # Engine bootstrap + property baking per world
│   ├── mc-boot.sh             # Boot-time: re-sync assets, seed live pointer, start live slot
│   ├── mc-boot.service        # Enabled unit that runs mc-boot.sh every boot
│   ├── heartbeat.sh           # Idle-detection cron script
│   └── disk-check.sh          # Publishes EBS used-% as a CloudWatch custom metric (cron)
├── util/                      # Local-only Python helpers (run via uv, behind make)
│   ├── manifest_tool.py       # Backs the make commands (create/edit manifest JSON)
│   └── register-commands.py   # Register Discord slash commands
├── config/
│   ├── eula.txt
│   ├── server.properties      # BASE template; manifest settings baked over it per world
│   └── whitelist.json         # Repo admins list; union-merged into each world
└── discord-bot/               # Discord bot Lambda (container image)
    ├── Dockerfile             # Built/pushed to ECR by CDK (DockerImageFunction)
    ├── pyproject.toml         # Runtime deps (pynacl, boto3)
    └── src/discord_handler.py # /wake, /status
```

## The "Cartridge & Slot" Model

The environment is decoupled into **Slots** and **Cartridges**:

- **Slots** — categories, one JSON file each under `infra/manifests/categories/`.
  Each declares a public `port` and an `activeWorld` pointer. The hosted zone, an
  `server.<domain>` A record, and a `_minecraft._tcp.<category>.<domain>` SRV
  record per slot are all created in the CDK via Route 53.
- **Cartridges** — worlds, one JSON file each under `infra/manifests/worlds/`.
  Each world is a self-contained `worlds/<UUID>/` folder on EBS.
- **Concurrency** — only **one** slot runs at a time, so the live world gets 100%
  of RAM/CPU. Enforced by stopping all `minecraft@*` units before starting one.

### No-code worlds & categories

Adding a world or category requires **no CDK/TypeScript changes** — you drop a
JSON file under `infra/manifests/` (via the `make` commands) and run `cdk deploy`.
`infra/bin/crossroads-mc.ts` scans both manifest directories at synth time,
validates them (`manifests.ts`), and injects a combined manifest into the instance
and the Lambda.

### Storage — EBS only

All world data lives on the **EBS data volume**; there is **no S3 tiering**. A
world is provisioned onto EBS the first time its category is woken/started and
stays there. The `minecraft-disk-usage` CloudWatch alarm watches a custom metric
(`CrossroadsMC/DiskUsedPercent`) published by `disk-check.sh` and fires when the
volume passes ~85% full.

### Manifest vs. runtime pointer

- **Combined manifest** = `{ categories: [...], worlds: [...] }`, built at synth
  from the manifest files. It is the deploy-time *universe* and also carries each
  category's `activeWorld` (set via `make set`). Injected to the instance
  (`/mnt/minecraft-data/manifest.json`) and the Lambda (`MANIFEST_JSON`).
  - Which world a category runs = `activeWorld` in the category manifest →
    changed with **`make set` + `cdk deploy`**, never by the bot.
- **Runtime pointer** (SSM Parameter Store):
  - `/crossroads-mc/active-category` → which slot is currently live. Seeded by
    `mc-boot.sh` if unset; set by the bot's `/wake` and by `mc-swap.sh`.
  - `/crossroads-mc/asset/{scripts,config,manifest}` → S3 URLs of the latest CDK
    assets. Written by CDK on every deploy (the keys are content-hashed and change
    each deploy). The instance reads these to re-sync on boot/swap.
- **Manually-created SSM params** (CDK only grants read; you create the values):
  - `/crossroads-mc/discord/public-key` (String) → Ed25519 verification key, read
    by the Lambda on cold start.
  - `/crossroads-mc/discord/admin-role-id` (String) → Discord role allowed `/wake`.
  - `/crossroads-mc/rcon-password` (SecureString) → re-baked into a world's
    `server.properties` on **every** provision (every boot/swap), so rotating it
    and rebooting takes effect.

### Re-sync model (no instance replacement needed)

CDK asset S3 keys change on every deploy, so the instance can't hard-code them.
Instead CDK publishes each asset's S3 URL to SSM, and `sync_assets` (in
`mc-common.sh`) reads those params and re-downloads **manifest + scripts + config**
from S3. It runs at the top of `mc-boot.sh` (every boot) and `mc-swap.sh` (every
swap), so `make set` + `cdk deploy` + the next `/wake` (or stop/start) picks up the
new active world — no instance replacement required.

| Category | Port (manifest) | SRV record (Route 53) |
|---|---|---|
| Hardcore | 25565 | `_minecraft._tcp.hardcore.crossroads-mc.net` |
| Creative | 25566 | `_minecraft._tcp.creative.crossroads-mc.net` |
| Survival | 25567 | `_minecraft._tcp.survival.crossroads-mc.net` |

(Ports are whatever the category manifests declare; the table reflects the seeded
set. Each SRV target is `0 5 <port> server.<domain>`; `server.<domain>` is an A
record to the Elastic IP. `<domain>` comes from `infra/config.json`.)

## CDK Stack (`MinecraftStack`)

Single stack deployed to `us-west-2`. All resources tagged `Project=CrossroadsMC`.

### Global Config (`infra/config.json`)

Loaded + validated by `loadConfig()` in `manifests.ts`; fails synth if malformed.

| Field | Effect |
|---|---|
| `domain_name` | Route 53 hosted-zone name; drives `server.<domain>` + SRV records and the `/status` connect host |
| `instance_type` | EC2 instance type for the game server |
| `ebs_size` | Persistent EBS data-volume size (GiB) |

### Context Parameters

| Parameter | How to pass | Default | Effect |
|---|---|---|---|
| `sshCidr` | `--context sshCidr=x.x.x.x/32` | `0.0.0.0/0` | Restricts SSH (22) and RCON (25575) ingress |

(Discord public key / admin role id moved to SSM — see "Manually-created SSM params".)

### Infrastructure Components

**VPC** — Single public subnet in one AZ, no NAT gateway.

**Security Group** — Ingress: one open TCP rule **per category** (port from the
manifest); `22` and `25575` (RCON) restricted to `sshCidr`. All category ports stay
open statically — `/wake` never mutates the SG; the new slot simply binds its port.

**Elastic IP** — Static IP; re-associated by user data on every instance
replacement so the server IP never changes.

**Route 53** — A CDK-created public hosted zone for `domain_name`, a `server.`
A record → EIP, and one `_minecraft._tcp.<category>` SRV record per slot. The
stack outputs `HostedZoneNameServers`; repoint the registrar to these to make
Route 53 authoritative.

> **DNS status (current): Route 53 is provisioned but DORMANT.** Authoritative
> DNS is served by **Cloudflare** today — the registrar's nameservers have **not**
> been repointed to the `HostedZoneNameServers` output. The Route 53 hosted zone
> and its records still deploy with the stack (and we keep paying for / maintaining
> them) but are not in the resolution path. Live records (e.g.
> `test.crossroads-mc.net`) are managed manually in Cloudflare. The plan is to
> transfer the domain's authoritative nameservers to AWS in a few months and pick
> up Route 53 where we left off; until then, treat the Route 53 records as
> deploy-time scaffolding, not live DNS, and make DNS changes in Cloudflare.

**EBS Data Volume** — GP3 sized from `config.json` `ebs_size` (`/dev/sdh`, mounted
at `/mnt/minecraft-data`), `RemovalPolicy.RETAIN`. Holds **all** worlds plus
scripts, manifest, and config templates.

**EC2 Instance** — type from `config.json` `instance_type`, Ubuntu 24.04,
`requireImdsv2`.

**IAM Role (instance)** — `AmazonSSMManagedInstanceCore`, `ec2:AssociateAddress`/
`DescribeAddresses` (EIP), `ssm:GetParameter(s)`/`PutParameter` on
`/crossroads-mc/*` (active-category + asset URLs + rcon-password),
`kms:Decrypt` (via SSM, for the SecureString RCON password),
`cloudwatch:PutMetricData` (disk metric), and read on the three CDK asset objects.
**No S3 world bucket.**

**Discord Bot Lambda** — Python 3.12, Function URL (auth NONE), fixed name
`crossroads-mc-discord-bot`. Role grants `ec2:Describe/Start/StopInstances`
(scoped), `ssm:SendCommand` (mc-swap via Run Command), `ssm:GetParameter`/
`PutParameter` on `/crossroads-mc/*`, and `lambda:InvokeFunction` on itself (the
deferred-response self-invoke — see below). Env: `INSTANCE_ID`, `SERVER_HOSTNAME`
(= `server.<domain>`), `SSM_PREFIX`, `MANIFEST_JSON`. The Discord public key +
admin role id are read from `/crossroads-mc/discord/*` at cold start (not env vars).

**CloudWatch Alarms** — `minecraft-idle` (network, monitoring only; `heartbeat.sh`
does the actual shutdown) and `minecraft-disk-usage` (EBS used-% custom metric).

## EBS Layout

```
/mnt/minecraft-data/
├── manifest.json              # Combined {categories, worlds}; re-synced each boot/swap
├── config-template/           # Persisted repo configs (eula/server.properties/whitelist)
├── scripts/                   # Re-synced from the CDK scripts asset each boot/swap
└── worlds/<UUID>/             # All provisioned worlds live here (no S3 tiering)
    ├── server.jar             # Engine (vanilla or paper)
    ├── plugins/<slug>.jar     # Paper plugins from settings.plugins (Modrinth, first provision)
    ├── mods/  world/
    ├── server.properties      # Baked from manifest settings (first provision only)
    └── world.json             # {uuid, category, name, engine, version}
```

Both JDKs are installed via apt (`java-17` for 1.20.1, `java-21` for 1.20.5+);
`start-server.sh` selects per world version.

## User Data Boot Sequence (first boot / instance replacement)

1. Dependencies — `openjdk-17-jre-headless openjdk-21-jre-headless jq unzip awscli wget`
2. EIP association (IMDSv2)
3. EBS mount (polls for the data disk; formats ext4 first boot only; `/etc/fstab` `nofail`)
4. Bootstrap the `scripts/` asset from S3 (just enough to run `mc-boot`)
5. Install `minecraft@.service` + `mc-boot.service`
6. `systemctl enable --now mc-boot.service` — re-syncs assets, seeds the live
   pointer, starts the live slot
7. Heartbeat + disk-check crons

**Note:** Ubuntu cloud-init runs user data once per instance, so a plain
stop/start does **not** re-run it. `mc-boot.service` is `enable`d, so it runs on
**every** boot: it `sync_assets` (re-pulling manifest/scripts/config from S3),
seeds the SSM `active-category` if needed, and starts the live slot.

## Live-Slot Switch (`mc-swap.sh <category>`)

Invoked on the instance via SSM Run Command by the bot's `/wake` when the server is
already running.

1. `sync_assets` — re-pull manifest/scripts/config from S3.
2. Resolve the target's `activeWorld` UUID from the manifest.
3. `provision-worlds.sh <uuid>` (idempotent — engine/properties only on first run).
4. `systemctl stop 'minecraft@*'` (graceful save & stop).
5. `ssm_put active-category <category>`; `systemctl start minecraft@<category>`.

## Discord Bot (`discord-bot/src/discord_handler.py`)

Ed25519 signature verification on every request.

**Deferred two-phase response.** To stay inside Discord's 3s interaction deadline
(cold starts + EC2/SSM calls can blow past it), the handler does **not** answer
commands inline. After verifying the signature it async-invokes itself
(`InvocationType="Event"`, payload `{crossroads_deferred, interaction}`) and
immediately returns a type-5 `DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE` ACK. The
second (async) invocation runs the actual command, then PATCHes the placeholder via
`webhooks/<app_id>/<token>/messages/@original` (the interaction token authorizes
it — no bot token). PING (type 1) and signature failures are still answered inline.

Commands:

- **`/wake <category>`** (admin) — if instance stopped: set `active-category` +
  `StartInstances` (mc-boot launches that slot); if running: SSM hot-swap via
  `mc-swap.sh`.
- **`/status`** (public) — instance power state + the active world per category
  (read from the manifest; live slot from `active-category`).

Slash-command choices for `/wake` are built from the category manifests. Register
with `make register-commands` (`util/register-commands.py`, run locally). There
is **no `/swap`** — world→category assignment is `make set` + `cdk deploy`.

## Config Persistence Model (per world)

| File | First provision | Re-provision / replacement boot |
|---|---|---|
| `eula.txt` | Written from repo | Overwritten from repo |
| `server.properties` | Baked from manifest settings + RCON password from SSM | **Preserved**, except `rcon.password` re-fetched from SSM each provision |
| `whitelist.json` | Union-merged | Union-merged |
| `server.jar` | Downloaded (vanilla/paper) | Preserved |
| `plugins/<slug>.jar` | Downloaded from Modrinth (Paper, `settings.plugins`) | Preserved (re-fetched only if the jar is missing) |
| `world.json` | Written from manifest | Refreshed from manifest |
| World data | Created by server | Preserved |

## Working in This Repo

- **Add a category:** `make category CATEGORY=<name> PORT=<port>` (checks the name
  and port are free), then `cdk deploy` (Route 53 SRV record is created from the
  manifest — no manual DNS step).
- **Add a world:** `make world CATEGORY=<name> NAME="<world>"` (prints a UUID; the
  world file is left blank), fill in `engine`/`version`/`settings`, then make it
  active and deploy.
- **Point a slot at a world:** `make set CATEGORY=<name> UUID=<uuid>` (requires the
  world to be fully configured), then `cdk deploy`.
- **Engines:** `vanilla` and `paper` are fully supported. `forge`/`fabric` are
  stubbed — `provision-worlds.sh` skips them (phase 2).
- **Plugins (Paper only):** a world's `settings.plugins` is a list of Modrinth
  project slugs (e.g. `["viaversion","viabackwards","viarewind"]` for cross-version
  client support). `provision-worlds.sh` downloads each to `worlds/<uuid>/plugins/<slug>.jar`
  on **first provision only** (skips a slug whose jar already exists), resolving the
  newest release build for the world's version + a Bukkit-family loader from the
  Modrinth API. Validation rejects `plugins` on a non-`paper` engine. To update a
  plugin, delete its jar from EBS and re-provision (`/wake` or reboot). A Modrinth
  miss for one slug is logged and skipped, not fatal.
- Changes to `scripts/`, `config/`, or the manifests are bundled as S3 assets on
  `cdk deploy`; a running instance picks them up on the next boot or swap (via
  `sync_assets`), not live.
- RCON passwords are **never committed**. Set the SecureString
  `/crossroads-mc/rcon-password`; it's re-baked into each world's
  `server.properties` on every provision (every boot/swap), so rotating the SSM
  value and rebooting (or swapping) re-applies it without editing world files.
- **Manual SSM params** (create before first deploy):
  `/crossroads-mc/discord/public-key` (String),
  `/crossroads-mc/discord/admin-role-id` (String),
  `/crossroads-mc/rcon-password` (SecureString).
- **First deploy only:** repoint the domain's registrar nameservers to the
  stack's `HostedZoneNameServers` output. **Deferred:** DNS currently runs through
  Cloudflare and Route 53 is dormant — do this only when cutting authoritative DNS
  over to AWS (planned in a few months). See the "Route 53" note above; manage live
  records in Cloudflare until then.
- TypeScript type-check: `cd infra && npx tsc --noEmit`

### Redeployment workflow

**Golden rule:** an EC2 **instance replacement** happens *only* when the user-data
text changes — i.e. when you edit `buildUserData` in `lib/minecraft-stack.ts`.
Adding/editing **worlds, categories, `scripts/`, or `config/` never touches
user-data**, so they deploy through the `sync_assets` re-sync model with **no
instance replacement**. Replacement is the only path that hits the
volume-attachment deadlock (the "User-data change" row below) — its recovery is
`stop instance → detach data volume → cdk deploy`.

**Always diff first — it tells you which path you're on:**

```bash
cd infra && npx cdk diff      # look for McServer being REPLACED
npx cdk deploy                # safe if the instance is not replacing
```

If `cdk diff` shows `McServer` replacing, you edited user-data → use the
stop→detach→deploy procedure. For worlds/scripts/config you'll never see that.

| Change | Steps | Goes live |
|---|---|---|
| **Add a world** | `make world …`, fill in the JSON, (`make set` if activating), `cdk diff` + `cdk deploy` | Provisioned lazily on the next start/swap of its category; `/wake <category>` to apply now |
| **Update `scripts/` or `config/`** | edit, `cdk diff` + `cdk deploy` | Picked up via `sync_assets` on the next boot or swap — `/wake <category>` or reboot to apply now (server restart; no hot reload) |
| **Add a category** | `make category …`, `cdk diff` + `cdk deploy`, then **`make register-commands`** | New SG port + SRV record on deploy; bot `/wake` choice after re-registering |
| **User-data change** (`buildUserData`) | stop instance → detach data volume → `cdk deploy` (instance is replaced) | On the replacement instance's first boot |

Notes:
- `cdk deploy` requires **Docker running** (it rebuilds the Discord-bot Lambda image), even for a scripts-only change.
- Applying scripts/config to a running server always means a **slot restart** (brief player disconnect) — there is no live reload.
