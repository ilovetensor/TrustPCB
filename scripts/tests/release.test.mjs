// Run with: node --test scripts/tests/   (or npm run test:release)
import assert from 'node:assert/strict';
import { cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, describe, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import {
  applyRelease,
  bumpFromLabels,
  bumpVersion,
  changelogNotes,
  checkVersions,
  describeTitle,
  findRecordedVersion,
  insertRelease,
  latestTagVersion,
  planRelease,
  readVersions,
  writeVersions,
} from '../release.mjs';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const scratch = [];

/** A throwaway copy of just the files the release script touches, set to 0.1.0. */
function fixture() {
  const dir = mkdtempSync(path.join(tmpdir(), 'release-test-'));
  scratch.push(dir);
  for (const file of ['package.json', 'web/package.json', 'web/package-lock.json', 'server/pyproject.toml', 'server/uv.lock']) {
    mkdirSync(path.dirname(path.join(dir, file)), { recursive: true });
    cpSync(path.join(repoRoot, file), path.join(dir, file));
  }
  writeVersions(dir, '0.1.0');
  writeFileSync(path.join(dir, 'CHANGELOG.md'), changelogWith(''));
  return { dir };
}

function changelogWith(unreleased, rest = '') {
  return `# Changelog\n\nAll notable changes to this project are documented in this file.\n\n## [Unreleased]\n${unreleased}${rest}`;
}

after(() => {
  for (const dir of scratch) rmSync(dir, { recursive: true, force: true });
});

describe('labels', () => {
  test('major bumps the first number', () => {
    assert.equal(bumpFromLabels(['major']), 'major');
    assert.equal(bumpVersion('1.4.7', 'major'), '2.0.0');
  });
  test('minor bumps the middle number and resets the last', () => {
    assert.equal(bumpFromLabels(['bug', 'minor']), 'minor');
    assert.equal(bumpVersion('1.4.7', 'minor'), '1.5.0');
  });
  test('no label, or other labels, bump the last number', () => {
    assert.equal(bumpFromLabels([]), 'patch');
    assert.equal(bumpFromLabels(['documentation', 'good first issue']), 'patch');
    assert.equal(bumpFromLabels('majority,minority'), 'patch');
    assert.equal(bumpVersion('1.4.7', 'patch'), '1.4.8');
  });
  test('both major and minor: major wins', () => {
    assert.equal(bumpFromLabels(['minor', 'major']), 'major');
    assert.equal(bumpFromLabels('minor, Major'), 'major');
  });
  test('labels are matched exactly, ignoring case and spaces', () => {
    assert.equal(bumpFromLabels(' MINOR '), 'minor');
  });
});

describe('planning', () => {
  test('first release: no tag means the version in the files is the first release', () => {
    for (const labels of [[], ['minor'], ['major']]) {
      assert.deepEqual(planRelease({ labels, fileVersion: '0.1.0', tags: [] }), { bump: bumpFromLabels(labels), base: null, next: '0.1.0' });
    }
  });
  test('non-version tags do not count as a release', () => {
    assert.equal(planRelease({ labels: [], fileVersion: '0.1.0', tags: ['v1', 'vNext', 'nightly', 'v1.2'] }).base, null);
  });
  test('a pre-existing tag is the base, and the highest tag wins (numeric, not text, order)', () => {
    assert.equal(latestTagVersion(['v0.2.0', 'v0.10.0', 'v0.9.5']), '0.10.0');
    assert.deepEqual(planRelease({ labels: ['minor'], fileVersion: '0.1.0', tags: ['v0.1.0', 'v0.2.3'] }), { bump: 'minor', base: '0.2.3', next: '0.3.0' });
  });
  test('the version file wins when it is ahead of the tags', () => {
    assert.equal(planRelease({ labels: [], fileVersion: '0.4.0', tags: ['v0.3.0'] }).next, '0.4.1');
  });
  test('a malformed version file is refused', () => {
    assert.throws(() => planRelease({ labels: [], fileVersion: '0.1', tags: [] }), /X\.Y\.Z/);
  });
});

describe('changelog', () => {
  const unreleased = '\n### Added\n\n- A hand-written note\n';

  test('a title becomes an entry in the right section with its pull request number', () => {
    assert.deepEqual(describeTitle('feat: add a thing'), { section: 'Added', text: 'Add a thing' });
    assert.deepEqual(describeTitle('fix(web): stop a crash'), { section: 'Fixed', text: 'Stop a crash' });
    assert.deepEqual(describeTitle('docs: explain it'), { section: 'Changed', text: 'Explain it' });
    assert.deepEqual(describeTitle('Plain title'), { section: 'Changed', text: 'Plain title' });
  });

  test('insertion keeps Unreleased empty on top and moves its notes into the new section', () => {
    const out = insertRelease(changelogWith(unreleased), { version: '0.2.0', date: '2026-11-05', pr: 7, title: 'feat: add a thing' });
    assert.match(out, /## \[Unreleased\]\n\n## \[0\.2\.0\] - 2026-11-05\n\n### Added\n\n- A hand-written note\n- Add a thing \(#7\)\n$/);
  });

  test('older sections stay below, in order', () => {
    const old = '\n## [0.1.0] - 2026-11-01\n\n### Added\n\n- First\n';
    const out = insertRelease(changelogWith('', old), { version: '0.1.1', date: '2026-11-05', pr: 8, title: 'fix: a bug' });
    assert.ok(out.indexOf('## [0.1.1]') < out.indexOf('## [0.1.0]'));
    assert.ok(out.indexOf('## [Unreleased]') < out.indexOf('## [0.1.1]'));
    assert.match(out, /### Fixed\n\n- A bug \(#8\)\n\n## \[0\.1\.0\]/);
  });

  test('the same pull request is never recorded twice', () => {
    const once = insertRelease(changelogWith(''), { version: '0.2.0', date: '2026-11-05', pr: 7, title: 'feat: x' });
    assert.equal(findRecordedVersion(once, 7), '0.2.0');
    assert.equal(findRecordedVersion(once, 70), null);
    assert.equal(insertRelease(once, { version: '0.2.1', date: '2026-11-06', pr: 7, title: 'feat: x' }), once);
  });

  test('a missing Unreleased heading is an error', () => {
    assert.throws(() => insertRelease('# Changelog\n', { version: '0.2.0', date: '2026-11-05', pr: 1, title: 'x' }), /Unreleased/);
  });

  test('release notes are the section body only', () => {
    const out = insertRelease(changelogWith(''), { version: '0.2.0', date: '2026-11-05', pr: 7, title: 'feat: x' });
    assert.equal(changelogNotes(out, '0.2.0'), '### Added\n\n- X (#7)\n');
    assert.throws(() => changelogNotes(out, '9.9.9'), /no section/);
  });
});

describe('version fields', () => {
  test('the real repository states one version everywhere (this is the CI drift check)', () => {
    const version = checkVersions(repoRoot);
    assert.match(version, /^\d+\.\d+\.\d+$/);
    assert.ok(readVersions(repoRoot).length >= 6);
  });

  test('a drifted field is reported by file name', () => {
    const { dir } = fixture();
    const file = path.join(dir, 'web/package.json');
    writeFileSync(file, readFileSync(file, 'utf8').replace('"version": "0.1.0"', '"version": "0.1.9"'));
    assert.throws(() => checkVersions(dir), /web\/package\.json: 0\.1\.9/);
  });

  test('applying a release writes every field in one step and nothing else changes', () => {
    const { dir } = fixture();
    const before = readFileSync(path.join(dir, 'web/package-lock.json'), 'utf8');
    const result = applyRelease(dir, { pr: 3, title: 'feat: x', labels: 'minor', date: '2026-11-05', tags: ['v0.1.0'] });
    assert.equal(result.version, '0.2.0');
    assert.equal(result.recorded, false);
    assert.equal(checkVersions(dir), '0.2.0');
    assert.deepEqual(result.changed.sort(), ['CHANGELOG.md', 'package.json', 'server/pyproject.toml', 'server/uv.lock', 'web/package-lock.json', 'web/package.json']);
    const after = readFileSync(path.join(dir, 'web/package-lock.json'), 'utf8');
    assert.equal(after.split('0.1.0').length, before.split('0.1.0').length - 2); // exactly two fields changed
    assert.equal(JSON.parse(after).packages[''].version, '0.2.0');
  });
});

describe('apply', () => {
  test('first release with no tag keeps 0.1.0 and dates the section', () => {
    const { dir } = fixture();
    const result = applyRelease(dir, { pr: 1, title: 'feat: first', labels: 'major', date: '2026-11-05', tags: [] });
    assert.equal(result.version, '0.1.0');
    assert.equal(result.bump, 'first');
    assert.equal(checkVersions(dir), '0.1.0');
    assert.match(readFileSync(path.join(dir, 'CHANGELOG.md'), 'utf8'), /## \[0\.1\.0\] - 2026-11-05/);
  });

  test('major, minor and none from a pre-existing tag', () => {
    for (const [labels, expected] of [['major', '1.0.0'], ['minor', '0.3.0'], ['', '0.2.1'], ['major,minor', '1.0.0']]) {
      const { dir } = fixture();
      const result = applyRelease(dir, { pr: 5, title: 'fix: y', labels, date: '2026-11-05', tags: ['v0.1.0', 'v0.2.0'] });
      assert.equal(result.version, expected, `labels ${JSON.stringify(labels)}`);
      assert.equal(checkVersions(dir), expected);
    }
  });

  test('a retry for the same pull request changes nothing and reports the same version', () => {
    const { dir } = fixture();
    const first = applyRelease(dir, { pr: 9, title: 'feat: z', labels: 'minor', date: '2026-11-05', tags: ['v0.1.0'] });
    const snapshot = ['CHANGELOG.md', 'package.json', 'server/uv.lock'].map((file) => readFileSync(path.join(dir, file), 'utf8'));
    // Even if a tag for the new version now exists, the retry must not bump again.
    const retry = applyRelease(dir, { pr: 9, title: 'feat: z', labels: 'major', date: '2026-11-09', tags: ['v0.1.0', first.version].map((v) => (v.startsWith('v') ? v : `v${v}`)) });
    assert.deepEqual({ version: retry.version, recorded: retry.recorded, changed: retry.changed }, { version: first.version, recorded: true, changed: [] });
    assert.deepEqual(['CHANGELOG.md', 'package.json', 'server/uv.lock'].map((file) => readFileSync(path.join(dir, file), 'utf8')), snapshot);
  });

  test('two different pull requests release two different versions', () => {
    const { dir } = fixture();
    const a = applyRelease(dir, { pr: 1, title: 'fix: a', labels: '', date: '2026-11-05', tags: ['v0.1.0'] });
    const b = applyRelease(dir, { pr: 2, title: 'fix: b', labels: '', date: '2026-11-06', tags: ['v0.1.0', `v${a.version}`] });
    assert.deepEqual([a.version, b.version], ['0.1.1', '0.1.2']);
    const log = readFileSync(path.join(dir, 'CHANGELOG.md'), 'utf8');
    assert.ok(log.indexOf('## [0.1.2]') < log.indexOf('## [0.1.1]'));
  });

  test('the Unreleased notes of the current state ride along into the first release', () => {
    const { dir } = fixture();
    writeFileSync(path.join(dir, 'CHANGELOG.md'), changelogWith('\n### Added\n\n- Initial feature set\n'));
    applyRelease(dir, { pr: 4, title: 'docs: guide', labels: '', date: '2026-11-05', tags: [] });
    const notes = changelogNotes(readFileSync(path.join(dir, 'CHANGELOG.md'), 'utf8'), '0.1.0');
    assert.match(notes, /- Initial feature set/);
    assert.match(notes, /### Changed\n\n- Guide \(#4\)/);
  });
});
