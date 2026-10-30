#!/usr/bin/env node
// Checks that a usable kicad-cli (version 9 or newer) can be found.
//
// Discovery order, the same one server/trustpcb/pcb/kicad_cli.py uses:
//   1. KICAD_CLI_PATH. A setting is a choice: a wrong value is not passed over for another KiCad.
//      A bare name is looked up on PATH.
//   2. Where KiCad's installers put it. macOS: the app bundle, then the two Homebrew prefixes.
//      Windows: every <Program Files>\KiCad\<version>\bin, newest version first.
//   3. PATH.
//
// Exit code 0 when found and supported, 1 otherwise. `--path` prints only the executable path.

import { execFile } from 'node:child_process';
import { constants as fsConstants } from 'node:fs';
import { access, readdir, stat } from 'node:fs/promises';
import path from 'node:path';
import { promisify } from 'node:util';

const execute = promisify(execFile);

export const MACOS_LOCATIONS = [
  '/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli',
  '/opt/homebrew/bin/kicad-cli',
  '/usr/local/bin/kicad-cli',
];

const versionFolder = /^(\d+)\.(\d+)$/u;

async function isRunnable(candidate, platform) {
  try {
    if (!(await stat(candidate)).isFile()) return false;
    await access(candidate, platform === 'win32' ? fsConstants.F_OK : fsConstants.R_OK | fsConstants.X_OK);
    return true;
  } catch {
    return false;
  }
}

async function listDirectory(directory) {
  try {
    return await readdir(directory);
  } catch {
    return [];
  }
}

async function windowsLocations(env) {
  const roots = [];
  for (const name of ['ProgramW6432', 'ProgramFiles']) {
    const value = env[name]?.trim();
    if (value && !roots.includes(value)) roots.push(value);
  }
  if (roots.length === 0) roots.push('C:\\Program Files');
  const found = [];
  for (const root of roots) {
    const kicad = path.win32.join(root, 'KiCad');
    for (const name of await listDirectory(kicad)) {
      const match = versionFolder.exec(name);
      if (!match) continue;
      found.push({
        major: Number(match[1]),
        minor: Number(match[2]),
        path: path.win32.join(kicad, name, 'bin', 'kicad-cli.exe'),
      });
    }
  }
  found.sort((a, b) => b.major - a.major || b.minor - a.minor);
  return found.map((entry) => entry.path);
}

async function onPath(name, env, platform) {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  const searched = env.PATH ?? env.Path ?? '';
  const names = platform === 'win32' && !name.toLowerCase().endsWith('.exe') ? [`${name}.exe`] : [name];
  for (const directory of searched.split(paths.delimiter)) {
    if (!directory || !paths.isAbsolute(directory)) continue;
    for (const entry of names) {
      const candidate = paths.join(directory, entry);
      if (await isRunnable(candidate, platform)) return candidate;
    }
  }
  return null;
}

export async function findKicadCli(env = process.env, platform = process.platform) {
  const paths = platform === 'win32' ? path.win32 : path.posix;
  const configured = env.KICAD_CLI_PATH?.trim();
  if (configured) {
    if (configured.includes('\0')) return null;
    if (paths.isAbsolute(configured)) return (await isRunnable(configured, platform)) ? configured : null;
    return onPath(configured, env, platform);
  }
  const standard =
    platform === 'win32' ? await windowsLocations(env) : platform === 'darwin' ? MACOS_LOCATIONS : [];
  for (const candidate of standard) {
    if (await isRunnable(candidate, platform)) return candidate;
  }
  return onPath('kicad-cli', env, platform);
}

export function parseKiCadVersion(output) {
  const match = /(?:^|\s)(\d+)\.(\d+)\.(\d+)/u.exec(output.trim());
  if (!match) throw new Error(`Unable to parse KiCad version: ${output.trim() || '<empty>'}`);
  return { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]) };
}

export const supportsKiCad = (version) => version.major >= 9;

async function main() {
  const executable = await findKicadCli();
  if (!executable) {
    const configured = process.env.KICAD_CLI_PATH?.trim();
    throw new Error(
      configured
        ? `KiCad CLI is not installed or configured: KICAD_CLI_PATH is set to ${configured}, which is not a program on this computer.`
        : 'KiCad CLI is not installed or configured. Install KiCad 9 or newer, or set KICAD_CLI_PATH.',
    );
  }
  if (process.argv.includes('--path')) {
    console.log(executable);
    return;
  }
  const { stdout } = await execute(executable, ['version']);
  const version = parseKiCadVersion(stdout);
  const formatted = [version.major, version.minor, version.patch].join('.');
  if (!supportsKiCad(version)) throw new Error(`KiCad ${formatted} is unsupported; V1 needs 9+.`);
  console.log(`KiCad ${formatted} is available at ${executable}.`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}
