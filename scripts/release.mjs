#!/usr/bin/env node
// Release helper, used by .github/workflows/release.yml and runnable locally.
//
//   node scripts/release.mjs check
//       Fail unless every version field in the repository states the same version.
//   node scripts/release.mjs plan  --labels "minor,docs"
//       Print the bump and the next version as JSON. Changes nothing.
//   node scripts/release.mjs apply --pr 12 --title "feat: add a thing" --labels "minor" [--date 2026-11-05]
//       Compute the next version, write it to every version field and add the CHANGELOG entry.
//       Safe to run twice for the same pull request: the second run changes nothing.
//   node scripts/release.mjs notes --version 0.2.0
//       Print the CHANGELOG section of one version (used as the GitHub Release notes).
//
// Rules: label "major" bumps X, label "minor" bumps Y, anything else bumps Z (X.Y.Z). Major wins
// over minor. The base is the highest vX.Y.Z git tag or the version in package.json, whichever is
// higher. With no tag at all, nothing has been released yet and the first release is the version
// in package.json (0.1.0) whatever the labels say.
//
// No dependencies. Only Node's standard library.
import { execFileSync } from 'node:child_process';
import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SEMVER = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

// ---------------------------------------------------------------------------------------------
// Version arithmetic

export function parseVersion(text) {
  const match = SEMVER.exec(String(text).trim());
  if (!match) throw new Error(`Not a version of the form X.Y.Z: ${JSON.stringify(text)}`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

export function compareVersions(a, b) {
  const [x, y] = [parseVersion(a), parseVersion(b)];
  for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}

/** 'major' | 'minor' | 'patch' from a list of label names. Major wins over minor. */
export function bumpFromLabels(labels) {
  const names = (Array.isArray(labels) ? labels : String(labels ?? '').split(','))
    .map((name) => String(name).trim().toLowerCase());
  if (names.includes('major')) return 'major';
  if (names.includes('minor')) return 'minor';
  return 'patch';
}

export function bumpVersion(version, bump) {
  const [major, minor, patch] = parseVersion(version);
  if (bump === 'major') return `${major + 1}.0.0`;
  if (bump === 'minor') return `${major}.${minor + 1}.0`;
  if (bump === 'patch') return `${major}.${minor}.${patch + 1}`;
  throw new Error(`Unknown bump: ${bump}`);
}

/** Highest strict vX.Y.Z tag among the given tag names, or null. Other tags are ignored. */
export function latestTagVersion(tags) {
  const versions = tags
    .map((tag) => /^v(\d+\.\d+\.\d+)$/.exec(String(tag).trim()))
    .filter((match) => match && SEMVER.test(match[1]))
    .map((match) => match[1]);
  if (versions.length === 0) return null;
  return versions.reduce((best, version) => (compareVersions(version, best) > 0 ? version : best));
}

/**
 * The version to release. `fileVersion` is the version the files state now; `tags` are the git
 * tag names. Returns { bump, base, next }; `base` is null for the first release.
 */
export function planRelease({ labels, fileVersion, tags }) {
  const bump = bumpFromLabels(labels);
  parseVersion(fileVersion);
  const tagged = latestTagVersion(tags);
  if (tagged === null) return { bump, base: null, next: fileVersion };
  const base = compareVersions(fileVersion, tagged) > 0 ? fileVersion : tagged;
  return { bump, base, next: bumpVersion(base, bump) };
}

// ---------------------------------------------------------------------------------------------
// Version fields. Each target names one file and says how to read and write its version fields.

function replaceOnce(text, pattern, replacement, what) {
  let hit = false;
  const out = text.replace(pattern, (...args) => {
    hit = true;
    return replacement(...args);
  });
  if (!hit) throw new Error(`Could not find ${what}`);
  return out;
}

export const VERSION_TARGETS = [
  {
    file: 'package.json',
    read: (text) => [JSON.parse(text).version],
    write: (text, version) => replaceOnce(text, /^(  "version": ")[^"]*(")/m, (_, a, b) => `${a}${version}${b}`, 'the version in package.json'),
  },
  {
    file: 'web/package.json',
    read: (text) => [JSON.parse(text).version],
    write: (text, version) => replaceOnce(text, /^(  "version": ")[^"]*(")/m, (_, a, b) => `${a}${version}${b}`, 'the version in web/package.json'),
  },
  {
    file: 'web/package-lock.json',
    read: (text) => {
      const lock = JSON.parse(text);
      return [lock.version, lock.packages?.['']?.version];
    },
    write: (text, version) => {
      const top = replaceOnce(text, /^(  "version": ")[^"]*(")/m, (_, a, b) => `${a}${version}${b}`, 'the version in web/package-lock.json');
      return replaceOnce(top, /^(    "": \{[^}]*?"version": ")[^"]*(")/m, (_, a, b) => `${a}${version}${b}`, 'the root package version in web/package-lock.json');
    },
  },
  {
    file: 'server/pyproject.toml',
    read: (text) => [/^version = "([^"]*)"/m.exec(text)?.[1]],
    write: (text, version) => replaceOnce(text, /^(version = ")[^"]*(")/m, (_, a, b) => `${a}${version}${b}`, 'the version in server/pyproject.toml'),
  },
  {
    file: 'server/uv.lock',
    read: (text) => [/^name = "trustpcb-server"\nversion = "([^"]*)"/m.exec(text)?.[1]],
    write: (text, version) => replaceOnce(text, /^(name = "trustpcb-server"\nversion = ")[^"]*(")/m, (_, a, b) => `${a}${version}${b}`, 'the trustpcb-server version in server/uv.lock'),
  },
];

export function readVersions(root) {
  const found = [];
  for (const target of VERSION_TARGETS) {
    const text = readFileSync(path.join(root, target.file), 'utf8');
    for (const version of target.read(text)) found.push({ file: target.file, version });
  }
  return found;
}

/** Throws when version fields disagree. Returns the shared version. */
export function checkVersions(root) {
  const found = readVersions(root);
  const bad = found.filter((entry) => typeof entry.version !== 'string' || !SEMVER.test(entry.version));
  if (bad.length) throw new Error(`Missing or malformed version in: ${bad.map((entry) => `${entry.file} (${entry.version})`).join(', ')}`);
  const distinct = [...new Set(found.map((entry) => entry.version))];
  if (distinct.length !== 1) {
    const lines = found.map((entry) => `  ${entry.file}: ${entry.version}`).join('\n');
    throw new Error(`Version fields disagree:\n${lines}\nRun: node scripts/release.mjs set <version>`);
  }
  return distinct[0];
}

export function writeVersions(root, version) {
  parseVersion(version);
  const changed = [];
  for (const target of VERSION_TARGETS) {
    const file = path.join(root, target.file);
    const before = readFileSync(file, 'utf8');
    const after = target.write(before, version);
    if (after !== before) {
      writeFileSync(file, after);
      changed.push(target.file);
    }
  }
  return changed;
}

// ---------------------------------------------------------------------------------------------
// CHANGELOG (Keep a Changelog)

const CHANGELOG_SECTIONS = ['Added', 'Changed', 'Deprecated', 'Removed', 'Fixed', 'Security'];
const UNRELEASED = /^## \[Unreleased\][^\n]*$/m;
const SECTION_HEADING = /^## \[(\d+\.\d+\.\d+)\][^\n]*$/gm;

/** Turns a pull request title into { section, text }. "feat: x" is Added, "fix: x" is Fixed. */
export function describeTitle(title) {
  const clean = String(title ?? '').replace(/\s+/g, ' ').trim();
  const match = /^(\w+)(\([^)]*\))?(!)?:\s*(.+)$/.exec(clean);
  let section = 'Changed';
  let text = clean;
  if (match) {
    const type = match[1].toLowerCase();
    text = match[4];
    if (type === 'feat') section = 'Added';
    else if (type === 'fix') section = 'Fixed';
    else if (type === 'security') section = 'Security';
    else if (type === 'revert') section = 'Removed';
  }
  text = text.charAt(0).toUpperCase() + text.slice(1);
  return { section, text: text || `Pull request` };
}

/** The version a pull request was already released in, according to the changelog, or null. */
export function findRecordedVersion(changelog, pr) {
  const entry = new RegExp(`\\(#${Number(pr)}\\)`);
  const headings = [...changelog.matchAll(SECTION_HEADING)];
  for (let i = 0; i < headings.length; i += 1) {
    const end = i + 1 < headings.length ? headings[i + 1].index : changelog.length;
    if (entry.test(changelog.slice(headings[i].index, end))) return headings[i][1];
  }
  return null;
}

function parseSections(body) {
  // body: the text under a heading. Returns { Added: [lines], ... } and keeps unknown text under "Changed".
  const sections = {};
  let current = null;
  for (const line of body.split('\n')) {
    const heading = /^### (.+?)\s*$/.exec(line);
    if (heading) {
      current = heading[1];
      sections[current] ??= [];
    } else if (line.trim() !== '') {
      current ??= 'Changed';
      sections[current] ??= [];
      sections[current].push(line);
    }
  }
  return sections;
}

function renderSections(sections) {
  const names = [...CHANGELOG_SECTIONS, ...Object.keys(sections).filter((name) => !CHANGELOG_SECTIONS.includes(name))];
  return names
    .filter((name) => sections[name]?.length)
    .map((name) => `### ${name}\n\n${sections[name].join('\n')}\n`)
    .join('\n');
}

/**
 * Moves the Unreleased entries plus this pull request's entry into a new dated section. The
 * Unreleased heading stays, empty, above it. Returns the new changelog text, unchanged when the
 * pull request is already recorded or the version already has a section.
 */
export function insertRelease(changelog, { version, date, pr, title }) {
  parseVersion(version);
  if (!UNRELEASED.test(changelog)) throw new Error('CHANGELOG.md has no "## [Unreleased]" heading');
  if (pr != null && findRecordedVersion(changelog, pr) !== null) return changelog;
  if (new RegExp(`^## \\[${version.replace(/\./g, '\\.')}\\]`, 'm').test(changelog)) {
    throw new Error(`CHANGELOG.md already has a section for ${version}`);
  }
  const start = changelog.search(UNRELEASED);
  const afterHeading = changelog.indexOf('\n', start) + 1;
  const rest = changelog.slice(afterHeading);
  const next = rest.search(/^## /m);
  const body = next === -1 ? rest : rest.slice(0, next);
  const tail = next === -1 ? '' : rest.slice(next);
  const sections = parseSections(body);
  if (pr != null) {
    const { section, text } = describeTitle(title);
    (sections[section] ??= []).push(`- ${text} (#${Number(pr)})`);
  }
  const rendered = renderSections(sections) || '### Changed\n\n- No notable changes.\n';
  const head = changelog.slice(0, afterHeading);
  return `${head}\n## [${version}] - ${date}\n\n${rendered}${tail ? `\n${tail}` : ''}`;
}

/** The body of one version's section, without its heading. */
export function changelogNotes(changelog, version) {
  const heading = new RegExp(`^## \\[${version.replace(/\./g, '\\.')}\\][^\\n]*\\n`, 'm').exec(changelog);
  if (!heading) throw new Error(`CHANGELOG.md has no section for ${version}`);
  const from = heading.index + heading[0].length;
  const rest = changelog.slice(from);
  const next = rest.search(/^## /m);
  return `${(next === -1 ? rest : rest.slice(0, next)).trim()}\n`;
}

// ---------------------------------------------------------------------------------------------
// The apply step

function gitTags(root) {
  try {
    return execFileSync('git', ['tag', '--list', 'v*'], { cwd: root, encoding: 'utf8' }).split('\n').filter(Boolean);
  } catch {
    return [];
  }
}

/**
 * Plans and writes one release. `tags` defaults to the repository's git tags. Returns
 * { version, bump, recorded, changed } where `recorded` is true when the pull request was
 * already released and nothing was written.
 */
export function applyRelease(root, { pr, title, labels, date, tags }) {
  const changelogFile = path.join(root, 'CHANGELOG.md');
  const changelog = readFileSync(changelogFile, 'utf8');
  const recorded = findRecordedVersion(changelog, pr);
  if (recorded !== null) {
    return { version: recorded, bump: null, recorded: true, changed: [] };
  }
  const fileVersion = checkVersions(root);
  const plan = planRelease({ labels, fileVersion, tags: tags ?? gitTags(root) });
  const changed = writeVersions(root, plan.next);
  const updated = insertRelease(changelog, { version: plan.next, date, pr, title });
  if (updated !== changelog) {
    writeFileSync(changelogFile, updated);
    changed.push('CHANGELOG.md');
  }
  return { version: plan.next, bump: plan.base === null ? 'first' : plan.bump, recorded: false, changed };
}

// ---------------------------------------------------------------------------------------------
// Command line

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) throw new Error(`Unexpected argument: ${arg}`);
    const key = arg.slice(2);
    args[key] = argv[i + 1] !== undefined && !argv[i + 1].startsWith('--') ? argv[(i += 1)] : '';
  }
  return args;
}

function emit(result) {
  console.log(JSON.stringify(result));
  if (process.env.GITHUB_OUTPUT) {
    const lines = Object.entries(result).filter(([, value]) => typeof value !== 'object').map(([key, value]) => `${key}=${value}`);
    appendFileSync(process.env.GITHUB_OUTPUT, `${lines.join('\n')}\n`);
  }
}

function main(argv) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const [command, ...rest] = argv;
  const args = command === 'set' ? {} : parseArgs(rest);
  const today = new Date().toISOString().slice(0, 10);
  switch (command) {
    case 'check':
      console.log(`All version fields are ${checkVersions(root)}`);
      return;
    case 'set':
      console.log(JSON.stringify({ changed: writeVersions(root, rest[0]) }));
      return;
    case 'plan':
      emit(planRelease({ labels: args.labels ?? '', fileVersion: checkVersions(root), tags: gitTags(root) }));
      return;
    case 'apply': {
      if (!/^\d+$/.test(args.pr ?? '')) throw new Error('apply needs --pr <number>');
      emit(applyRelease(root, { pr: Number(args.pr), title: args.title ?? '', labels: args.labels ?? '', date: args.date || today }));
      return;
    }
    case 'notes':
      process.stdout.write(changelogNotes(readFileSync(path.join(root, 'CHANGELOG.md'), 'utf8'), args.version ?? ''));
      return;
    default:
      throw new Error('Usage: release.mjs check | plan --labels L | apply --pr N --title T --labels L [--date D] | notes --version V | set <version>');
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exit(1);
  }
}
