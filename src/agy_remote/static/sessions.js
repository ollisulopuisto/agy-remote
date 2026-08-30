// Pure helpers for starting a session from the phone (W1, item 1.3).
//
// The sheet collects a repo, a branch, a task and a name; the server does
// the cloning and the spawning. These functions keep the decisions that
// belong on the device -- what to send, what the chip says, whose spawn an
// event belongs to -- away from the DOM, so the tests can reach them.
//
// Classic script, not a module: the PWA loads no bundler and no third-party
// code. Attached to window so the page and the tests can both reach it.
(function (global) {
  'use strict';

  // The sheet's fields become the /api/sessions body. The server re-checks
  // everything -- this is the fast, kind refusal for the one field the form
  // must not send empty, and the trimming so a stray space never becomes part
  // of a git URL. Blank optionals are omitted: the server derives a name from
  // the repo when none is given, and an empty branch is no branch.
  function buildSpawnRequest(fields) {
    var f = fields || {};
    var repoUrl = String(f.repoUrl == null ? '' : f.repoUrl).trim();
    if (!repoUrl) {
      return { ok: false, error: 'A repo URL is required to start a session.' };
    }

    var payload = { repo_url: repoUrl };
    var branch = String(f.branch == null ? '' : f.branch).trim();
    var task = String(f.task == null ? '' : f.task).trim();
    var name = String(f.name == null ? '' : f.name).trim();
    if (branch) payload.branch = branch;
    if (task) payload.task = task;
    if (name) payload.name = name;
    return { ok: true, payload: payload };
  }

  // What the sheet's progress chip says for a stage. The server walks
  // cloning -> starting and then reports session_created; a failure arrives
  // as a `failed` stage carrying git's own stderr, which is the most useful
  // thing the phone can show, so it wins over any fixed wording.
  function spawnStageLabel(stage, error) {
    if (stage === 'cloning') return 'Cloning…';
    if (stage === 'starting') return 'Starting…';
    if (stage === 'failed') return String(error || 'The server refused the spawn.');
    return null;
  }

  // The 202 answer names the spawn: its tmux session, its workdir, its name.
  // Every later event carries the same fields, so a sheet only reacts to
  // events for the spawn it sent -- two phones on one server would otherwise
  // drive each other's sheets.
  function spawnEventMatches(eventData, handle) {
    if (!eventData || !handle) return false;
    if (eventData.tmux_session && eventData.tmux_session === handle.tmux_session) return true;
    if (eventData.workdir && eventData.workdir === handle.workdir) return true;
    if (eventData.name && eventData.name === handle.name) return true;
    return false;
  }

  // The one rule the drawer's dot and the Stop button share (W1, item 1.4).
  // A pending tool gate is red and wins over everything. A transcript update
  // inside the busy window means the agent is mid-turn: yellow, even on the
  // session on screen. Beyond the window, or with no activity at all, only
  // "is this the one on screen" decides: active or idle. The window is a
  // parameter so tests can tighten it.
  function sessionStatus(state) {
    var s = state || {};
    if (s.pending > 0) return 'approval';
    var windowMs = s.busyWindowMs != null ? s.busyWindowMs : 30000;
    if (s.lastActivityAt != null && s.now - s.lastActivityAt < windowMs) return 'busy';
    return s.isActive ? 'active' : 'idle';
  }

  // The same rule applied to one drawer row: the server sends an ISO
  // `updated_at` (the transcript's last write) and the row knows whether it is
  // the session on screen. `liveActivityMs` is a second source: a step this
  // client has seen since the snapshot, and the fresher of the two wins.
  // Anything that is not a time the Date constructor accepts is treated as no
  // activity at all, not as a time.
  function sessionStatusOf(conversation, currentId, pending, now, liveActivityMs) {
    var c = conversation || {};
    var updatedAt = null;
    if (c.updated_at) {
      var t = new Date(c.updated_at).getTime();
      if (!isNaN(t)) updatedAt = t;
    }
    if (liveActivityMs != null && (!updatedAt || liveActivityMs > updatedAt)) updatedAt = liveActivityMs;
    return sessionStatus({
      pending: pending > 0 ? pending : 0,
      lastActivityAt: updatedAt,
      isActive: !!(c.id && currentId && c.id === currentId),
      now: now
    });
  }

  global.AgySessions = {
    buildSpawnRequest: buildSpawnRequest,
    spawnStageLabel: spawnStageLabel,
    spawnEventMatches: spawnEventMatches,
    sessionStatus: sessionStatus,
    sessionStatusOf: sessionStatusOf
  };
})(typeof window !== 'undefined' ? window : this);