// Pins the transcript selection-hold mechanism in app.js: while the reader
// drags a live text selection across the transcript, streaming step redraws
// must be held (not applied to the DOM, which would throw the selection away)
// and flushed the moment the selection is released, newest content winning.
// Run with: node --test tests/js

import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/app.js', import.meta.url), 'utf8');

test('the redraw-hold mechanism exists and is wired to selectionchange', () => {
  assert.match(source, /function selectionHoldsTranscript\(\)/, 'selection gate must exist');
  assert.match(source, /document\.addEventListener\('selectionchange', flushHeldSteps\)/,
    'held redraws must flush when the selection changes');
  assert.match(source, /if \(selectionHoldsTranscript\(\)\) \{\s*\n\s*heldStepUpdates/, 
    'replaceStep must consult the selection gate before touching the DOM');
});

// Slice the contiguous mechanism block (heldStepUpdates .. applyReplaceStep)
// out of app.js and run it against a minimal DOM harness.
const block = source.slice(
  source.indexOf('let heldStepUpdates = null;'),
  source.indexOf('function appendStep'),
);
assert.ok(block.includes('function applyReplaceStep'), 'mechanism block must include applyReplaceStep');

function makeEl() {
  return {
    addEventListener() {}, removeEventListener() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    dataset: {}, style: {},
    children: [], appended: [], inserted: [], removed: [],
    appendChild(node) { this.appended.push(node); this.children.push(node); },
    insertBefore(node, ref) { this.inserted.push(node); this.children.unshift(node); },
    removeChild(node) { this.removed.push(node); },
    remove() {},
    contains() { return false; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    closest() { return null; }, focus() {}, click() {},
    textContent: '', innerHTML: '', value: '',
  };
}

function makeHarness({ insideSelection }) {
  const chatContainer = makeEl();
  chatContainer.contains = () => insideSelection;
  const listeners = {};
  const selection = { rangeCount: 1, isCollapsed: false, anchorNode: {}, focusNode: {} };
  const document = {
    getElementById: () => makeEl(),
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    createElement: () => makeEl(),
    createDocumentFragment: () => makeEl(),
    querySelector: () => null, querySelectorAll: () => [],
    body: makeEl(),
  };
  const window = {
    getSelection: () => selection,
    addEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    visualViewport: { addEventListener() {} },
    location: { href: 'http://localhost/', search: '' },
    navigator: { clipboard: { writeText: async () => {} } },
    requestAnimationFrame() {},
  };
  window.window = window;
  const context = {
    window, document, chatContainer,
    navigator: window.navigator, location: window.location,
    requestAnimationFrame: window.requestAnimationFrame,
    setTimeout, clearTimeout, setInterval, clearInterval,
    console,
    // stepNodes lives elsewhere in app.js; the mechanism only receives its output.
    stepNodes: (step) => ({ dataset: { stepId: step.id }, tag: `nodes-${step.id}-${step.content}` }),
    applyReplaceStep: undefined,
  };
  vm.createContext(context);
  vm.runInContext(block, context);
  return {
    context,
    flush: () => (listeners.selectionchange || []).forEach((fn) => fn()),
    setSelection: (collapsed, inside) => {
      selection.isCollapsed = collapsed;
      chatContainer.contains = () => inside;
    },
  };
}

test('a live selection holds streaming redraws', () => {
  const h = makeHarness({ insideSelection: true });
  h.context.replaceStep({ id: 's1', content: 'partial' });
  assert.equal(h.context.chatContainer.appended.length, 0, 'no redraw may touch the DOM mid-selection');
  assert.equal(h.context.chatContainer.inserted.length, 0, 'no redraw may touch the DOM mid-selection');
});

test('releasing the selection flushes held redraws with the newest content', () => {
  const h = makeHarness({ insideSelection: true });
  h.context.replaceStep({ id: 's1', content: 'older' });
  h.context.replaceStep({ id: 's1', content: 'newer' });
  h.setSelection(true, false);
  h.flush();
  const applied = h.context.chatContainer.appended.concat(h.context.chatContainer.inserted);
  assert.equal(applied.length, 1, 'held redraws coalesce per step id');
  assert.equal(applied[0].tag, 'nodes-s1-newer', 'the newest content of each held step wins');
});

test('a collapsed selection applies redraws immediately', () => {
  const h = makeHarness({ insideSelection: false });
  h.context.replaceStep({ id: 's1', content: 'streaming' });
  assert.equal(h.context.chatContainer.appended.length, 1, 'idle readers must see streaming updates at once');
});

test('a selection outside the transcript does not hold redraws', () => {
  const h = makeHarness({ insideSelection: false });
  h.setSelection(false, false);
  h.context.replaceStep({ id: 's1', content: 'streaming' });
  assert.equal(h.context.chatContainer.appended.length, 1);
});
