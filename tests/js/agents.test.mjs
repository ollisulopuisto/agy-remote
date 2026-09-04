import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/agents.js', import.meta.url), 'utf8');
const sandbox = { window: {}, globalThis: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const AgentHelpers = sandbox.window.AgentHelpers || sandbox.globalThis.AgentHelpers;

test('sortAgents places needs_attention and running before completed and failed', () => {
  const agents = [
    { agent_id: '1', status: 'completed', started_at: '2026-09-01T10:00:00Z' },
    { agent_id: '2', status: 'running', started_at: '2026-09-01T10:05:00Z' },
    { agent_id: '3', status: 'needs_attention', started_at: '2026-09-01T10:10:00Z' },
    { agent_id: '4', status: 'failed', started_at: '2026-09-01T09:00:00Z' },
  ];

  const sorted = AgentHelpers.sortAgents(agents);
  assert.equal(sorted[0].agent_id, '3'); // needs_attention
  assert.equal(sorted[1].agent_id, '2'); // running
  assert.equal(sorted[2].agent_id, '1'); // completed
  assert.equal(sorted[3].agent_id, '4'); // failed
});

test('groupAgentsByStatus partitions into four dashboard buckets', () => {
  const agents = [
    { agent_id: '1', status: 'running' },
    { agent_id: '2', status: 'needs_attention' },
    { agent_id: '3', status: 'completed' },
    { agent_id: '4', status: 'failed' },
    { agent_id: '5', status: 'cancelled' },
  ];

  const groups = AgentHelpers.groupAgentsByStatus(agents);
  assert.equal(groups.running.length, 1);
  assert.equal(groups.needs_attention.length, 1);
  assert.equal(groups.completed.length, 1);
  assert.equal(groups.failed.length, 2); // failed + cancelled
});

test('formatAgentStatus provides distinct icons and labels', () => {
  const r = AgentHelpers.formatAgentStatus('running');
  assert.equal(r.icon, '●');
  assert.equal(r.label, 'Running');

  const a = AgentHelpers.formatAgentStatus('needs_attention');
  assert.equal(a.icon, '!');
  assert.equal(a.label, 'Needs attention');

  const c = AgentHelpers.formatAgentStatus('completed');
  assert.equal(c.icon, '✓');
  assert.equal(c.label, 'Completed');

  const f = AgentHelpers.formatAgentStatus('failed');
  assert.equal(f.icon, '✕');
  assert.equal(f.label, 'Failed');
});

test('formatProviderBadge maps providers cleanly', () => {
  assert.equal(AgentHelpers.formatProviderBadge('gemini').name, 'Gemini');
  assert.equal(AgentHelpers.formatProviderBadge('claude').name, 'Claude');
  assert.equal(AgentHelpers.formatProviderBadge('antigravity').name, 'Antigravity');
});

test('formatElapsedTime calculates minutes and hours', () => {
  const now = 1000000000;
  assert.equal(AgentHelpers.formatElapsedTime(now - 14 * 60 * 1000, now), '14m');
  assert.equal(AgentHelpers.formatElapsedTime(now - 80 * 60 * 1000, now), '1h 20m');
});

test('buildHandoffPayload constructs delegate request from prior result', () => {
  const job = {
    project: 'transposer',
    provider: 'gemini',
    current_task: 'Fix SQLite persistence',
    result: 'Azure persistence bug diagnosed in db.py:L45',
    commit: '7b8c9d0',
    remaining_issues: ['Write migration test'],
  };

  const handoff = AgentHelpers.buildHandoffPayload(job, 'claude');
  assert.equal(handoff.project, 'transposer');
  assert.equal(handoff.provider, 'claude');
  assert.ok(handoff.task.includes('Azure persistence bug diagnosed'));
  assert.ok(handoff.context.includes('Prior task: Fix SQLite persistence'));
  assert.ok(handoff.context.includes('Commit: 7b8c9d0'));
  assert.ok(handoff.context.includes('Remaining issues: Write migration test'));
});
