// Tests for the PWA's new-session helpers. Run with: node --test tests/js
//
// static/sessions.js is a classic script (the PWA loads no modules and no
// bundler), so it is evaluated here in a sandbox with a stand-in window.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/sessions.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const { buildSpawnRequest, spawnStageLabel, spawnEventMatches, sessionStatus } = sandbox.window.AgySessions;

// Objects built inside the vm sandbox carry the sandbox's Object.prototype,
// which deepStrictEqual rejects; a JSON round-trip gives them this realm's.
const inThisRealm = (value) => JSON.parse(JSON.stringify(value));

// -- buildSpawnRequest: the sheet's fields become the /api/sessions body ----

test('a filled form becomes the spawn request', () => {
  const result = buildSpawnRequest({
    repoUrl: '  https://github.com/org/repo  ',
    branch: 'dev',
    task: 'Fix the build',
    name: 'repo',
  });
  assert.equal(result.ok, true);
  assert.deepEqual(inThisRealm(result.payload), {
    repo_url: 'https://github.com/org/repo',
    branch: 'dev',
    task: 'Fix the build',
    name: 'repo',
  });
});

test('optional fields that are blank are omitted, not sent empty', () => {
  const result = buildSpawnRequest({ repoUrl: 'https://github.com/org/repo', branch: '   ', task: '', name: null });
  assert.equal(result.ok, true);
  assert.deepEqual(inThisRealm(result.payload), { repo_url: 'https://github.com/org/repo' });
});

test('an empty repo url is refused before anything is sent', () => {
  for (const repoUrl of ['', '   ', null, undefined]) {
    const result = buildSpawnRequest({ repoUrl, task: 'whatever' });
    assert.equal(result.ok, false);
    assert.match(result.error, /repo/i);
    assert.equal(result.payload, undefined);
  }
});

// -- spawnStageLabel: what the sheet's chip says per stage ------------------

test('the chip names the in-progress stage', () => {
  assert.match(spawnStageLabel('cloning'), /clon/i);
  assert.match(spawnStageLabel('starting'), /start/i);
});

test("a failed stage shows the server's reason, not a generic error", () => {
  assert.match(spawnStageLabel('failed', 'fatal: repository not found'), /repository not found/);
});

test('an unknown stage says nothing rather than guessing', () => {
  assert.equal(spawnStageLabel('nope'), null);
  assert.equal(spawnStageLabel(undefined), null);
});

// -- spawnEventMatches: only this sheet's spawn drives this sheet ------------

test('a spawn event is ours when the tmux session matches', () => {
  const handle = { name: 'repo', workdir: '/p/repo', tmux_session: 'agy-remote-repo' };
  const theirs = { name: 'other', workdir: '/p/other', tmux_session: 'agy-remote-other' };
  assert.equal(spawnEventMatches(theirs, handle), false);
  assert.equal(spawnEventMatches({ ...theirs, tmux_session: handle.tmux_session }, handle), true);
});

test('a spawn event is ours when the workdir matches', () => {
  const handle = { name: 'repo', workdir: '/p/repo', tmux_session: 'agy-remote-repo' };
  assert.equal(spawnEventMatches({ workdir: '/p/repo' }, handle), true);
  assert.equal(spawnEventMatches({ workdir: '/p/repo-2' }, handle), false);
});

test('a spawn event is ours when only the name matches', () => {
  const handle = { name: 'repo', workdir: '/p/repo', tmux_session: 'agy-remote-repo' };
  assert.equal(spawnEventMatches({ name: 'repo' }, handle), true);
  assert.equal(spawnEventMatches({ name: 'Repo' }, handle), false);
});

test('an event without any identifying field is nobody\'s', () => {
  const handle = { name: 'repo', workdir: '/p/repo', tmux_session: 'agy-remote-repo' };
  assert.equal(spawnEventMatches({}, handle), false);
  assert.equal(spawnEventMatches(null, handle), false);
});
// -- sessionStatus: the drawer dot and the Stop button share one rule -------

test('a pending approval wins over everything: red', () => {
  assert.equal(sessionStatus({ pending: 2, lastActivityAt: 0, now: 1000, isActive: true }), 'approval');
  assert.equal(sessionStatus({ pending: 1, lastActivityAt: null, now: 0, isActive: false }), 'approval');
});

test('a recent transcript update means busy: yellow', () => {
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 90000, now: 100000, isActive: false }), 'busy');
  // Busy beats active: the dot on the current session pulses while it works.
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 90000, now: 100000, isActive: true }), 'busy');
});

test('quiescence beyond the window is no longer busy', () => {
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 60000, now: 100000, isActive: true }), 'active');
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 60000, now: 100000, isActive: false }), 'idle');
});

test('the boundary is exact: a full window of silence is quiescent', () => {
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 70001, now: 100000, isActive: true }), 'busy');
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 70000, now: 100000, isActive: true }), 'active');
});

test('a session with no activity at all is just active or idle', () => {
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: null, now: 100000, isActive: true }), 'active');
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: null, now: 100000, isActive: false }), 'idle');
});

test('a caller may tighten the busy window for tests', () => {
  assert.equal(sessionStatus({ pending: 0, lastActivityAt: 99900, now: 100000, isActive: false, busyWindowMs: 50 }), 'idle');
});
