// Tests for the PWA's notification sounds. Run with: node --test tests/js
//
// opencode's PWA plays bundled alert sounds; agy-remote ships none, on an
// air-gapped tailnet there is no CDN to load them from, and binary assets
// would bloat the package. The tones are synthesized from small note
// sequences instead: pure data here, played by WebAudio in the browser.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../../src/agy_remote/static/sounds.js', import.meta.url), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const AgySound = sandbox.window.AgySound;

test('every named event has a note sequence', () => {
  for (const name of ['approval', 'complete', 'error']) {
    const notes = AgySound.notes(name);
    assert.ok(Array.isArray(notes) && notes.length > 0, name);
    for (const note of notes) {
      assert.ok(typeof note.freq === 'number' && note.freq > 0, name + ' freq');
      assert.ok(typeof note.ms === 'number' && note.ms > 0, name + ' ms');
    }
  }
});

test('an unknown event has no sequence, so nothing plays', () => {
  assert.equal(AgySound.notes('mystery'), null);
});

test('the enabled switch persists and defaults to on', () => {
  const store = {};
  const fakeWindow = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => {
        store[k] = String(v);
      },
    },
  };
  const fresh = { window: fakeWindow };
  vm.createContext(fresh);
  vm.runInContext(source, fresh);
  const sound = fresh.window.AgySound;

  assert.equal(sound.enabled(), true, 'default is on');
  sound.setEnabled(false);
  assert.equal(sound.enabled(), false, 'persisted off');
  sound.setEnabled(true);
  assert.equal(sound.enabled(), true, 'persisted on');
});

test('play with no AudioContext is a silent no-op, not a crash', () => {
  assert.doesNotThrow(() => AgySound.play('approval'));
});
