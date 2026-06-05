#!/usr/bin/env python3
"""Register the Crossroads slash commands with Discord.

Run locally (not on the server). Requires three env vars:
  DISCORD_APP_ID     — Application ID (Discord Dev Portal → General Information)
  DISCORD_BOT_TOKEN  — Bot token (Dev Portal → Bot)
  DISCORD_GUILD_ID   — (optional) register to one guild for instant updates;
                       omit to register global commands (up to 1h propagation).

Usage (normally via make):
  DISCORD_APP_ID=... DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... make register-commands
"""
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
        'description': 'Power on the server and load a world category (admin only)',
        'options': [{
            'type': STRING, 'name': 'category', 'description': 'Which slot to wake',
            'required': True, 'choices': CATEGORY_CHOICES,
        }],
    },
    {
        'name': 'status',
        'description': 'Show server power state and the active world per category',
    },
]


def main():
    app_id = os.environ.get('DISCORD_APP_ID')
    token = os.environ.get('DISCORD_BOT_TOKEN')
    guild_id = os.environ.get('DISCORD_GUILD_ID')
    if not app_id or not token:
        sys.exit('Set DISCORD_APP_ID and DISCORD_BOT_TOKEN.')

    if guild_id:
        url = f'https://discord.com/api/v10/applications/{app_id}/guilds/{guild_id}/commands'
        scope = f'guild {guild_id}'
    else:
        url = f'https://discord.com/api/v10/applications/{app_id}/commands'
        scope = 'global'

    # PUT bulk-overwrites the full command set.
    req = urllib.request.Request(
        url, data=json.dumps(COMMANDS).encode(), method='PUT',
        headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json'},
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
