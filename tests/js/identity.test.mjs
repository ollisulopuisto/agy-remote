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

test('a socket that stopped answering is treated as dead', () => {
  const { socketIsStale } = sandbox.window.AgyFormat;
  // iOS suspends a backgrounded PWA: on resume the socket still reads OPEN
  // while the connection underneath is gone, and `onclose` may never fire.
  // Only a pong that stopped coming back reveals it.
  assert.equal(socketIsStale(1000, 1000 + 5000, 20000), false);
  assert.equal(socketIsStale(1000, 1000 + 19999, 20000), false);
  assert.equal(socketIsStale(1000, 1000 + 20001, 20000), true);
  // Never seen a pong at all: nothing to judge it stale by yet.
  assert.equal(socketIsStale(null, 999999, 20000), false);
});

test('a revised step replaces the one already on screen', () => {
  const { applyStepUpdate } = sandbox.window.AgyFormat;
  // opencode creates an assistant message empty and fills it in: the text, the
  // tool calls and the thinking all arrive as revisions of the same step. The
  // PWA only ever handled `step_added`, so everything after the empty first
  // frame was dropped and the answer appeared only on a session switch.
  const steps = [{ id: 'msg_1', content: 'hi' }, { id: 'msg_2', content: '' }];

  const revised = applyStepUpdate(steps, { id: 'msg_2', content: 'the answer' });
  assert.equal(revised.index, 1);
  assert.equal(revised.steps.length, 2);
  assert.equal(revised.steps[1].content, 'the answer');
  assert.equal(revised.steps[0].content, 'hi');

  // A step nobody has seen yet belongs at the end, not nowhere.
  const fresh = applyStepUpdate(steps, { id: 'msg_3', content: 'new' });
  assert.equal(fresh.index, 2);
  assert.equal(fresh.steps.length, 3);

  // No id to match on: appended rather than silently swallowed.
  const anon = applyStepUpdate(steps, { content: 'unidentified' });
  assert.equal(anon.index, 2);
  assert.equal(anon.steps.length, 3);
});

test('the phone adopts a session id when it has none', () => {
  const { adoptConversationId } = sandbox.window.AgyFormat;
  // A phone that connected before any session existed had `null` and never
  // recovered, so its prompts went unaddressed and the server's own idea of
  // "active" decided where they landed -- possibly a session not on screen.
  assert.equal(adoptConversationId(null, 'ses_from_event'), 'ses_from_event');
  assert.equal(adoptConversationId(undefined, 'ses_from_event'), 'ses_from_event');
  assert.equal(adoptConversationId('', 'ses_from_event'), 'ses_from_event');
  // One it already knows is never overwritten by traffic from elsewhere.
  assert.equal(adoptConversationId('ses_mine', 'ses_other'), 'ses_mine');
  assert.equal(adoptConversationId(null, null), null);
});
