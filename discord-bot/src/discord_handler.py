import boto3
import logging
import os
import json
import time
import urllib.error
import urllib.request
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from mypy_boto3_ec2 import EC2Client
from mypy_boto3_ssm import SSMClient
from mypy_boto3_lambda import LambdaClient

# The Lambda runtime installs its own handler on the root logger before our code
# runs, which makes logging.basicConfig() a no-op — so its level is ignored and our
# logger inherits the runtime default (WARNING), dropping every log.info(). Set the
# level directly on our logger instead. Honour LOG_LEVEL (default INFO).
log = logging.getLogger("crossroads-bot")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())

SSM_PREFIX = os.environ.get("SSM_PREFIX", "/crossroads-mc")

ec2: EC2Client = boto3.client("ec2")
ssm: SSMClient = boto3.client("ssm")
lam: LambdaClient = boto3.client("lambda")


def _ssm_get(name):
    full_name = f"{SSM_PREFIX}/{name}"
    try:
        value = ssm.get_parameter(Name=full_name, WithDecryption=True)["Parameter"][
            "Value"
        ]
        log.info("SSM get %s → ok (%d chars)", full_name, len(value))
        return value
    except ssm.exceptions.ParameterNotFound:
        log.warning("SSM get %s → ParameterNotFound, returning empty", full_name)
        return ""
    except Exception:
        log.exception("SSM get %s → unexpected error", full_name)
        raise


def _ssm_put(name, value):
    full_name = f"{SSM_PREFIX}/{name}"
    log.info("SSM put %s = %r", full_name, value)
    try:
        ssm.put_parameter(Name=full_name, Value=value, Type="String", Overwrite=True)
    except Exception:
        log.exception("SSM put %s → failed", full_name)
        raise


log.info("Cold start: initialising configuration")

INSTANCE_ID = os.environ["INSTANCE_ID"]
# Apex domain (e.g. crossroads-mc.net). Players connect per-category via the SRV
# record at <category>.<domain>, so we build the connect host from this.
DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "")
# Lambda's own name (set by the runtime) — used to async-invoke ourselves so we can
# ACK Discord within its 3s deadline and finish the slow work afterwards.
FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")
# Discord secrets live in SSM (/crossroads-mc/discord/*).
# PUBLIC_KEY is needed in Phase 1 to verify the signature before we can ACK, so it
# must load during cold-start init. ADMIN_ROLE_ID is only used in Phase 2 (the
# async worker), so we DON'T fetch it at import — keeping the Phase-1 ACK path off
# the critical path of Discord's 3s deadline. It's fetched + cached on first use.
PUBLIC_KEY = _ssm_get("discord/public-key")

_admin_role_id_cache = None


def _admin_role_id():
    """Lazily fetch + cache the admin role id (Phase-2 only)."""
    global _admin_role_id_cache
    if _admin_role_id_cache is None:
        _admin_role_id_cache = _ssm_get("discord/admin-role-id")
    return _admin_role_id_cache
# Deploy-time manifest, injected by CDK as { "categories": [...], "worlds": [...] }.
MANIFEST = json.loads(os.environ.get("MANIFEST_JSON", "{}"))
CATEGORIES = MANIFEST.get("categories", [])
WORLDS = MANIFEST.get("worlds", [])
VALID_CATEGORIES = {c["name"] for c in CATEGORIES}

log.info(
    "Config loaded: instance=%s domain=%s function=%s categories=%s worlds=%d "
    "public_key_set=%s (admin_role_id lazy-loaded in Phase 2)",
    INSTANCE_ID,
    DOMAIN_NAME or "<unset>",
    FUNCTION_NAME or "<unset>",
    sorted(VALID_CATEGORIES),
    len(WORLDS),
    bool(PUBLIC_KEY),
)
if not PUBLIC_KEY:
    log.error("PUBLIC_KEY is empty — all signature verification will fail")

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
    aws_request_id = getattr(context, "aws_request_id", "?")

    # Phase 2: our own async invocation — do the slow work and edit the message.
    if event.get("crossroads_deferred"):
        log.info("[%s] Phase 2: deferred invocation received", aws_request_id)
        return _process_deferred(event["interaction"])

    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    signature = headers.get("x-signature-ed25519")
    timestamp = headers.get("x-signature-timestamp")
    body = event.get("body", "")
    log.info(
        "[%s] Phase 1: incoming request (body=%d bytes, signature=%s, timestamp=%s)",
        aws_request_id,
        len(body or ""),
        "present" if signature else "missing",
        "present" if timestamp else "missing",
    )

    try:
        verify_key = VerifyKey(bytes.fromhex(PUBLIC_KEY))
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
    except (BadSignatureError, TypeError, ValueError) as e:
        log.warning(
            "[%s] Signature verification failed: %s: %s",
            aws_request_id,
            type(e).__name__,
            e,
        )
        return {"statusCode": 401, "body": "Invalid request signature"}

    log.info("[%s] Signature verified", aws_request_id)
    data = json.loads(body)
    interaction_type = data.get("type")

    # Discord PING — identity verification handshake
    if interaction_type == 1:
        log.info("[%s] PING handshake → PONG", aws_request_id)
        return _json(200, {"type": PONG})

    command_name = data.get("data", {}).get("name", "?")
    log.info(
        "[%s] Interaction type=%s command=%s → deferring to async worker",
        aws_request_id,
        interaction_type,
        command_name,
    )

    # Phase 1: hand the command off to an async copy of ourselves and immediately
    # ACK with a deferred response so we never miss Discord's 3s deadline (cold
    # starts + EC2/SSM calls can otherwise blow past it).
    try:
        lam.invoke(
            FunctionName=FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(
                {"crossroads_deferred": True, "interaction": data}
            ).encode(),
        )
        log.info("[%s] Async self-invoke dispatched for %s", aws_request_id, command_name)
    except Exception:
        log.exception("[%s] Async self-invoke failed", aws_request_id)
        raise
    return _json(200, {"type": DEFERRED_CHANNEL_MESSAGE})


# ── Phase 2 worker: runs in the async invocation, edits the deferred message ──────
def _process_deferred(data):
    command_name = data["data"]["name"]
    options = _options(data)
    user_roles = data.get("member", {}).get("roles", [])
    user = data.get("member", {}).get("user", {})
    user_label = f"{user.get('username', '?')}#{user.get('id', '?')}"
    log.info(
        "Processing command=%s options=%s user=%s roles=%d",
        command_name,
        options,
        user_label,
        len(user_roles),
    )

    try:
        if command_name == "wake":
            message = _handle_wake(options.get("category"), user_roles)
        elif command_name == "status":
            message = _handle_status(user_roles)
        else:
            log.warning("Unknown command: %s", command_name)
            message = {"content": "Unknown command."}
    except Exception:
        log.exception("Command %s raised — sending error message to user", command_name)
        message = _text("⚠️ Something went wrong handling that command. Try again.")

    try:
        _edit_original(data["application_id"], data["token"], message)
        log.info("Command=%s complete; deferred message edited", command_name)
    except Exception:
        log.exception("Failed to edit deferred message for command=%s", command_name)
        raise
    return {"ok": True}


# ── /wake <category> — power on (if stopped) and/or hot-swap to category ─────────
def _handle_wake(category, user_roles):
    if _admin_role_id() not in user_roles:
        log.warning("/wake denied: user lacks admin role (category=%s)", category)
        return _text("❌ **Access Denied.** You need the `MC_BOT_AUTH` role.")
    if category not in VALID_CATEGORIES:
        log.warning("/wake rejected: unknown category=%s", category)
        return _text(f"❌ Unknown category `{category}`.")

    state, _ = _get_instance_info()
    world = _active_world_name(category)
    log.info("/wake category=%s world=%s instance_state=%s", category, world, state)

    if state == "stopped":
        # Mark which slot to boot into, then power the hardware on.
        _ssm_put("active-category", category)
        ec2.start_instances(InstanceIds=[INSTANCE_ID])
        log.info("/wake: started instance %s for category=%s", INSTANCE_ID, category)
        return _text(
            f"🚀 **Engine Starting** for **{category}** ({world}). "
            "Hardware is spooling up; join in ~90s."
        )
    if state == "running":
        _run_swap(category)
        log.info("/wake: hot-swap requested for category=%s", category)
        return _text(f"🔄 **Swapping** to **{category}** ({world}). Ready in ~20s.")
    log.info("/wake: instance in transitional state=%s — asking user to retry", state)
    return _text(f"⏳ Server is currently **{state}** — try again shortly.")


# ── /status — admin; instance state + active worlds ─────────────────────────────
def _handle_status(user_roles):
    if _admin_role_id() not in user_roles:
        log.warning("/status denied: user lacks admin role")
        return _text("❌ **Access Denied.** You need the `MC_BOT_AUTH` role.")

    state, _ = _get_instance_info()
    log.info("/status: instance_state=%s", state)

    if state == "running":
        live = _ssm_get("active-category")
        log.info("/status: live category=%s", live or "<none>")
        lines = []
        for cat in CATEGORIES:
            marker = "🎮" if cat["name"] == live else "💤"
            lines.append(
                f"{marker} **{cat['name']}** → {_active_world_name(cat['name'])}"
            )
        connect_note = ""
        if live and DOMAIN_NAME:
            connect_note = f"Connect via `{live}.{DOMAIN_NAME}`\n\n"
        desc = connect_note + "\n".join(lines)
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
    log.info("Sending SSM RunShellScript: %s %s", SWAP_SCRIPT, category)
    try:
        res = ssm.send_command(
            InstanceIds=[INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [f"{SWAP_SCRIPT} {category}"]},
        )
        command_id = res.get("Command", {}).get("CommandId", "?")
        log.info("SSM command dispatched: CommandId=%s", command_id)
    except Exception:
        log.exception("SSM send_command failed for category=%s", category)
        raise


def _get_instance_info():
    try:
        res = ec2.describe_instances(InstanceIds=[INSTANCE_ID])
        inst = res["Reservations"][0]["Instances"][0]
        state, ip = inst["State"]["Name"], inst.get("PublicIpAddress", "")
        log.info("describe_instances %s → state=%s ip=%s", INSTANCE_ID, state, ip or "<none>")
        return state, ip
    except Exception:
        log.exception("describe_instances failed for %s", INSTANCE_ID)
        raise


def _edit_original(application_id, token, message):
    """Replace the deferred ('thinking…') placeholder with the real response.

    The type-5 deferred ACK is returned by the Phase-1 invocation while this
    Phase-2 edit runs in a *separate* async invocation. For fast commands Phase 2
    can reach Discord before that ACK is registered, so the followup webhook
    briefly 404s with "Unknown Webhook" (code 10015). Retry with backoff to ride
    out that propagation window before giving up.
    """
    url = f"{DISCORD_API}/webhooks/{application_id}/{token}/messages/@original"
    data = json.dumps(message).encode()
    # Sleep between attempts (so len == retries; the final attempt doesn't sleep).
    backoff = [0.5, 1.0, 2.0, 3.0]

    for attempt in range(len(backoff) + 1):
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                # Discord sits behind Cloudflare, which 403s the default
                # `Python-urllib` UA. The API requires this DiscordBot form.
                "User-Agent": "DiscordBot (https://github.com/adam42739/crossroads-mc, 1.0)",
            },
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                log.info(
                    "Discord webhook PATCH @original → HTTP %s (attempt %d)",
                    resp.status,
                    attempt + 1,
                )
            return
        except urllib.error.HTTPError as e:
            # Read the body for Discord's error detail (e.g. expired token, bad embed).
            body = e.read().decode("utf-8", "replace")
            # 10015 = Unknown Webhook: the deferred ACK hasn't propagated yet.
            unknown_webhook = e.code == 404 and '"code": 10015' in body
            if unknown_webhook and attempt < len(backoff):
                delay = backoff[attempt]
                log.warning(
                    "Discord webhook PATCH @original → 404 Unknown Webhook "
                    "(attempt %d/%d); deferred ACK not propagated, retrying in %ss",
                    attempt + 1,
                    len(backoff) + 1,
                    delay,
                )
                time.sleep(delay)
                continue
            log.error(
                "Discord webhook PATCH failed: HTTP %s %s — %s",
                e.code,
                e.reason,
                body,
            )
            raise
        except Exception:
            log.exception("Discord webhook PATCH errored")
            raise


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
