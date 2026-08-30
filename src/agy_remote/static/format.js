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
  function pairedManifest(baseManifest, token, e2eeKey, origin) {
    if (!token || !e2eeKey) return null;
    var m = {};
    for (var k in baseManifest) m[k] = baseManifest[k];
    var start = '/?token=' + encodeURIComponent(token) + '#key=' + e2eeKey;
    // Absolute when the origin is known. A data: manifest has no URL a
    // relative start_url could resolve against, so a relative one is invalid
    // per spec and engines fall back to the bare page -- the unpaired launch.
    if (origin) start = origin + start;
    m.start_url = start;
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

  // The transcript names host files in three shapes, depending on how the
  // agent was feeling: a markdown link [name](file:///abs/path), the
  // bracketed shorthand [file:///abs/path], or a bare file:///abs/path in
  // prose. The server holds those bytes and serves them (within its
  // sanctioned roots), so the phone can offer them as tappable chips; this
  // only has to find the references. Only the empty-host form is a reference
  // to this machine; `file://host/path` and relative paths never become
  // chips.
  //
  // Mirrored from opencode's packages/opencode/src/util/remote-pwa.ts
  // (parseFileRefs) -- keep the two in sync. The bracketed form here never
  // lets the bare-path scan swallow the closing bracket, which the original
  // does; its tests did not cover that shape.
  function scanFileRefs(value) {
    var found = [];
    function collect(re, pathGroup, labelGroup) {
      var m;
      re.lastIndex = 0;
      while ((m = re.exec(value)) !== null) {
        found.push({ index: m.index, raw: m[0], path: m[pathGroup], label: labelGroup ? m[labelGroup] : null });
        if (m.index === re.lastIndex) re.lastIndex += 1;
      }
    }
    collect(/\[([^\]\n]+)\]\(file:\/\/\/([^)\s]+)\)/g, 2, 1);
    collect(/\[file:\/\/(\/[^\]\n]+)\]/g, 1, null);
    collect(/file:\/\/\/([^\s\)\]"'<>]+)/g, 1, null);
    // The markdown and bare scans consume all three slashes of `file:///`,
    // so their capture is missing the leading slash of the path; the
    // bracketed scan keeps it. Normalize before anything else looks.
    for (var j = 0; j < found.length; j++) {
      if (found[j].path.charAt(0) !== '/') found[j].path = '/' + found[j].path;
    }
    found.sort(function (a, b) { return a.index - b.index; });
    return found;
  }

  function refName(hit, path) {
    var base = path.slice(path.lastIndexOf('/') + 1) || path;
    if (hit.label && !/^file:\/\//.test(hit.label)) {
      return hit.label.replace(/\s+$/, '');
    }
    return base;
  }

  function parseFileRefs(text) {
    var value = String(text == null ? '' : text);
    var refs = [];
    var seen = {};
    var cursor = 0;
    var scanned = scanFileRefs(value);
    for (var i = 0; i < scanned.length; i++) {
      var hit = scanned[i];
      // A raw earlier in the document already swallowed this match -- the
      // bare-path scan runs inside every bracketed reference, and without
      // this guard a path with spaces yields a truncated ghost ref.
      if (hit.index < cursor) continue;
      cursor = hit.index + hit.raw.length;
      var path = hit.path.replace(/\s+$/, '');
      if (seen[path]) continue;
      seen[path] = true;
      refs.push({
        raw: hit.raw,
        path: path,
        name: refName(hit, path)
      });
    }
    return refs;
  }

  // The rendering twin of parseFileRefs: every reference is swapped for
  // render(ref), spliced by hand so no raw match is ever left beside its
  // chip -- and no chip is emitted twice for one file. The markdown renderer
  // hands this escaped text; paths are unescaped by the caller.
  function replaceFileRefs(text, render) {
    var value = String(text == null ? '' : text);
    var scanned = scanFileRefs(value);
    if (scanned.length === 0) return value;
    var out = '';
    var cursor = 0;
    var seen = {};
    for (var i = 0; i < scanned.length; i++) {
      var hit = scanned[i];
      // A raw earlier in the document already swallowed this match (the
      // label branch of a nested reference); it is spent either way.
      if (hit.index < cursor) continue;
      var path = hit.path.replace(/\s+$/, '');
      if (seen[path]) continue;
      seen[path] = true;
      out += value.slice(cursor, hit.index);
      out += render({ raw: hit.raw, path: path, name: refName(hit, path) });
      cursor = hit.index + hit.raw.length;
    }
    out += value.slice(cursor);
    return out;
  }

  // Whether a fenced code block's language tag marks a mermaid diagram.
  // agy writes them as ```mermaid; mmd is the classic file extension. Anything
  // else -- including lookalikes like mermaid2 -- stays a plain code block.
  function isMermaidLang(lang) {
    return /^(mermaid|mmd)$/i.test(String(lang == null ? '' : lang).trim());
  }

  // The auto-accept toggle's decision: 'allow' when the operator turned the
  // switch on and the event really is an approval an id can answer, null
  // otherwise -- the caller then falls through to the ordinary banner. Kept
  // pure so the moment it fires is pinned by tests, not by vibes.
  function autoAcceptDecision(enabled, approval) {
    if (!enabled) return null;
    if (!approval || !approval.id) return null;
    // A question gate is not a permission. ask_question exists to put a
    // question in front of a human; an auto-allow swallows the only dialogue
    // the agent can ever open, and the session then sits waiting on an answer
    // that was answered by nobody.
    if (approval.tool_name === 'ask_question') return null;
    return 'allow';
  }

  // What the approval banner announces. A question gate reads as a question
  // ("Agent asks" + the question text); everything else keeps the permission
  // framing and the command text. Pure so both framings stay pinned by tests.
  function approvalDisplay(app) {
    var args = app && app.args;
    if (app && app.tool_name === 'ask_question') {
      var question = null;
      if (typeof args === 'string' && args.trim()) {
        question = args;
      } else if (args && typeof args === 'object') {
        question = args.question || args.prompt || args.text || null;
      }
      return { title: 'Agent asks', body: question || app.tool_name };
    }
    var cmdText = '';
    if (args && typeof args === 'object') {
      cmdText = args.CommandLine || args.TargetFile || args.command || args.title || args.pattern || '';
    } else if (typeof args === 'string') {
      cmdText = args;
    }
    return {
      title: 'Permission Required: ' + ((app && app.tool_name) || 'tool'),
      body: cmdText || (args ? JSON.stringify(args) : '')
    };
  }

  // The URL scrub waits for the installed app. In the tab, the URL is the only
  // thing iOS Add to Home Screen reliably captures -- WebKit does not read the
  // client-built data: manifest, so scrubbing on first load handed every icon
  // a bare address and an unpairable app. The installed app gets its own
  // storage container and no reason to keep the secrets visible, so there the
  // scrub happens as before.
  function shouldScrubCredentials(standalone) {
    return standalone === true;
  }

  // Pinch-zoom for diagrams: an absolute scale clamped to the sane range.
  // Below 1x is pointless (the SVG already fits its box) and above the max a
  // phone stops rendering the path usefully anyway. A lost or corrupt state
  // falls back to natural size rather than NaN.
  function clampZoom(scale, min, max) {
    var value = typeof scale === 'number' && isFinite(scale) ? scale : min;
    return Math.min(max, Math.max(min, value));
  }

  // Whether this tap is the second of a double-tap (used to reset a zoomed
  // diagram back to natural size).
  function isDoubleTap(lastTapAt, now, maxGapMs) {
    return typeof lastTapAt === 'number' && (now - lastTapAt) < maxGapMs;
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
    approvalDisplay: approvalDisplay,
    shouldScrubCredentials: shouldScrubCredentials,
    peerNotice: peerNotice,
    promptWasDelivered: promptWasDelivered,
    adoptConversationId: adoptConversationId,
    applyStepUpdate: applyStepUpdate,
    socketIsStale: socketIsStale,
    autoAcceptDecision: autoAcceptDecision,
    replaceFileRefs: replaceFileRefs,
    promptRoute: promptRoute,
    agentIdentity: agentIdentity,
    sessionLabel: sessionLabel,
    toolSummary: toolSummary,
    outputSummary: outputSummary,
    isCollapsible: isCollapsible,
    firstLine: firstLine,
    parseEnvelope: parseEnvelope,
    parseFileRefs: parseFileRefs,
    isMermaidLang: isMermaidLang,
    clampZoom: clampZoom,
    isDoubleTap: isDoubleTap,
    trafficForSession: trafficForSession,
    formatTrafficPill: formatTrafficPill
  };
})(typeof window !== 'undefined' ? window : this);

