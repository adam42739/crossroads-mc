// ── Crossroads Multi-World Manifest loader ──────────────────────────────────────
//
// The "Cartridge & Slot" model:
//   • Slots     — categories. Each category is one JSON file under
//                 infra/manifests/categories/<name>.json, declaring its public
//                 port and which world is currently active (the "activeWorld"
//                 pointer, set via `make set`). The CDK creates a Route 53 SRV
//                 record (_minecraft._tcp.<name>.<domain>) per slot at synth time.
//   • Cartridges— worlds. Each is one JSON file under
//                 infra/manifests/worlds/<uuid>.json — a self-contained UUID folder
//                 on EBS (server.jar, world data, baked server.properties).
//
// Adding a world or category is purely a matter of dropping a new JSON file in the
// right directory (see the `make` commands) and running `cdk deploy` — no code
// changes. The stack scans these directories at synth time, validates them, and
// injects a combined manifest into the instance and the Lambda.

import * as fs from 'fs';
import * as path from 'path';

export type Engine = 'paper' | 'vanilla' | 'forge' | 'fabric';
export type Version = '1.20.1' | '1.21';
export type Difficulty = 'peaceful' | 'easy' | 'normal' | 'hard';
export type LevelType = 'default' | 'flat' | 'large_biomes';
export type Gamemode = 'survival' | 'creative' | 'adventure' | 'spectator';

export interface MinecraftWorld {
  /** Unique folder name on EBS (worlds/<uuid>/). */
  uuid: string;
  /** Which category (slot/port) this world belongs to. */
  category: string;
  /** Human-facing name. Need not be unique. */
  name: string;
  /** Server engine. vanilla + paper are fully supported; forge/fabric are stubbed. */
  engine: Engine | '';
  /** Game version — determines the Java runtime and the jar to download. */
  version: Version | '';
  settings: {
    gamemode: Gamemode | '';
    difficulty: Difficulty | '';
    hardcore: boolean;
    levelType: LevelType | '';
    seed?: string;
  };
}

export interface Category {
  /** Slot name; drives the Route 53 SRV record (_minecraft._tcp.<name>.<domain>). */
  name: string;
  /** Public TCP port this slot listens on. */
  port: number;
  /** UUID of the world this slot currently runs. Empty until `make set`. */
  activeWorld: string;
}

export interface Manifest {
  categories: Category[];
  worlds: MinecraftWorld[];
}

/** Global infrastructure settings, read from infra/config.json. */
export interface GlobalConfig {
  /** Apex domain that owns the Route 53 hosted zone (e.g. crossroads-mc.net). */
  domain_name: string;
  /** EC2 instance type for the game server (e.g. c6i.xlarge). */
  instance_type: string;
  /** Size of the persistent EBS data volume, in GiB. */
  ebs_size: number;
}

const CATEGORIES_DIR = path.join(__dirname, '..', 'manifests', 'categories');
const WORLDS_DIR = path.join(__dirname, '..', 'manifests', 'worlds');
const CONFIG_FILE = path.join(__dirname, '..', 'config.json');

function readJsonDir<T>(dir: string): T[] {
  if (!fs.existsSync(dir)) return [];
  return fs
    .readdirSync(dir)
    .filter((f) => f.endsWith('.json'))
    .map((f) => JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8')) as T);
}

/** Scan the manifest directories. */
export function loadManifest(): Manifest {
  const categories = readJsonDir<Category>(CATEGORIES_DIR);
  const worlds = readJsonDir<MinecraftWorld>(WORLDS_DIR);
  return { categories, worlds };
}

/** Load + validate the global config (infra/config.json). Throws on any problem
 *  so a malformed config fails the CDK synth rather than deploying bad infra. */
export function loadConfig(): GlobalConfig {
  if (!fs.existsSync(CONFIG_FILE)) {
    throw new Error(`Missing infra/config.json (expected at ${CONFIG_FILE}).`);
  }
  const cfg = JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf-8')) as GlobalConfig;
  if (!cfg.domain_name || typeof cfg.domain_name !== 'string') {
    throw new Error('config.json: "domain_name" must be a non-empty string.');
  }
  if (!cfg.instance_type || typeof cfg.instance_type !== 'string') {
    throw new Error('config.json: "instance_type" must be a non-empty string.');
  }
  if (!Number.isInteger(cfg.ebs_size) || cfg.ebs_size <= 0) {
    throw new Error('config.json: "ebs_size" must be a positive integer (GiB).');
  }
  return cfg;
}

/** A world is runnable only once its engine + version + core settings are filled. */
export function isConfigured(w: MinecraftWorld): boolean {
  return Boolean(
    w.engine && w.version &&
    w.settings?.gamemode && w.settings?.difficulty && w.settings?.levelType,
  );
}

// Allowed enum values (mirror the TS unions above). Validated at synth so a typo
// in a hand-edited manifest fails the build with a clear message instead of a
// confusing failure deep in provisioning. Blank stubs from `make world` are
// exempt — each check only runs when the field is non-empty.
const ENGINES: Engine[] = ['paper', 'vanilla', 'forge', 'fabric'];
const GAMEMODES: Gamemode[] = ['survival', 'creative', 'adventure', 'spectator'];
const DIFFICULTIES: Difficulty[] = ['peaceful', 'easy', 'normal', 'hard'];
const LEVEL_TYPES: LevelType[] = ['default', 'flat', 'large_biomes'];
// New Minecraft versions ship constantly and the engine jars are resolved
// dynamically at provision time, so validate the shape, not a fixed list.
const VERSION_RE = /^\d+\.\d+(\.\d+)?$/;

/**
 * Validate the manifest. Throws (failing the CDK synth) on any violation so bad
 * definitions never reach a deploy. Blank worlds freshly created by `make world`
 * are tolerated — they just cannot be set active until configured.
 */
export function validateManifest(m: Manifest): void {
  const catByName = new Map<string, Category>();
  const seenPort = new Map<number, string>();

  for (const c of m.categories) {
    if (!c.name) throw new Error('Category manifest missing "name".');
    if (catByName.has(c.name)) throw new Error(`Duplicate category "${c.name}".`);
    if (!Number.isInteger(c.port) || c.port < 1 || c.port > 65535) {
      throw new Error(`Category "${c.name}": invalid port "${c.port}" (must be 1-65535).`);
    }
    const portOwner = seenPort.get(c.port);
    if (portOwner) {
      throw new Error(`Port ${c.port} is used by both "${portOwner}" and "${c.name}".`);
    }
    seenPort.set(c.port, c.name);
    catByName.set(c.name, c);
  }

  const worldByUuid = new Map<string, MinecraftWorld>();
  for (const w of m.worlds) {
    if (!w.uuid) throw new Error(`World "${w.name}" missing "uuid".`);
    if (worldByUuid.has(w.uuid)) throw new Error(`Duplicate world uuid "${w.uuid}".`);
    worldByUuid.set(w.uuid, w);

    if (!catByName.has(w.category)) {
      throw new Error(`World "${w.name}" (${w.uuid}): unknown category "${w.category}".`);
    }
    // Field-level enum checks (only when filled in — blank stubs stay valid).
    if (w.engine && !ENGINES.includes(w.engine as Engine)) {
      throw new Error(`World "${w.name}" (${w.uuid}): unknown engine "${w.engine}" (expected ${ENGINES.join('|')}).`);
    }
    if (w.version && !VERSION_RE.test(w.version)) {
      throw new Error(`World "${w.name}" (${w.uuid}): invalid version "${w.version}" (expected e.g. 1.20.1 or 1.21).`);
    }
    if (w.settings.gamemode && !GAMEMODES.includes(w.settings.gamemode as Gamemode)) {
      throw new Error(`World "${w.name}" (${w.uuid}): invalid gamemode "${w.settings.gamemode}" (expected ${GAMEMODES.join('|')}).`);
    }
    if (w.settings.difficulty && !DIFFICULTIES.includes(w.settings.difficulty as Difficulty)) {
      throw new Error(`World "${w.name}" (${w.uuid}): invalid difficulty "${w.settings.difficulty}" (expected ${DIFFICULTIES.join('|')}).`);
    }
    if (w.settings.levelType && !LEVEL_TYPES.includes(w.settings.levelType as LevelType)) {
      throw new Error(`World "${w.name}" (${w.uuid}): invalid levelType "${w.settings.levelType}" (expected ${LEVEL_TYPES.join('|')}).`);
    }
    // Hardcore implies hard difficulty — only meaningful once the world is filled in.
    if (isConfigured(w) && w.settings.hardcore && w.settings.difficulty !== 'hard') {
      throw new Error(
        `World "${w.name}": hardcore worlds must use difficulty "hard" ` +
          '(Minecraft forces hard difficulty when hardcore is enabled).',
      );
    }
  }

  // Every active pointer must reference an existing, configured world in that category.
  for (const c of m.categories) {
    if (!c.activeWorld) continue;
    const w = worldByUuid.get(c.activeWorld);
    if (!w) {
      throw new Error(`Category "${c.name}" activeWorld "${c.activeWorld}" is not a known world.`);
    }
    if (w.category !== c.name) {
      throw new Error(
        `Category "${c.name}" activeWorld "${c.activeWorld}" belongs to category "${w.category}".`,
      );
    }
    if (!isConfigured(w)) {
      throw new Error(
        `Category "${c.name}" activeWorld "${w.name}" (${c.activeWorld}) is not fully configured ` +
          '(engine/version/settings still blank).',
      );
    }
  }
}
