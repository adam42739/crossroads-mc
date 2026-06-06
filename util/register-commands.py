#!/usr/bin/env python3
"""Register the Crossroads slash commands with Discord.

Run locally (not on the server). Requires three arguments:
  --app-id     Application ID (Discord Dev Portal → General Information)
  --bot-token  Bot token (Dev Portal → Bot)
  --guild-id   (optional) register to one guild for instant updates;
               omit to register global commands (up to 1h propagation).

Usage (normally via make):
  make register-commands APP_ID=... BOT_TOKEN=... GUILD_ID=...
"""
import argparse
import json
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATEGORIES_DIR = os.path.join(REPO_ROOT, 'infra', 'manifests', 'categories')


def _category_choices():
    """Build /wake choices by scanning the category manifests."""
    choices = []
    for f in sorted(os.listdir(CATEGORIES_DIR)):
        if not f.endswith('.json'):
            continue
        with open(os.path.join(CATEGORIES_DIR, f)) as fh:
            name = json.load(fh)['name']
        choices.append({'name': name.capitalize(), 'value': name})
    return choices


CATEGORY_CHOICES = _category_choices()

# Discord application command option types
STRING = 3

COMMANDS = [
    {
        'name': 'wake',
        'description': 'Power on the server and load a world category (MC_BOT_AUTH role required)',
        'options': [{
            'type': STRING, 'name': 'category', 'description': 'Which slot to wake',
            'required': True, 'choices': CATEGORY_CHOICES,
        }],
    },
    {
        'name': 'status',
        'description': 'Show server power state and the active world by category (MC_BOT_AUTH role required)',
    },
]


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--app-id', required=True,
                        help='Discord Application ID')
    parser.add_argument('--bot-token', required=True,
                        help='Discord bot token')
    parser.add_argument('--guild-id', default=None,
                        help='Register to one guild for instant updates '
                             '(omit for global commands)')
    args = parser.parse_args()

    app_id = args.app_id
    token = args.bot_token
    guild_id = args.guild_id

    if guild_id:
        url = f'https://discord.com/api/v10/applications/{app_id}/guilds/{guild_id}/commands'
        scope = f'guild {guild_id}'
    else:
        url = f'https://discord.com/api/v10/applications/{app_id}/commands'
        scope = 'global'

    # PUT bulk-overwrites the full command set.
    # Discord (behind Cloudflare) rejects the default Python-urllib UA with a
    # 1010 error, so send a proper DiscordBot user agent per their API docs.
    req = urllib.request.Request(
        url, data=json.dumps(COMMANDS).encode(), method='PUT',
        headers={
            'Authorization': f'Bot {token}',
            'Content-Type': 'application/json',
            'User-Agent': 'DiscordBot (https://github.com/adam42939/crossroads-mc, 1.0)',
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            registered = json.load(resp)
        print(f'Registered {len(registered)} commands ({scope}): '
              + ', '.join(c['name'] for c in registered))
    except urllib.error.HTTPError as e:
        sys.exit(f'Discord API error {e.code}: {e.read().decode()}')


if __name__ == '__main__':
    main()
