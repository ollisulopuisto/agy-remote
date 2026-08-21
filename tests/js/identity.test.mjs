// The PWA's agent identity. Run with: node --test tests/js
//
// The header, the tab title and the manifest all said "agy" whatever was
// behind them, so an opencode session on the phone read as an agy one -- the
// tool cards even carry agy's step vocabulary. The name shown must come from
// the server, and must never fall back to the name of one particular agent.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/format.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const { agentIdentity } = sandbox.window.AgyFormat;

test('the shown name is the agent the server reports', () => {
  assert.equal(agentIdentity('opencode').label, 'opencode');
  assert.equal(agentIdentity('opencode').title, 'opencode remote');
  assert.equal(agentIdentity('agy').label, 'agy');
  assert.equal(agentIdentity('agy').title, 'agy remote');
});

test('an unknown agent is never guessed to be agy', () => {
  for (const nothing of [undefined, null, '', '   ']) {
    assert.equal(agentIdentity(nothing).label, 'agent');
    assert.equal(agentIdentity(nothing).title, 'agent remote');
  }
});

test('the header does not hardcode one agent name', () => {
  const html = readFileSync(new URL('../../src/agy_remote/static/index.html', import.meta.url), 'utf8');
  const title = html.match(/<div class="app-title">([\s\S]*?)<\/div>/);
  assert.ok(title, 'index.html has no app-title block');
  assert.ok(
    /id="agentName"/.test(title[1]),
    'the app title must carry an element the server-reported agent fills in'
  );
  assert.ok(
    !/>\s*agy\s*</.test(title[1]),
    'the app title must not ship a literal agent name'
  );
});

test('a prompt is never dropped just because the socket is not open', () => {
  const { promptRoute } = sandbox.window.AgyFormat;
  // WebSocket.OPEN === 1. Everything else -- CONNECTING, CLOSING, CLOSED, or
  // no socket at all -- used to mean the prompt was silently discarded.
  assert.equal(promptRoute(1), 'socket');
  for (const state of [0, 2, 3, undefined, null, -1]) {
    assert.equal(promptRoute(state), 'rest');
  }
});
