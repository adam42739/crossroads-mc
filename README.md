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
aws configure
```

Enter your Access Key ID, Secret Access Key, and set the default region to `us-west-2`.

Alternatively, use SSO or an assumed role — the CDK just needs a resolved credential chain.

### 2. Required IAM permissions

The deploying identity needs at minimum:

- `AWSCloudFormationFullAccess`
- `AmazonEC2FullAccess`
- `IAMFullAccess` (CDK creates instance roles)
- `AmazonS3FullAccess` (CDK uploads assets)
- `AmazonEC2ContainerRegistryFullAccess` (CDK pushes the Discord-bot image to ECR)
- `CloudWatchFullAccess`
- `AmazonSSMReadOnlyAccess` (CDK resolves the Ubuntu AMI via SSM parameter)

For a quick personal setup, `AdministratorAccess` covers all of the above.

> **Docker is required at deploy time** — the Discord-bot Lambda is a container
> image built from `discord-bot/Dockerfile` during `cdk deploy`.

### 3. CDK Bootstrap (one-time per account/region)

CDK needs an S3 bucket and supporting resources in your account before the first deploy:

```bash
cd infra
npm install
cdk bootstrap aws://ACCOUNT_ID/us-west-2
```

Replace `ACCOUNT_ID` with your 12-digit AWS account number (`aws sts get-caller-identity --query Account --output text`).

### 4. Configure secrets (SSM Parameter Store)

Three runtime values live in SSM and are **not** created by CDK (so secrets stay out of the repo and CloudFormation, and the `SecureString` type is supported). Create them once before the first deploy:

```bash
aws ssm put-parameter --name /crossroads-mc/discord/public-key    --type String       --value <ed25519-public-key-hex> --region us-west-2
aws ssm put-parameter --name /crossroads-mc/discord/admin-role-id --type String       --value <discord-role-id>       --region us-west-2
aws ssm put-parameter --name /crossroads-mc/rcon-password         --type SecureString --value <rcon-password>         --region us-west-2
```

- **discord/public-key** — the Discord application's Ed25519 public key (Developer Portal → General Information). The Lambda reads it on cold start to verify request signatures.
- **discord/admin-role-id** — the Discord role ID allowed to run `/wake`.
- **rcon-password** — baked into each world's `server.properties` on first provision (`SecureString`, decrypted by the instance).

To change a value later, re-run the command with `--overwrite`. Discord params take effect on the Lambda's next cold start; the RCON password applies to worlds provisioned after the change.

---

## Find Your Home IP

SSH and RCON access is restricted to a single CIDR. Get your current public IP:

```bash
curl -s https://checkip.amazonaws.com
```

Use it as `YOUR_IP/32` in the deploy commands below.

---

## Deploy

Set your domain and instance sizing in `infra/config.json` first:

```json
{ "domain_name": "crossroads-mc.net", "instance_type": "c6i.xlarge", "ebs_size": 20 }
```

Then deploy:

```bash
cd infra
npm install          # first time only
cdk deploy --context sshCidr=YOUR_IP/32
```

CDK will print a changeset summary and ask for confirmation before creating resources. The deploy takes 3–5 minutes.

On completion, the outputs show:

```
CrossroadsMcStack.ServerIP               = <Elastic IP>
CrossroadsMcStack.InstanceId             = i-0abc...
CrossroadsMcStack.DataVolumeId           = vol-0xyz...
CrossroadsMcStack.HostedZoneNameServers  = ns-1.awsdns-01.com, ns-2.awsdns-02.net, ...
```

**First deploy only:** CDK creates a Route 53 public hosted zone for `domain_name` and owns every record in it (the `server.` A record and one `_minecraft._tcp.<category>` SRV per slot). To make AWS authoritative for the domain, point the **domain's nameservers** at the four hosts in the `HostedZoneNameServers` output. This is full-zone delegation, not a per-record change:

- **Registrar-managed DNS:** in your registrar's control panel, replace the domain's nameservers with the four AWS hosts.
- **Cloudflare-managed domain:** because `crossroads-mc.net` is an apex domain, set the AWS nameservers as the domain's nameservers in Cloudflare's registrar settings — do **not** add per-record `NS` entries, which only work for delegating a sub-zone.

Propagation can take up to 24–48h but is often live in 15–30 min; check with `dig NS crossroads-mc.net`.

Once the User Data bootstrap finishes (~3 minutes after the instance passes health checks) the live slot is reachable at `<ServerIP>` on its category port. On the very first boot the live slot defaults to the first category (with an active world); afterwards `/wake` controls which slot is live. Boot progress is logged to `/var/log/user-data.log`.

---

## First Deploy — Walkthrough & Validation

The full Day-Zero sequence and how to prove each layer works. DNS delegation propagates in the background, so you can validate the server over its raw IP long before the friendly hostnames resolve.

### Step 1 — Provision the infrastructure

Run the deploy from [Deploy](#deploy) above. Capture three outputs:

- `ServerIP` — the Elastic IP (never changes across instance replacements).
- `HostedZoneNameServers` — the four AWS nameservers for the next step.
- `InstanceId` / `DiscordWebhookUrl` — used below.

### Step 2 — Delegate DNS

Point the domain's nameservers at the `HostedZoneNameServers` output (see the delegation note under [Deploy](#deploy)). Verify with `dig NS crossroads-mc.net` — no need to wait for this before continuing.

### Step 3 — Seed secrets & wire up Discord (while DNS propagates)

1. Create the three SSM parameters from [Configure secrets](#4-configure-secrets-ssm-parameter-store) (`discord/public-key`, `discord/admin-role-id`, `rcon-password`).
   - The Lambda reads the two Discord params from SSM **on its next cold start** — there is **no second `cdk deploy` needed**. (If the bot was already invoked once, the first call after seeding may cold-start to pick them up.)
   - `rcon-password` is applied on the next boot/swap, so its timing is not critical (see [RCON Passwords](#rcon-passwords)).
2. Paste the `DiscordWebhookUrl` output into the Discord Developer Portal → **Interactions Endpoint URL**. Discord immediately sends a signed PING; if the `public-key` param is correct, the Lambda verifies it and Discord saves the URL.
3. Register the slash commands with `make register-commands` (see [Discord Bot](#discord-bot-multi-world-control)).

### Step 4 — Whitelist yourself *before* testing connectivity

`config/whitelist.json` ships with `enforce-whitelist=true`, so **only listed players can join**. Add your own account (UUIDs must be in **dashed** form) before the connectivity test, or you'll be kicked at login:

```json
[
  { "uuid": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "name": "YourName" }
]
```

The list is union-merged on each provision, so a redeploy + slot bounce applies it. See [Whitelist Players](#whitelist-players).

### Step 5 — Validate

| Target | Test | Expectation |
|---|---|---|
| **Connectivity** | Direct-connect to `<ServerIP>:<port>` (e.g. `<ServerIP>:25565` for the `test` slot) | You join the world. This works the moment the instance is up — **no DNS required**. If it fails, the issue is the Security Group or the slot service, not DNS. |
| **SRV / friendly name** | Once DNS resolves, connect to `<category>.crossroads-mc.net` (e.g. `test.crossroads-mc.net`, no port) | SRV record routes you to the right port automatically. |
| **Bot — status** | `/status` in Discord | Embed shows power state + the active world per category. |
| **Bot — auth** | `/wake <category>` as a **non-admin** | `❌ Access Denied` (only the `admin-role-id` role may wake/swap). |
| **Bot — wake/swap** | `/wake <category>` as an admin | Stopped → instance starts (~90s); running → hot-swap to that slot (~20s). |
| **Persistence** | Stop then start the instance (EC2 console or CLI) | World data is unchanged — it lives on the retained EBS volume. |

> The Discord bot controls **power and slot selection only** (`/wake`, `/status`). It does not run in-game RCON commands, so there's no "bot sends `/say`" step — RCON is exposed for your own tooling (port `25575`, restricted to `sshCidr`).

Because you must include the port for a raw-IP connect, the connectivity test is also your fastest smoke test: if `<ServerIP>:<port>` works, the server and Security Group are healthy and any remaining issue is purely DNS.

---

## RCON Passwords

The RCON password comes from the `/crossroads-mc/rcon-password` SecureString (see [Configure secrets](#4-configure-secrets-ssm-parameter-store)). It is re-applied to every world's `server.properties` on **every provision** — which happens on each boot (`mc-boot.sh`) and each slot swap (`mc-swap.sh`). The SSM parameter is the single source of truth.

Because of this, the param does **not** have to exist before the first deploy: set it whenever, and it lands on the next boot/swap. If it's blank, Minecraft simply runs with RCON disabled.

To rotate the password, overwrite the SSM value and bounce the slot:

```bash
aws ssm put-parameter --name /crossroads-mc/rcon-password --type SecureString \
  --value <new-password> --overwrite --region us-west-2
# then reboot or swap the slot to re-bake it (e.g. /wake in Discord, or:)
aws ssm start-session --target INSTANCE_ID --region us-west-2
sudo systemctl restart 'minecraft@*'   # re-runs provisioning → re-applies the new password
```

`ls /mnt/minecraft-data/worlds/` lists the provisioned world UUIDs; `cat .../world.json` shows each world's name/category.

---

## Discord Bot (multi-world control)

The stack deploys a Lambda exposed via a Function URL. After deploy:

1. Set the `/crossroads-mc/discord/public-key` and `/crossroads-mc/discord/admin-role-id` SSM params (see [Configure secrets](#4-configure-secrets-ssm-parameter-store)). The Lambda picks them up on its next cold start — no redeploy needed. (The `/status` connect host is derived from `domain_name` in `infra/config.json`.)
2. Paste the `DiscordWebhookUrl` output into the Discord Developer Portal → **Interactions Endpoint URL**.
3. Register the slash commands locally:

   ```bash
   DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... \
     make register-commands
   ```

Commands: `/wake <category>` (admin — powers on and/or hot-swaps to a slot), `/status` (public). Pointing a slot at a different world is done with `make set` + `cdk deploy`, not from Discord.

---

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

### Restart / swap the live slot

```bash
sudo systemctl restart 'minecraft@*'                       # restart the current slot
sudo /mnt/minecraft-data/scripts/mc-swap.sh creative       # switch to another slot
```

(Normally you switch slots with `/wake <category>` in Discord, which runs this for you.)

### Stop the instance (saves cost when not playing)

```bash
aws ec2 stop-instances --instance-ids INSTANCE_ID --region us-west-2
```

The EBS volume is retained. Start it again with:

```bash
aws ec2 start-instances --instance-ids INSTANCE_ID --region us-west-2
```

Note: Ubuntu runs user data only once per instance, so a stop/start does **not** re-run the full bootstrap. The enabled `mc-boot.service` runs on every boot: it re-syncs the manifest/scripts/config from S3, seeds the `active-category` pointer if needed, and starts the live slot. The EBS data is untouched.

### Idle auto-shutdown

`heartbeat.sh` runs every 20 minutes. If a `minecraft@*` slot is running and fewer than 100 network packets are received in a 5-minute window, it issues `shutdown -h +1`. The CloudWatch `minecraft-idle` alarm provides a separate signal for monitoring but does not trigger shutdown on its own.

### Disk-usage alarm

`disk-check.sh` runs every 5 minutes and publishes the data volume's used percentage as the `CrossroadsMC/DiskUsedPercent` custom metric. The `minecraft-disk-usage` CloudWatch alarm fires when the EBS volume passes ~85% full (all worlds live on EBS, so this is the signal to grow the volume).

### Tear down

```bash
cdk destroy
```

The EBS data volume has `RemovalPolicy.RETAIN` — it is **not** deleted on `cdk destroy`. Delete it manually (EC2 console) if you no longer need the world data.

---

## Update After Code Changes

```bash
cd infra
cdk deploy --context sshCidr=YOUR_IP/32
```

Changes to `scripts/`, `config/`, or the manifests are bundled as S3 assets and uploaded on deploy. A running instance picks them up on its next boot or slot swap (it re-syncs from S3 via `sync_assets`), so a plain stop/start — or a `/wake` — applies them; no instance replacement required.
