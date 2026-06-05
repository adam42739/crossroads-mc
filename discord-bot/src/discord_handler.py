import boto3
import os
import json
import urllib.request
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from mypy_boto3_ec2 import EC2Client
from mypy_boto3_ssm import SSMClient
from mypy_boto3_lambda import LambdaClient

SSM_PREFIX = os.environ.get("SSM_PREFIX", "/crossroads-mc")

ec2: EC2Client = boto3.client("ec2")
ssm: SSMClient = boto3.client("ssm")
lam: LambdaClient = boto3.client("lambda")


def _ssm_get(name):
    try:
        return ssm.get_parameter(Name=f"{SSM_PREFIX}/{name}", WithDecryption=True)[
            "Parameter"
        ]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return ""


def _ssm_put(name, value):
    ssm.put_parameter(
        Name=f"{SSM_PREFIX}/{name}", Value=value, Type="String", Overwrite=True
    )


INSTANCE_ID = os.environ["INSTANCE_ID"]
SERVER_HOSTNAME = os.environ.get("SERVER_HOSTNAME", "")
# Lambda's own name (set by the runtime) — used to async-invoke ourselves so we can
# ACK Discord within its 3s deadline and finish the slow work afterwards.
FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
# Discord secrets live in SSM (/crossroads-mc/discord/*); read once on cold start.
ADMIN_ROLE_ID = _ssm_get("discord/admin-role-id")
PUBLIC_KEY = _ssm_get("discord/public-key")
# Deploy-time manifest, injected by CDK as { "categories": [...], "worlds": [...] }.
MANIFEST = json.loads(os.environ.get("MANIFEST_JSON", "{}"))
CATEGORIES = MANIFEST.get("categories", [])
WORLDS = MANIFEST.get("worlds", [])
VALID_CATEGORIES = {c["name"] for c in CATEGORIES}

SWAP_SCRIPT = "/mnt/minecraft-data/scripts/mc-swap.sh"
DISCORD_API = "https://discord.com/api/v10"

# Discord interaction-response types
PONG = 1
CHANNEL_MESSAGE = 4
DEFERRED_CHANNEL_MESSAGE = 5  # "thinking…" — edit the message later via webhook

GREEN = 0x57F287
RED = 0xED4245
YELLOW = 0xFEE75C


def handler(event, context):
    # Phase 2: our own async invocation — do the slow work and edit the message.
    if event.get("crossroads_deferred"):
        return _process_deferred(event["interaction"])

    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    signature = headers.get("x-signature-ed25519")
    timestamp = headers.get("x-signature-timestamp")
    body = event.get("body", "")

    try:
        verify_key = VerifyKey(bytes.fromhex(PUBLIC_KEY))
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
    except (BadSignatureError, TypeError, ValueError):
        return {"statusCode": 401, "body": "Invalid request signature"}

    data = json.loads(body)

    # Discord PING — identity verification handshake
    if data["type"] == 1:
        return _json(200, {"type": PONG})

    # Phase 1: hand the command off to an async copy of ourselves and immediately
    # ACK with a deferred response so we never miss Discord's 3s deadline (cold
    # starts + EC2/SSM calls can otherwise blow past it).
    lam.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({"crossroads_deferred": True, "interaction": data}).encode(),
    )
    return _json(200, {"type": DEFERRED_CHANNEL_MESSAGE})


# ── Phase 2 worker: runs in the async invocation, edits the deferred message ──────
def _process_deferred(data):
    command_name = data["data"]["name"]
    options = _options(data)
    user_roles = data.get("member", {}).get("roles", [])

    if command_name == "wake":
        message = _handle_wake(options.get("category"), user_roles)
    elif command_name == "status":
        message = _handle_status()
    else:
        message = {"content": "Unknown command."}

    _edit_original(data["application_id"], data["token"], message)
    return {"ok": True}


# ── /wake <category> — power on (if stopped) and/or hot-swap to category ─────────
def _handle_wake(category, user_roles):
    if ADMIN_ROLE_ID not in user_roles:
        return _text("❌ **Access Denied.** You need the `Minecraft Admin` role.")
    if category not in VALID_CATEGORIES:
        return _text(f"❌ Unknown category `{category}`.")

    state, _ = _get_instance_info()
    world = _active_world_name(category)

    if state == "stopped":
        # Mark which slot to boot into, then power the hardware on.
        _ssm_put("active-category", category)
        ec2.start_instances(InstanceIds=[INSTANCE_ID])
        return _text(
            f"🚀 **Engine Starting** for **{category}** ({world}). "
            "Hardware is spooling up; join in ~90s."
        )
    if state == "running":
        _run_swap(category)
        return _text(f"🔄 **Swapping** to **{category}** ({world}). Ready in ~20s.")
    return _text(f"⏳ Server is currently **{state}** — try again shortly.")


# ── /status — public; instance state + active worlds ────────────────────────────
def _handle_status():
    state, public_ip = _get_instance_info()

    if state == "running":
        live = _ssm_get("active-category")
        lines = []
        for cat in CATEGORIES:
            marker = "🎮" if cat["name"] == live else "💤"
            lines.append(
                f"{marker} **{cat['name']}** → {_active_world_name(cat['name'])}"
            )
        ip_line = f"`{public_ip}`" if public_ip else "IP unavailable"
        host_note = f"\nConnect via `{SERVER_HOSTNAME}`" if SERVER_HOSTNAME else ""
        desc = f"**IP:** {ip_line}{host_note}\n\n" + "\n".join(lines)
        return _embed(GREEN, "🟢 Server Online", desc)
    if state == "stopped":
        return _embed(
            RED,
            "🔴 Server Offline",
            "Server is currently **Resting** (Offline). Use `/wake` to start it.",
        )
    return _embed(
        YELLOW,
        "🟡 Server Transitioning",
        f"Server is currently **{state.capitalize()}** — check back in a moment.",
    )


# ── Helpers ─────────────────────────────────────────────────────────────────────
def _options(data):
    return {o["name"]: o["value"] for o in data["data"].get("options", [])}


def _active_world_name(category):
    """Name of the world a category currently points at (from the manifest)."""
    uuid = next(
        (c.get("activeWorld", "") for c in CATEGORIES if c["name"] == category), ""
    )
    for w in WORLDS:
        if w["uuid"] == uuid:
            return w["name"]
    return uuid or "—"


def _run_swap(category):
    ssm.send_command(
        InstanceIds=[INSTANCE_ID],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [f"{SWAP_SCRIPT} {category}"]},
    )


def _get_instance_info():
    res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
    inst = res["Reservations"][0]["Instances"][0]
    return inst["State"]["Name"], inst.get("PublicIpAddress", "")


def _edit_original(application_id, token, message):
    """Replace the deferred ('thinking…') placeholder with the real response."""
    url = f"{DISCORD_API}/webhooks/{application_id}/{token}/messages/@original"
    req = urllib.request.Request(
        url,
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    urllib.request.urlopen(req)


def _text(content):
    return {"content": content}


def _embed(color, title, description):
    return {"embeds": [{"color": color, "title": title, "description": description}]}


def _json(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
