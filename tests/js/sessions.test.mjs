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
const { buildSpawnRequest, spawnStageLabel, spawnEventMatches, sessionStatus, sessionStatusOf, renameRequestPayload, parseRenameCommand, applyRenameEvent, breadcrumbSegments } = sandbox.window.AgySessions;

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

// -- sessionStatusOf: a drawer row, from what the server actually sends ------
//
// The row carries an ISO `updated_at` (the transcript's mtime), the id of the
// session on screen, and the row's own pending-approval count.

const NOW = 100000;
const iso = (ms) => new Date(ms).toISOString();

test('a row updated inside the window is busy', () => {
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(90000) }, 'b', 0, NOW), 'busy');
});

test('the row on screen is still busy while it works', () => {
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(90000) }, 'a', 0, NOW), 'busy');
});

test('a quiet row is active on screen and idle away from it', () => {
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(10000) }, 'a', 0, NOW), 'active');
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(10000) }, 'b', 0, NOW), 'idle');
});

test('a pending gate makes the row approval even when it is quiet', () => {
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(10000) }, 'b', 2, NOW), 'approval');
});

test('a row with no timestamp is just active or idle', () => {
  assert.equal(sessionStatusOf({ id: 'a' }, 'a', 0, NOW), 'active');
  assert.equal(sessionStatusOf({ id: 'a', updated_at: null }, 'b', 0, NOW), 'idle');
});

test('a timestamp the server cannot send is no activity at all', () => {
  assert.equal(sessionStatusOf({ id: 'a', updated_at: 'not a date' }, 'b', 0, NOW), 'idle');
});

test('a step this client just saw beats the drawer snapshot', () => {
  // The drawer fetched a quiet timestamp; a step arrived since.
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(10000) }, 'a', 0, NOW, 95000), 'busy');
});

test('the fresher of the two sources wins, in either direction', () => {
  // Server says recent, the client last saw it a while ago: still busy.
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(90000) }, 'a', 0, NOW, 10000), 'busy');
  // Both quiet: no source rescues it.
  assert.equal(sessionStatusOf({ id: 'a', updated_at: iso(10000) }, 'b', 0, NOW, 5000), 'idle');
  // A sighting with no server timestamp is activity.
  assert.equal(sessionStatusOf({ id: 'a' }, 'b', 0, NOW, 95000), 'busy');
});

// -- renaming a session from the drawer --------------------------------------

test('a rename is trimmed and refused when blank', () => {
  assert.deepEqual(
    inThisRealm(renameRequestPayload('  Fresh name ')),
    { ok: true, payload: { title: 'Fresh name' } },
  );
  for (const title of ['', '   ', null, undefined]) {
    const result = renameRequestPayload(title);
    assert.equal(result.ok, false);
    assert.equal(result.payload, undefined);
  }
});

// -- /rename typed in the composer: the name is the server's, not the agent's -
//
// A `/rename <name>` sent as a prompt reached the agent and the transcript,
// while the drawer's names live in the server's title store -- so the sidebar
// never moved. The composer hands the command to the rename API instead.

test('a /rename prompt is intercepted and its argument becomes the title', () => {
  assert.deepEqual(
    inThisRealm(parseRenameCommand('/rename Fresh name')),
    { ok: true, title: 'Fresh name' },
  );
  assert.deepEqual(
    inThisRealm(parseRenameCommand('  /rename   Fresh name  ')),
    { ok: true, title: 'Fresh name' },
  );
});

test('a bare /rename is refused before anything is sent', () => {
  for (const prompt of ['/rename', '/rename   ']) {
    const result = parseRenameCommand(prompt);
    assert.equal(result.ok, false);
    assert.match(result.error, /name is required/i);
    assert.equal(result.title, undefined);
  }
});

test('anything else is not a rename and goes to the agent untouched', () => {
  assert.equal(parseRenameCommand('rename the file please'), null);
  assert.equal(parseRenameCommand('/renamified stuff'), null);
  assert.equal(parseRenameCommand('/model'), null);
  assert.equal(parseRenameCommand(''), null);
  assert.equal(parseRenameCommand(null), null);
});

test('a rename event updates the matching row and only that row', () => {
  const convs = [
    { id: 'a', title: 'Old A' },
    { id: 'b', title: 'B' },
  ];
  const updated = applyRenameEvent(convs, 'a', 'New A');
  assert.equal(updated[0].title, 'New A');
  assert.equal(updated[1].title, 'B');
  // The drawer's own array is not mutated in place.
  assert.equal(convs[0].title, 'Old A');
  // An id no row carries changes nothing.
  assert.deepEqual(inThisRealm(applyRenameEvent(convs, 'zzz', 'X')), inThisRealm(convs));
});

// -- the file tree's breadcrumb ----------------------------------------------

test('a path becomes tappable segments, shallowest first', () => {
  const crumbs = breadcrumbSegments('/Users/me/proj/src');
  assert.deepEqual(
    inThisRealm(crumbs),
    [
      { name: 'Users', path: '/Users' },
      { name: 'me', path: '/Users/me' },
      { name: 'proj', path: '/Users/me/proj' },
      { name: 'src', path: '/Users/me/proj/src' },
    ],
  );
});

test('the root and degenerate paths yield nothing to draw', () => {
  assert.deepEqual(inThisRealm(breadcrumbSegments('/')), []);
  for (const bad of ['', null, undefined]) {
    assert.deepEqual(inThisRealm(breadcrumbSegments(bad)), []);
  }
});

test('with a root, crumbs start at the root, not the filesystem', () => {
  const crumbs = breadcrumbSegments('/Users/me/proj/src/sub', '/Users/me/proj');
  assert.deepEqual(
    inThisRealm(crumbs),
    [
      { name: 'proj', path: '/Users/me/proj' },
      { name: 'src', path: '/Users/me/proj/src' },
      { name: 'sub', path: '/Users/me/proj/src/sub' },
    ],
  );
  // The root itself has only the home crumb, which the pane draws itself.
  assert.deepEqual(inThisRealm(breadcrumbSegments('/Users/me/proj', '/Users/me/proj')), [
    { name: 'proj', path: '/Users/me/proj' },
  ]);
  // A path outside the root falls back to the absolute segments.
  assert.deepEqual(inThisRealm(breadcrumbSegments('/etc', '/Users/me/proj')), [
    { name: 'etc', path: '/etc' },
  ]);
});
