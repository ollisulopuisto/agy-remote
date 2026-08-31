// The agy slash-command menu, as data.
//
// Typing "/" on a phone keyboard is a long-press away on iOS and a hidden
// symbol layer on others, so the PWA offers a navigable menu instead: this
// module is the tree the menu renders. The command set comes from
// docs/cc-parity-plan.md and the README's warnings -- two flags matter:
//
//   panel  the command opens a transient TUI panel (/model, /permissions,
//          /resume) that never reaches the transcript; the caller should
//          open the terminal mirror so the user can see what happened.
//   arg    the command needs an argument (/rename <name>); the caller
//          should prefill the composer instead of sending blind.
//
// Classic script, not a module: the PWA loads no bundler and no third-party
// code. Attached to window so the page and the tests can both reach it.

(function (global) {
  'use strict';

  var CATEGORIES = [
    {
      id: 'session',
      title: 'Session',
      commands: [
        { name: '/new', desc: 'Start a fresh conversation' },
        { name: '/clear', desc: 'Clear the current conversation' },
        { name: '/resume', desc: 'Pick a past conversation', panel: true },
        { name: '/rename', desc: 'Rename the conversation', arg: true },
        { name: '/fork', desc: 'Branch the conversation here' },
        { name: '/rewind', desc: 'Rewind to an earlier turn' },
      ],
    },
    {
      id: 'planning',
      title: 'Planning & tasks',
      commands: [
        { name: '/planning', desc: 'Toggle planning mode' },
        { name: '/tasks', desc: 'Show the task list' },
      ],
    },
    {
      id: 'context',
      title: 'Context & usage',
      commands: [
        { name: '/context', desc: 'Show what fills the context window' },
        { name: '/usage', desc: 'Show token usage and cost' },
      ],
    },
    {
      id: 'model',
      title: 'Model & speed',
      commands: [
        { name: '/model', desc: 'Pick the model', panel: true },
        { name: '/fast', desc: 'Toggle the fast lane' },
      ],
    },
    {
      id: 'tools',
      title: 'Agents & tools',
      commands: [
        { name: '/agents', desc: 'List available agents' },
        { name: '/mcp', desc: 'Show MCP servers and tools' },
        { name: '/permissions', desc: 'Review tool permissions', panel: true },
      ],
    },
    {
      id: 'help',
      title: 'Help',
      commands: [
        { name: '/help', desc: 'Print every command agy knows' },
        { name: '/diff', desc: 'Show the working diff' },
      ],
    },
  ];

  function categories() {
    return CATEGORIES;
  }

  function all() {
    return CATEGORIES.flatMap(function (cat) { return cat.commands; });
  }

  function find(name) {
    if (!name) return null;
    var wanted = name.startsWith('/') ? name : '/' + name;
    return all().find(function (cmd) { return cmd.name === wanted; }) || null;
  }

  // Flat search across every command, for the menu's search box. A query
  // matches a command's name or its description, case-insensitively; each
  // result carries its category title so the flat list still shows context.
  function search(query) {
    if (!query) return [];
    var q = query.toLowerCase();
    return CATEGORIES.flatMap(function (cat) {
      return cat.commands
        .filter(function (cmd) {
          return cmd.name.toLowerCase().indexOf(q) !== -1 ||
            cmd.desc.toLowerCase().indexOf(q) !== -1;
        })
        .map(function (cmd) {
          var hit = Object.assign({}, cmd, { cat: cat.title });
          return hit;
        });
    });
  }

  global.AgyCommands = {
    categories: categories,
    all: all,
    find: find,
    search: search,
  };
})(typeof window !== 'undefined' ? window : this);
