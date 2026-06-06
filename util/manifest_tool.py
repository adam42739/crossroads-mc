#!/usr/bin/env python3
"""Create and edit Crossroads world/category manifests.

Backs the `make category|world|set` targets. Manifests live in
infra/manifests/{categories,worlds}/ and are scanned at `cdk deploy` time — no
code changes are needed to add a world or category.

Usage (normally via make):
  manifest_tool.py category --name <name> --port <port>
  manifest_tool.py world    --category <name> --name <world name>
  manifest_tool.py set      --category <name> --uuid <uuid>
"""
import argparse
import json
import os
import sys
import uuid as uuidlib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFESTS = os.path.join(REPO_ROOT, 'infra', 'manifests')
CATEGORIES_DIR = os.path.join(MANIFESTS, 'categories')
WORLDS_DIR = os.path.join(MANIFESTS, 'worlds')


def _fail(msg):
    sys.exit(f'error: {msg}')


def _load(path):
    with open(path) as f:
        return json.load(f)


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(obj, f, indent=2)
        f.write('\n')


def _categories():
    if not os.path.isdir(CATEGORIES_DIR):
        return []
    return [_load(os.path.join(CATEGORIES_DIR, f))
            for f in os.listdir(CATEGORIES_DIR) if f.endswith('.json')]


def cmd_category(args):
    path = os.path.join(CATEGORIES_DIR, f'{args.name}.json')
    if os.path.exists(path):
        _fail(f'category "{args.name}" already exists ({path}).')
    for c in _categories():
        if c.get('port') == args.port:
            _fail(f'port {args.port} is already used by category "{c.get("name")}".')
    _write(path, {'name': args.name, 'port': args.port, 'activeWorld': ''})
    print(f'Created category "{args.name}" on port {args.port}.')
    print(f'  → {path}')
    print('Run `cd infra && cdk deploy` to apply (CDK creates the SRV record + opens the port).')


def cmd_world(args):
    cat_path = os.path.join(CATEGORIES_DIR, f'{args.category}.json')
    if not os.path.exists(cat_path):
        _fail(f'category "{args.category}" does not exist — create it with `make category` first.')
    new_uuid = str(uuidlib.uuid4())
    path = os.path.join(WORLDS_DIR, f'{new_uuid}.json')
    _write(path, {
        'uuid': new_uuid,
        'category': args.category,
        'name': args.name,
        'engine': '',
        'version': '',
        'settings': {'gamemode': '', 'difficulty': '', 'hardcore': False, 'levelType': '',
                     'plugins': []},
    })
    print(f'Created world "{args.name}" in category "{args.category}".')
    print(f'  uuid: {new_uuid}')
    print(f'  → {path}')
    print('Fill in engine/version/settings, then `make set` it active and `cdk deploy`.')


def cmd_set(args):
    cat_path = os.path.join(CATEGORIES_DIR, f'{args.category}.json')
    if not os.path.exists(cat_path):
        _fail(f'category "{args.category}" does not exist.')
    world_path = os.path.join(WORLDS_DIR, f'{args.uuid}.json')
    if not os.path.exists(world_path):
        _fail(f'world "{args.uuid}" does not exist.')
    world = _load(world_path)
    if world.get('category') != args.category:
        _fail(f'world "{args.uuid}" belongs to category "{world.get("category")}", '
              f'not "{args.category}".')
    settings = world.get('settings', {})
    if not (world.get('engine') and world.get('version') and settings.get('gamemode')
            and settings.get('difficulty') and settings.get('levelType')):
        _fail(f'world "{args.uuid}" is not fully configured '
              '(engine/version/settings.gamemode/difficulty/levelType must be set).')
    category = _load(cat_path)
    category['activeWorld'] = args.uuid
    _write(cat_path, category)
    print(f'Category "{args.category}" now points at "{world.get("name")}" ({args.uuid}).')
    print('Run `cdk deploy` to apply (the server picks it up on next /wake or boot).')


def main():
    parser = argparse.ArgumentParser(description='Manage Crossroads manifests.')
    sub = parser.add_subparsers(dest='command', required=True)

    p_cat = sub.add_parser('category', help='create a new category')
    p_cat.add_argument('--name', required=True)
    p_cat.add_argument('--port', required=True, type=int)
    p_cat.set_defaults(func=cmd_category)

    p_world = sub.add_parser('world', help='create a new (blank) world')
    p_world.add_argument('--category', required=True)
    p_world.add_argument('--name', required=True)
    p_world.set_defaults(func=cmd_world)

    p_set = sub.add_parser('set', help="set a category's active world")
    p_set.add_argument('--category', required=True)
    p_set.add_argument('--uuid', required=True)
    p_set.set_defaults(func=cmd_set)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
