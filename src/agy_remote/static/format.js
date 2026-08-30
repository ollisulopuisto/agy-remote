// Pure formatting helpers for the transcript view.
//
// A tool call on the desktop is one line -- `Bash(git status)` -- with its
// arguments and output behind an expand. The phone was rendering every
// argument and every byte of output inline, so a single `du` buried the
// conversation. These functions produce the one-line form; app.js hangs the
// full detail behind a <details> element.
//
// Classic script, not a module: the PWA loads no bundler and no third-party
// code. Attached to window so the page and the tests can both reach it.
(function (global) {
  'use strict';

  // The argument worth showing for the tools agy actually calls, in the order
  // we would rather have them. An unlisted tool falls back to its first text
  // argument, which is usually the interesting one.
  var PRIMARY_KEYS = [
    'CommandLine',
    'Query',
    'TargetFile',
    'AbsolutePath',
    'DirectoryPath',
    'SearchPath',
    'Uri',
    'Url',
    'toolSummary',
    'toolAction'
  ];

  var ARG_LIMIT = 60;
  var LINE_LIMIT = 80;

  function truncate(text, limit) {
    var value = String(text == null ? '' : text).trim();
    return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
  }

  function firstLine(text) {
    var value = String(text == null ? '' : text).trim();
    if (!value) return '';
    return truncate(value.split('\n')[0], LINE_LIMIT);
  }

  // `run_command` plus a pile of arguments, reduced to `run_command(du -hd 1)`.
  function toolSummary(name, args) {
    var values = args || {};
    var primary = null;

    for (var i = 0; i < PRIMARY_KEYS.length; i++) {
      var candidate = values[PRIMARY_KEYS[i]];
      if (typeof candidate === 'string' && candidate.trim()) {
        primary = candidate;
        break;
      }
    }

    if (primary === null) {
      var keys = Object.keys(values);
      for (var j = 0; j < keys.length; j++) {
        var value = values[keys[j]];
        if (typeof value === 'string' && value.trim()) {
          primary = value;
          break;
        }
      }
    }

    if (primary === null) return String(name || 'tool_call');
    return String(name || 'tool_call') + '(' + truncate(primary.replace(/\s+/g, ' '), ARG_LIMIT) + ')';
  }

  // Tool output is the bulkiest thing in a transcript and the least often
  // wanted: a line count is enough to decide whether to open it.
  function outputSummary(text) {
    var value = String(text == null ? '' : text).replace(/\s+$/, '');
    if (!value.trim()) return 'output';

    var lines = value.split('\n');
    if (lines.length === 1) return truncate(lines[0], LINE_LIMIT);
    return 'output · ' + lines.length + ' lines';
  }

  // Whether the full text is worth an expand at all.
  function isCollapsible(text) {
    var value = String(text == null ? '' : text).replace(/\s+$/, '');
    return value.split('\n').length > 1 || value.length > LINE_LIMIT;
  }

  // agy opens every session with a checkpoint that reads like prior history, so
  // a session has to say plainly which one it is and when it began.
  function sessionLabel(conversation, timeText) {
    if (!conversation || !conversation.id) return '';

    var parts = ['Session ' + String(conversation.id).slice(0, 8)];
    if (timeText) parts.push('started ' + timeText);

    var steps = conversation.step_count;
    if (typeof steps === 'number' && steps > 0) {
      parts.push(steps + (steps === 1 ? ' step' : ' steps'));
    }
    return parts.join(' · ');
  }

  // Which agent is behind this server. The name was hardcoded in the header,
  // so a session was labelled by whatever the markup happened to say -- and
  // since every backend normalizes to the same step vocabulary, nothing else
  // on screen corrected it. An unreported agent is "agent", never a guess.
  function agentIdentity(agent) {
    var name = String(agent == null ? '' : agent).trim() || 'agent';
    return { label: name, title: name + ' remote' };
  }

  // Where a prompt should go. A socket that is connecting, closing, closed or
  // gone used to mean the prompt was written nowhere and the input cleared
  // anyway -- the text simply vanished. REST reaches the same server.
  function promptRoute(readyState) {
    return readyState === 1 ? 'socket' : 'rest';
  }

  // Whether a socket has stopped answering. iOS suspends a backgrounded PWA
  // and the socket comes back reading OPEN with nothing underneath -- writes
  // succeed into nowhere and `onclose` may never fire. A pong that stopped
  // coming back is the only sign, so nothing is judged stale until one has
  // been seen at all.
  function socketIsStale(lastPongAt, now, timeoutMs) {
    if (!lastPongAt) return false;
    return now - lastPongAt > timeoutMs;
  }

  // Where a revised step goes. A backend may create a message empty and fill
  // it in, so the text, the tool calls and the thinking arrive as revisions of
  // a step already on screen -- an update that is not applied is the whole
  // answer missing until something reloads the transcript.
  function applyStepUpdate(steps, step) {
    var next = steps.slice();
    var index = -1;
    if (step && step.id) {
      for (var i = 0; i < next.length; i++) {
        if (next[i] && next[i].id === step.id) { index = i; break; }
      }
    }
    if (index === -1) {
      index = next.length;
      next.push(step);
    } else {
      next[index] = step;
    }
    return { steps: next, index: index };
  }

  // The session a client should call its own. A phone that connected before
  // any session existed held `null` and never recovered: its prompts went out
  // unaddressed, leaving the server's own idea of "active" to decide where
  // they landed. One it already knows is never overwritten from outside.
  function adoptConversationId(current, fromEvent) {
    return current ? current : (fromEvent || null);
  }

  // Whether anything actually took the prompt. The agy backend answers
  // "broadcast" when no supervisor was there to type it -- the prompt went
  // nowhere, and a client that only hears `prompt_sent` cannot tell. A server
  // too old to say anything is taken at its word.
  function promptWasDelivered(deliveredVia) {
    return deliveredVia === undefined || deliveredVia === null || deliveredVia !== 'broadcast';
  }

  // What to say about how many devices are connected. Access to this server is
  // all-or-nothing -- every client holds the same token, none can be revoked
  // alone -- so a connection you did not expect is the only sign the pairing
  // URL has escaped. One device is the ordinary case and says nothing.
  function peerNotice(count) {
    return typeof count === 'number' && count > 1 ? count + ' devices connected' : null;
  }

  // Which session an approval came from, when that is not the one on screen.
  // Hiding another session's banner was the fix for an unattributed one -- a
  // bare `bash` request drawn into the transcript in front of you reads as
  // belonging to it -- but hiding it also meant nobody could answer, and the
  // agent that asked sat blocked. Naming it does both jobs.
  function approvalOrigin(approval, currentConversationId) {
    if (!approval) return null;
    var id = approval.conversation_id;
    if (!id || id === currentConversationId) return null;
    return approval.conversation_title || String(id).slice(0, 8);
  }

  // Approvals belong to the session that raised them, not to whatever
  // transcript happens to be open. Drawing another session's banner here would
  // read as belonging to the work in front of you; counting it per session
  // says where to look instead.
  function approvalsForSession(approvals, conversationId) {
    return (approvals || []).filter(function (a) {
      return a && a.conversation_id === conversationId;
    });
  }

  function approvalCountsBySession(approvals) {
    var counts = {};
    (approvals || []).forEach(function (a) {
      if (!a || !a.conversation_id) return;
      counts[a.conversation_id] = (counts[a.conversation_id] || 0) + 1;
    });
    return counts;
  }

  // What the one "look elsewhere" badge should say: a name when a single other
  // session is waiting, a count of sessions when several are.
  function approvalsElsewhere(approvals, conversationId) {
    var other = (approvals || []).filter(function (a) {
      return a && a.conversation_id && a.conversation_id !== conversationId;
    });
    var sessions = [];
    other.forEach(function (a) {
      if (sessions.indexOf(a.conversation_id) === -1) sessions.push(a.conversation_id);
    });

    var label = null;
    if (sessions.length === 1) {
      var named = other.find(function (a) {
        return a.conversation_title;
      });
      label = named ? named.conversation_title : String(sessions[0]).slice(0, 8);
    } else if (sessions.length > 1) {
      label = sessions.length + ' sessions';
    }
    return { count: other.length, sessions: sessions, label: label };
  }

  // The manifest an installed app should launch from, built from credentials
  // the client already holds. Served from the server, this JSON put the E2EE
  // key into an HTTP response body -- the one place it must never be: the key
  // rides in the QR fragment precisely because fragments never traverse the
  // wire, and on a plaintext LAN hop the payload layer it keys is the only
  // protection. Built here and handed over as a data: URI, it never leaves
  // the device. Null when either credential is missing: an icon that launches
  // half-paired is worse than the anonymous fallback.
  function pairedManifest(baseManifest, token, e2eeKey) {
    if (!token || !e2eeKey) return null;
    var m = {};
    for (var k in baseManifest) m[k] = baseManifest[k];
    m.start_url = '/?token=' + encodeURIComponent(token) + '#key=' + e2eeKey;
    return JSON.stringify(m);
  }

  // Agent-to-agent mailbox envelope parser (W2, item 2.3).
  // When an agent sends a message via agy-msg, the server wraps it in
  // `[message from <from>: <text>]`. The phone recognizes this synthetic prompt
  // and renders it distinctly from a human prompt.
  function parseEnvelope(text) {
    var raw = String(text == null ? '' : text).trim();
    var match = raw.match(/^\[message from ([A-Za-z0-9_-]+):\s*([\s\S]*)\]$/);
    if (!match) return { isEnvelope: false, from: null, text: raw };
    return {
      isEnvelope: true,
      from: match[1],
      text: match[2].trim()
    };
  }

  // The mailbox pairs one session is part of, addressed as peers.
  function trafficForSession(pairs, sessionKey) {
    if (!pairs || !Array.isArray(pairs) || !sessionKey) return [];
    return pairs.filter(function (p) {
      return p && (p.a === sessionKey || p.b === sessionKey);
    });
  }

  // Format a single mailbox traffic pair into a display pill for the session row.
  function formatTrafficPill(pair, currentSessionKey) {
    if (!pair) return null;
    var peer = pair.a === currentSessionKey ? pair.b : (pair.b === currentSessionKey ? pair.a : pair.a + '↔' + pair.b);
    var count = typeof pair.count === 'number' ? pair.count : 0;
    var state = pair.muted ? 'muted' : (pair.looping ? 'looping' : 'active');
    return {
      peer: peer,
      count: count,
      state: state,
      muted: !!pair.muted,
      looping: !!pair.looping,
      label: '⇄ ' + peer + ' ×' + count
    };
  }

  global.AgyFormat = {
    pairedManifest: pairedManifest,
    approvalsForSession: approvalsForSession,
    approvalCountsBySession: approvalCountsBySession,
    approvalsElsewhere: approvalsElsewhere,
    approvalOrigin: approvalOrigin,
    peerNotice: peerNotice,
    promptWasDelivered: promptWasDelivered,
    adoptConversationId: adoptConversationId,
    applyStepUpdate: applyStepUpdate,
    socketIsStale: socketIsStale,
    promptRoute: promptRoute,
    agentIdentity: agentIdentity,
    sessionLabel: sessionLabel,
    toolSummary: toolSummary,
    outputSummary: outputSummary,
    isCollapsible: isCollapsible,
    firstLine: firstLine,
    parseEnvelope: parseEnvelope,
    trafficForSession: trafficForSession,
    formatTrafficPill: formatTrafficPill
  };
})(typeof window !== 'undefined' ? window : this);

