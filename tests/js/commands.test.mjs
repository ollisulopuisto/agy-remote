// Tests for the hierarchical slash-command menu data (AgyCommands).
// Run with: node --test tests/js

import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/commands.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
// Objects built inside the vm sandbox carry the sandbox's Object.prototype,
// which deepStrictEqual rejects; a JSON round-trip gives them this realm's.
const inThisRealm = (value) => JSON.parse(JSON.stringify(value));
const AgyCommands = sandbox.window.AgyCommands;

test('exposes a category tree with titles and commands', () => {
  const categories = inThisRealm(AgyCommands.categories());
  assert.ok(categories.length >= 4, 'menu needs several categories to be worth navigating');
  for (const cat of categories) {
    assert.ok(cat.id && cat.title, `category ${JSON.stringify(cat)} needs an id and a title`);
    assert.ok(cat.commands.length > 0, `category ${cat.id} must not be empty`);
    for (const cmd of cat.commands) {
      assert.match(cmd.name, /^\//, `command in ${cat.id} must start with /`);
      assert.ok(cmd.desc, `${cmd.name} needs a description`);
    }
  }
});

test('covers the known agy command set from docs/cc-parity-plan.md', () => {
  const names = inThisRealm(AgyCommands.all()).map((c) => c.name);
  for (const name of ['/clear', '/new', '/context', '/usage', '/diff', '/fast', '/fork',
    '/rewind', '/rename', '/resume', '/planning', '/tasks', '/agents', '/mcp',
    '/permissions', '/model', '/help']) {
    assert.ok(names.includes(name), `missing ${name}`);
  }
});

test('command names are unique across categories', () => {
  const names = inThisRealm(AgyCommands.all()).map((c) => c.name);
  assert.equal(new Set(names).size, names.length, 'duplicate command in the menu');
});

test('panel-opening commands are flagged so the terminal mirror can be offered', () => {
  for (const name of ['/model', '/permissions', '/resume']) {
    const cmd = inThisRealm(AgyCommands.find(name));
    assert.ok(cmd, `${name} should exist`);
    assert.equal(cmd.panel, true, `${name} opens a transient TUI panel`);
  }
  assert.notEqual(inThisRealm(AgyCommands.find('/planning')).panel, true);
});

test('argument-taking commands are flagged so the composer is prefilled', () => {
  const rename = inThisRealm(AgyCommands.find('/rename'));
  assert.equal(rename.arg, true);
  assert.notEqual(inThisRealm(AgyCommands.find('/help')).arg, true);
});

test('search matches by name and description, case-insensitively', () => {
  const byName = inThisRealm(AgyCommands.search('res'));
  assert.ok(byName.some((c) => c.name === '/resume'), '"res" should surface /resume');
  const byDesc = inThisRealm(AgyCommands.search('branch'));
  assert.ok(byDesc.some((c) => c.name === '/fork'), 'descriptions are searchable too');
  const upper = inThisRealm(AgyCommands.search('TOKEN'));
  assert.ok(upper.some((c) => c.name === '/usage'), 'description search is case-insensitive');
});

test('search results carry their category title and flags', () => {
  const hit = inThisRealm(AgyCommands.search('model')).find((c) => c.name === '/model');
  assert.ok(hit, '/model should be found');
  assert.ok(hit.cat, 'result needs its category title for display');
  assert.equal(hit.panel, true, 'flags survive the search');
});

test('search returns nothing for blank or unmatched queries', () => {
  assert.deepEqual(inThisRealm(AgyCommands.search('')), []);
  assert.deepEqual(inThisRealm(AgyCommands.search(null)), []);
  assert.deepEqual(inThisRealm(AgyCommands.search('zzzznope')), []);
});

test('find normalizes a missing slash', () => {
  assert.equal(inThisRealm(AgyCommands.find('planning')).name, '/planning');
  assert.equal(AgyCommands.find('/nope'), null);
  assert.equal(AgyCommands.find(''), null);
});
