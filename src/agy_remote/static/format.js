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

  global.AgyFormat = {
    toolSummary: toolSummary,
    outputSummary: outputSummary,
    isCollapsible: isCollapsible,
    firstLine: firstLine
  };
})(typeof window !== 'undefined' ? window : this);
