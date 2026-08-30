// Notification sounds for the PWA, synthesized with WebAudio.
//
// opencode's PWA plays bundled audio alerts; agy-remote ships no binary
// assets and loads no third-party scripts, so the same affordance is built
// from tiny note sequences played by the browser's own oscillator. The data
// half (window.AgySound.notes / enabled / setEnabled) is pure and testable
// headlessly; play degrades to a no-op without an AudioContext (node tests,
// old WebViews), because a missing sound must never break an approval.
(function (global) {
  'use strict';

  // Two rising tones say "something needs you"; falling says "done"; a low
  // buzz says "something went wrong". Short by design: these play on the
  // lock screen's behalf, not over it.
  var SEQUENCES = {
    approval: [
      { freq: 880, ms: 90 },
      { freq: 1318.5, ms: 140 }
    ],
    complete: [
      { freq: 1318.5, ms: 90 },
      { freq: 987.8, ms: 150 }
    ],
    error: [{ freq: 233.1, ms: 180 }]
  };
  var STORE_KEY = 'agy-sounds';

  function notes(name) {
    var seq = SEQUENCES[name];
    if (!seq) return null;
    return seq.map(function (n) {
      return { freq: n.freq, ms: n.ms };
    });
  }

  function store() {
    try {
      return global.localStorage || null;
    } catch (e) {
      return null;
    }
  }

  // Defaults to on, like opencode's; the bell menu's toggle persists '0'.
  function enabled() {
    var s = store();
    if (!s) return true;
    try {
      return s.getItem(STORE_KEY) !== '0';
    } catch (e) {
      return true;
    }
  }

  function setEnabled(on) {
    var s = store();
    if (!s) return;
    try {
      s.setItem(STORE_KEY, on ? '1' : '0');
    } catch (e) {
      /* this load only */
    }
  }

  var ctx = null;
  function audioContext() {
    var Ctor = global.AudioContext || global.webkitAudioContext;
    if (!Ctor) return null;
    if (!ctx) {
      try {
        ctx = new Ctor();
      } catch (e) {
        return null;
      }
    }
    return ctx;
  }

  function play(name) {
    if (!enabled()) return;
    var seq = notes(name);
    if (!seq) return;
    var ac = audioContext();
    if (!ac) return;
    var t = ac.currentTime;
    for (var i = 0; i < seq.length; i++) {
      var osc = ac.createOscillator();
      var gain = ac.createGain();
      osc.type = 'sine';
      osc.frequency.value = seq[i].freq;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.12, t + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + seq[i].ms / 1000);
      osc.connect(gain).connect(ac.destination);
      osc.start(t);
      osc.stop(t + seq[i].ms / 1000 + 0.02);
      t += seq[i].ms / 1000;
    }
  }

  global.AgySound = { notes: notes, enabled: enabled, setEnabled: setEnabled, play: play };
})(typeof window !== 'undefined' ? window : this);
