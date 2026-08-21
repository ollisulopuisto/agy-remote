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
