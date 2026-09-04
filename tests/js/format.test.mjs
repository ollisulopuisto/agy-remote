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

// Objects built inside the vm sandbox carry the sandbox's Object.prototype,
// which deepStrictEqual rejects; a JSON round-trip gives them this realm's.
const inThisRealm = (value) => JSON.parse(JSON.stringify(value));

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

test('file references written as markdown links and bare paths become chips too', () => {
  const { parseFileRefs } = sandbox.window.AgyFormat;
  // Agents answer in markdown: [name](file:///abs/path). That form is the
  // common one in reports, and it used to render as dead text on the phone.
  const linked = parseFileRefs(
    'Implemented [harness/src/project-memory.ts]' +
    '(file:///Users/dst/x/harness/src/project-memory.ts), verified.'
  );
  assert.equal(linked.length, 1);
  assert.equal(linked[0].path, '/Users/dst/x/harness/src/project-memory.ts');
  assert.equal(linked[0].name, 'harness/src/project-memory.ts', 'the link label names the chip');

  // A bare file:///abs/path in prose is a reference as well.
  const bare = parseFileRefs('See file:///Users/dst/x/README.md for details');
  assert.equal(bare.length, 1);
  assert.equal(bare[0].path, '/Users/dst/x/README.md');
  assert.equal(bare[0].name, 'README.md');

  // A label that is itself a file reference falls back to the file name,
  // instead of naming a chip "file:///...".
  const selfLabeled = parseFileRefs('[file:///Users/dst/x/a.ts](file:///Users/dst/x/a.ts)');
  assert.equal(selfLabeled.length, 1);
  assert.equal(selfLabeled[0].name, 'a.ts');

  // The same file through different forms is one chip.
  const mixed = parseFileRefs('[file:///a/b.ts] and [b.ts](file:///a/b.ts)');
  assert.equal(mixed.length, 1);

  // Markdown labels must not swallow prose between two references.
  const two = parseFileRefs(
    '[x.ts](file:///a/x.ts) and then [y.ts](file:///a/y.ts)'
  );
  assert.equal(two.length, 2);
  assert.equal(two[0].path, '/a/x.ts');
  assert.equal(two[1].path, '/a/y.ts');

  // Relative markdown links and file://host links are not host references.
  assert.deepEqual([...parseFileRefs('[readme](docs/README.md)')], []);
  assert.deepEqual([...parseFileRefs('[share](file://host/x.txt)')], []);
});

test('mermaid fences are recognized by their language tag, nothing else', () => {
  const { isMermaidLang } = sandbox.window.AgyFormat;
  // agy writes diagrams as ```mermaid fences; mmd is the classic extension.
  assert.equal(isMermaidLang('mermaid'), true);
  assert.equal(isMermaidLang('mmd'), true);
  assert.equal(isMermaidLang('Mermaid'), true, 'the tag is not case-sensitive');
  // Everything else stays a code block -- a python or bash fence must never
  // be handed to the diagram renderer.
  for (const lang of ['python', 'ts', '', 'mermaid2', 'mmdown']) {
    assert.equal(isMermaidLang(lang), false, lang);
  }
  assert.equal(isMermaidLang(undefined), false);
  assert.equal(isMermaidLang(null), false);
});

test('diagram zoom is clamped, never inverting or exploding', () => {
  const { clampZoom } = sandbox.window.AgyFormat;
  // A pinch multiplies an absolute scale; the result must stay within bounds.
  assert.equal(clampZoom(2, 1, 5), 2);
  assert.equal(clampZoom(0.4, 1, 5), 1, 'never smaller than the natural size');
  assert.equal(clampZoom(12, 1, 5), 5, 'never so large the phone gives up rendering it');
  assert.equal(clampZoom(5, 1, 5), 5);
  assert.equal(clampZoom(1, 1, 5), 1);
  // A lost or corrupt state falls back to natural size, not NaN.
  assert.equal(clampZoom(NaN, 1, 5), 1);
  assert.equal(clampZoom(undefined, 1, 5), 1);
});

test('a second quick tap on a diagram is a double-tap', () => {
  const { isDoubleTap } = sandbox.window.AgyFormat;
  assert.equal(isDoubleTap(1000, 1000 + 200, 300), true);
  assert.equal(isDoubleTap(1000, 1000 + 299, 300), true);
  assert.equal(isDoubleTap(1000, 1000 + 301, 300), false, 'a slow second tap is just a tap');
  assert.equal(isDoubleTap(null, 1000, 300), false, 'no previous tap yet');
  assert.equal(isDoubleTap(undefined, 1000, 300), false);
});

test('mermaid renders from vendored code only, with strict security', () => {
  // The page holds the E2EE key, so the CSP permits no remote script origins;
  // mermaid must be vendored and served same-origin. It parses
  // attacker-influenceable transcript text, so the strict security level (its
  // built-in sanitization) is not optional either.
  const html = readFileSync(new URL('../../src/agy_remote/static/index.html', import.meta.url), 'utf8');
  const vendored = html.indexOf('src="/static/mermaid.min.js"');
  assert.ok(vendored !== -1, 'index.html must load the vendored mermaid bundle');
  const appScript = html.indexOf('src="/static/app.js"');
  assert.ok(vendored < appScript, 'mermaid must load before app.js uses it');
  assert.ok(/<script src="\/static\/mermaid\.min\.js" defer><\/script>/.test(html), 'mermaid loads deferred');

  const app = readFileSync(new URL('../../src/agy_remote/static/app.js', import.meta.url), 'utf8');
  assert.ok(/securityLevel:\s*'strict'/.test(app), 'mermaid must run at securityLevel strict');
  assert.ok(!/https?:\/\/[^"']*(mermaid|cdn)/.test(app), 'no mermaid CDN may be referenced');
});


test('auto-accept answers allow only when enabled and the event is a real approval', () => {
  const { autoAcceptDecision } = sandbox.window.AgyFormat;
  assert.equal(autoAcceptDecision(true, { id: 'ap-1' }), 'allow');
  assert.equal(autoAcceptDecision(false, { id: 'ap-1' }), null, 'the operator must opt in');
  assert.equal(autoAcceptDecision(true, null), null);
  assert.equal(autoAcceptDecision(true, {}), null, 'an event without an id cannot be answered');
});

test('auto-accept never answers a question gate', () => {
  const { autoAcceptDecision } = sandbox.window.AgyFormat;
  // ask_question is not a permission: allowing it answers nothing -- the tool
  // exists to put a question in front of a human, and an auto-allow swallows
  // the only dialogue the agent can ever open.
  assert.equal(autoAcceptDecision(true, { id: 'ap-q', tool_name: 'ask_question' }), null);
  assert.equal(autoAcceptDecision(true, { id: 'ap-q', tool_name: 'run_command' }), 'allow');
});

test('a question gate renders as a question, not as a permission warning', () => {
  const { approvalDisplay } = sandbox.window.AgyFormat;
  // Cross-realm objects (vm sandbox) fail deepStrictEqual's prototype check,
  // so fields are compared directly.
  const display = (app) => {
    const d = approvalDisplay(app);
    return { title: d.title, body: d.body };
  };

  assert.deepEqual(
    display({ tool_name: 'ask_question', args: { question: 'Publish now or benchmark first?' } }),
    { title: 'Agent asks', body: 'Publish now or benchmark first?' },
  );
  // No question field: any string arg carries it, then the bare tool name.
  assert.deepEqual(
    display({ tool_name: 'ask_question', args: 'publish or benchmark?' }),
    { title: 'Agent asks', body: 'publish or benchmark?' },
  );
  assert.deepEqual(display({ tool_name: 'ask_question', args: {} }), {
    title: 'Agent asks',
    body: 'ask_question',
  });

  // An ordinary gate keeps the permission framing and its command text.
  assert.deepEqual(
    display({ tool_name: 'run_command', args: { CommandLine: 'du -hd 1 /tmp' } }),
    { title: 'Permission Required: run_command', body: 'du -hd 1 /tmp' },
  );
  assert.equal(display({ tool_name: 'run_command', args: null }).title, 'Permission Required: run_command');
});

test('an ask_question approval yields its structured questions for the dock', () => {
  const { parseQuestions } = sandbox.window.AgyFormat;
  // The server-decoded shape (normalized by parse_ask_question_args).
  const qs = parseQuestions({
    tool_name: 'ask_question',
    questions: [
      { question: 'How would you like to proceed?', options: ['A', 'B', 'C'], multi_select: false },
      { question: 'Which checks?', options: ['x', 'y'], multi_select: true },
    ],
  });
  assert.equal(qs.length, 2);
  assert.equal(qs[0].question, 'How would you like to proceed?');
  assert.deepEqual(inThisRealm(qs[0].options), ['A', 'B', 'C']);
  assert.equal(qs[0].multiSelect, false);
  assert.equal(qs[1].multiSelect, true);
});

test('raw JSON-string questions from the hook are decoded, and malformed ones yield nothing', () => {
  const { parseQuestions } = sandbox.window.AgyFormat;
  const raw = parseQuestions({
    tool_name: 'ask_question',
    args: { questions: '[{"question":"Q","options":["a","  b "],"is_multi_select":true}]' },
  });
  assert.equal(raw.length, 1);
  assert.equal(raw[0].multiSelect, true);
  assert.deepEqual(inThisRealm(raw[0].options), ['a', 'b']);

  // Nothing question-shaped: the caller falls back to the plain banner.
  for (const app of [
    { tool_name: 'ask_question', args: { questions: 'not json' } },
    { tool_name: 'ask_question', args: { questions: '42' } },
    { tool_name: 'ask_question', args: { questions: '[{"options":[]}]' } },
    { tool_name: 'run_command', args: { CommandLine: 'ls' } },
    { tool_name: 'ask_question' },
    null,
  ]) {
    assert.equal(parseQuestions(app), null, JSON.stringify(app));
  }
});

test('a question banner reads as the question, not a permission', () => {
  const { approvalDisplay } = sandbox.window.AgyFormat;
  const d = approvalDisplay({
    tool_name: 'ask_question',
    questions: [{ question: 'Ship it today?', options: ['Yes', 'No'], multi_select: false }],
  });
  assert.equal(d.title, 'Agent asks');
  assert.equal(d.body, 'Ship it today?');
});

test('credentials are scrubbed from the URL only once the app is installed', () => {
  const { shouldScrubCredentials } = sandbox.window.AgyFormat;
  // In the tab the URL is the only thing iOS Add to Home Screen reliably
  // captures, so it must keep the pairing; the installed app gets its own
  // storage container and must not keep secrets in its launch URL.
  assert.equal(shouldScrubCredentials(false), false);
  assert.equal(shouldScrubCredentials(true), true);
});

test('HUD formatters handle context percentage, token counts, and severity', () => {
  const { formatContextPercent, formatTokenCount, contextSeverity } = sandbox.window.AgyFormat;

  assert.equal(formatContextPercent(12), '12%');
  assert.equal(formatContextPercent(12.4), '12.4%');
  assert.equal(formatContextPercent(null), null);

  assert.equal(formatTokenCount(850), '850');
  assert.equal(formatTokenCount(24500), '24.5k');
  assert.equal(formatTokenCount(200000), '200k');
  assert.equal(formatTokenCount(1500000), '1.5M');
  assert.equal(formatTokenCount(null), null);

  assert.equal(contextSeverity(20), 'normal');
  assert.equal(contextSeverity(65), 'warning');
  assert.equal(contextSeverity(75), 'warning');
  assert.equal(contextSeverity(88), 'critical');
  assert.equal(contextSeverity(null), 'normal');
});

