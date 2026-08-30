// Tests for the working-tree diff pane's pure parser. Run with: node --test tests/js
//
// The server splits `git diff HEAD` into per-file chunks (tests/test_git_diff.py
// covers that); this parses one chunk into typed lines the PWA renders with its
// own escape-first helpers. format.js never produces HTML, so neither does this.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/format.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const { parseUnifiedDiff } = sandbox.window.AgyFormat;

const CHUNK = [
  'diff --git a/hello.txt b/hello.txt',
  'index 111..222 100644',
  '--- a/hello.txt',
  '+++ b/hello.txt',
  '@@ -1,2 +1,2 @@',
  'line one',
  '-line two',
  '+line two changed',
].join('\n');

test('a chunk parses into typed lines in order', () => {
  const lines = parseUnifiedDiff(CHUNK);
  assert.deepEqual(
    [...lines.map((l) => l.type)],
    ['meta', 'meta', 'meta', 'meta', 'hunk', 'ctx', 'del', 'add']
  );
  assert.equal(lines[6].text, '-line two');
  assert.equal(lines[7].text, '+line two changed');
});

test('adds and dels carry their bare line text for rendering', () => {
  const lines = parseUnifiedDiff(CHUNK);
  const adds = lines.filter((l) => l.type === 'add');
  const dels = lines.filter((l) => l.type === 'del');
  assert.equal(adds.length, 1);
  assert.equal(dels.length, 1);
  assert.equal(adds[0].text, '+line two changed');
});

test('the path is parsed from the b/ side of the header', () => {
  const lines = parseUnifiedDiff(CHUNK);
  assert.equal(lines[0].path, 'hello.txt');
});

test('empty and non-diff input parse to nothing', () => {
  assert.equal(parseUnifiedDiff('').length, 0);
  assert.equal(parseUnifiedDiff('not a diff at all').length, 0);
});

test('a binary-file chunk parses with no add or del counts', () => {
  const binary = 'diff --git a/logo.png b/logo.png\nBinary files /dev/null and b/logo.png differ';
  const lines = parseUnifiedDiff(binary);
  assert.equal(lines.filter((l) => l.type === 'add' || l.type === 'del').length, 0);
  assert.equal(lines[0].path, 'logo.png');
});
