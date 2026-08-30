// Tests for the PWA's pure formatting helpers. Run with: node --test tests/js
//
// static/format.js is a classic script (the PWA loads no modules and no
// bundler), so it is evaluated here in a sandbox with a stand-in window.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/format.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const { toolSummary, outputSummary, firstLine } = sandbox.window.AgyFormat;

test('a tool card collapses to its name and the argument that matters', () => {
  assert.equal(
    toolSummary('run_command', { CommandLine: 'du -hd 1 /Users/dst', Cwd: '/Users/dst', WaitMsBeforeAsync: '5000' }),
    'run_command(du -hd 1 /Users/dst)'
  );
  assert.equal(
    toolSummary('grep_search', { CaseInsensitive: true, Query: 'season', SearchPath: '/Users/dst' }),
    'grep_search(season)'
  );
  assert.equal(toolSummary('view_file', { TargetFile: 'src/app.py' }), 'view_file(src/app.py)');
});

test('an unrecognised tool falls back to its first text argument', () => {
  assert.equal(toolSummary('mystery_tool', { flag: true, note: 'do the thing' }), 'mystery_tool(do the thing)');
  assert.equal(toolSummary('no_args_tool', {}), 'no_args_tool');
});

test('a long argument is truncated, never wrapped across the header', () => {
  const summary = toolSummary('run_command', { CommandLine: 'x'.repeat(200) });
  assert.ok(summary.length < 90, summary.length);
  assert.ok(summary.endsWith('…)'));
});

test('output collapses to a line count, so a directory listing is one row', () => {
  assert.equal(outputSummary('a\nb\nc\nd'), 'output · 4 lines');
  assert.equal(outputSummary('Exit code 0\n\n'), 'Exit code 0');
  assert.equal(outputSummary(''), 'output');
});

test('short single-line output is shown as itself, with nothing to expand', () => {
  assert.equal(outputSummary('No results found'), 'No results found');
});

test('firstLine trims and truncates without breaking on empty input', () => {
  assert.equal(firstLine('  hello \nworld'), 'hello');
  assert.equal(firstLine(''), '');
  assert.equal(firstLine('y'.repeat(300)).length, 80);
});

test('a session announces itself, so a new one is not read as more of the last', () => {
  const { sessionLabel } = sandbox.window.AgyFormat;

  assert.equal(
    sessionLabel({ id: '8511476a-e89e-4ff4-8275-663cbab32678', step_count: 12 }, '17:35'),
    'Session 8511476a · started 17:35 · 12 steps'
  );
  assert.equal(sessionLabel({ id: 'abcdef12', step_count: 1 }, '09:04'), 'Session abcdef12 · started 09:04 · 1 step');
  assert.equal(sessionLabel({ id: 'abcdef12' }, ''), 'Session abcdef12');
  assert.equal(sessionLabel(null, '17:35'), '');
});

test('mailbox envelope parses agent-to-agent messages and ignores human prompts', () => {
  const { parseEnvelope } = sandbox.window.AgyFormat;

  const agentMsg = parseEnvelope('[message from agy-work: tests are red, check log]');
  assert.equal(agentMsg.isEnvelope, true);
  assert.equal(agentMsg.from, 'agy-work');
  assert.equal(agentMsg.text, 'tests are red, check log');

  const multiline = parseEnvelope('[message from agy-db:\nLine 1\nLine 2\n]');
  assert.equal(multiline.isEnvelope, true);
  assert.equal(multiline.from, 'agy-db');
  assert.equal(multiline.text, 'Line 1\nLine 2');

  const human = parseEnvelope('just a regular human prompt');
  assert.equal(human.isEnvelope, false);
  assert.equal(human.from, null);
  assert.equal(human.text, 'just a regular human prompt');

  const empty = parseEnvelope('');
  assert.equal(empty.isEnvelope, false);
  assert.equal(empty.text, '');
});

test('trafficForSession filters pairs where session is a member', () => {
  const { trafficForSession } = sandbox.window.AgyFormat;
  const pairs = [
    { a: 'agy-a', b: 'agy-b', count: 3, looping: false, muted: false },
    { a: 'agy-b', b: 'agy-c', count: 5, looping: true, muted: false },
    { a: 'agy-x', b: 'agy-y', count: 1, looping: false, muted: true }
  ];

  const forB = trafficForSession(pairs, 'agy-b');
  assert.equal(forB.length, 2);
  assert.equal(forB[0].a, 'agy-a');
  assert.equal(forB[1].b, 'agy-c');

  assert.equal(trafficForSession(pairs, 'agy-unknown').length, 0);
  assert.equal(trafficForSession(null, 'agy-a').length, 0);
});

test('formatTrafficPill formats display label, peer, and status states', () => {
  const { formatTrafficPill } = sandbox.window.AgyFormat;

  const normal = formatTrafficPill({ a: 'agy-a', b: 'agy-b', count: 4, looping: false, muted: false }, 'agy-a');
  assert.equal(normal.peer, 'agy-b');
  assert.equal(normal.count, 4);
  assert.equal(normal.state, 'active');
  assert.equal(normal.label, '⇄ agy-b ×4');

  const looping = formatTrafficPill({ a: 'agy-a', b: 'agy-b', count: 20, looping: true, muted: false }, 'agy-b');
  assert.equal(looping.peer, 'agy-a');
  assert.equal(looping.state, 'looping');
  assert.equal(looping.looping, true);

  const muted = formatTrafficPill({ a: 'agy-a', b: 'agy-c', count: 0, looping: false, muted: true }, 'agy-a');
  assert.equal(muted.peer, 'agy-c');
  assert.equal(muted.state, 'muted');
  assert.equal(muted.muted, true);
});

test('file references in the transcript become tappable chips', () => {
  const { parseFileRefs } = sandbox.window.AgyFormat;
  // agy writes absolute paths wrapped as [file:///abs/path] -- in prose and in
  // tool summaries. The server holds those bytes, so the phone can show the
  // file; the parser only has to find the references.
  const text =
    'Implemented ([file:///Users/dst/x/harness/src/classifier.ts] and ' +
    '[file:///Users/dst/x/harness/tests/ts/classifier.ts])';
  const refs = parseFileRefs(text);
  assert.equal(refs.length, 2);
  assert.equal(refs[0].path, '/Users/dst/x/harness/src/classifier.ts');
  assert.equal(refs[0].name, 'classifier.ts');
  assert.equal(refs[1].path, '/Users/dst/x/harness/tests/ts/classifier.ts');

  // The same reference twice is deduplicated, so a long answer yields one chip
  // per file, not one per mention.
  const dupes = parseFileRefs('[file:///a/b.ts] then [file:///a/b.ts] again');
  assert.equal(dupes.length, 1);

  // No references, or nothing to parse: no chips. (Spread: arrays built
  // inside the vm sandbox carry a foreign Array prototype.)
  assert.deepEqual([...parseFileRefs('no refs here')], []);
  assert.deepEqual([...parseFileRefs('')], []);
  assert.deepEqual([...parseFileRefs(null)], []);

  // Only absolute paths can be served: file://host/path has a host part, and
  // file://relative is just a malformed reference. Neither becomes a chip that
  // asks the server for something it would have to refuse.
  assert.deepEqual([...parseFileRefs('[file://host/share/x.txt]')], []);
  assert.deepEqual([...parseFileRefs('[file://relative/path.txt]')], []);

  // Paths with spaces survive -- repos have them.
  const spaced = parseFileRefs('[file:///Users/dst/My Repo/src/a.ts]');
  assert.equal(spaced.length, 1);
  assert.equal(spaced[0].path, '/Users/dst/My Repo/src/a.ts');
  assert.equal(spaced[0].name, 'a.ts');
});

