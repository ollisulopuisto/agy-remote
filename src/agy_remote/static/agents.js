// Pure helpers for multi-agent dashboard, sorting, and task handoff.
//
// Classic script, not a module: the PWA loads no bundler and no third-party
// code. Attached to window/global and module.exports for Node tests.
(function (global) {
  'use strict';

  var STATUS_PRIORITY = {
    'needs_attention': 0,
    'running': 1,
    'completed': 2,
    'failed': 3,
    'cancelled': 4
  };

  function sortAgents(agents) {
    if (!Array.isArray(agents)) return [];
    var list = agents.slice();
    list.sort(function (a, b) {
      var pA = STATUS_PRIORITY[a.status] != null ? STATUS_PRIORITY[a.status] : 5;
      var pB = STATUS_PRIORITY[b.status] != null ? STATUS_PRIORITY[b.status] : 5;
      if (pA !== pB) return pA - pB;

      var tA = a.last_activity || a.started_at || 0;
      var tB = b.last_activity || b.started_at || 0;
      var msA = typeof tA === 'number' ? tA : new Date(tA).getTime() || 0;
      var msB = typeof tB === 'number' ? tB : new Date(tB).getTime() || 0;
      return msB - msA;
    });
    return list;
  }

  function groupAgentsByStatus(agents) {
    var groups = {
      running: [],
      needs_attention: [],
      completed: [],
      failed: []
    };
    var sorted = sortAgents(agents);
    for (var i = 0; i < sorted.length; i++) {
      var a = sorted[i];
      var s = a.status || 'running';
      if (s === 'needs_attention') {
        groups.needs_attention.push(a);
      } else if (s === 'running') {
        groups.running.push(a);
      } else if (s === 'completed') {
        groups.completed.push(a);
      } else {
        groups.failed.push(a);
      }
    }
    return groups;
  }

  function formatAgentStatus(status) {
    var s = (status || '').toLowerCase();
    if (s === 'running') {
      return { label: 'Running', icon: '●', badgeClass: 'agent-status-running' };
    }
    if (s === 'needs_attention') {
      return { label: 'Needs attention', icon: '!', badgeClass: 'agent-status-attention' };
    }
    if (s === 'completed') {
      return { label: 'Completed', icon: '✓', badgeClass: 'agent-status-completed' };
    }
    if (s === 'failed') {
      return { label: 'Failed', icon: '✕', badgeClass: 'agent-status-failed' };
    }
    if (s === 'cancelled') {
      return { label: 'Cancelled', icon: '⊘', badgeClass: 'agent-status-cancelled' };
    }
    return { label: status || 'Unknown', icon: '●', badgeClass: 'agent-status-running' };
  }

  function formatProviderBadge(provider) {
    var p = (provider || 'worker').toLowerCase();
    if (p === 'gemini') return { name: 'Gemini', pillClass: 'provider-gemini' };
    if (p === 'claude') return { name: 'Claude', pillClass: 'provider-claude' };
    if (p === 'codex') return { name: 'Codex', pillClass: 'provider-codex' };
    if (p === 'astra') return { name: 'Astra', pillClass: 'provider-astra' };
    if (p === 'antigravity') return { name: 'Antigravity', pillClass: 'provider-agy' };
    return { name: p.charAt(0).toUpperCase() + p.slice(1), pillClass: 'provider-default' };
  }

  function formatElapsedTime(startedAt, now) {
    if (!startedAt) return '';
    var startMs = typeof startedAt === 'number' ? startedAt : new Date(startedAt).getTime();
    if (isNaN(startMs)) return '';
    var currMs = now != null ? now : Date.now();
    var diffSec = Math.max(0, Math.floor((currMs - startMs) / 1000));
    if (diffSec < 60) return diffSec + 's';
    var diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return diffMin + 'm';
    var diffHr = Math.floor(diffMin / 60);
    var remMin = diffMin % 60;
    return diffHr + 'h ' + remMin + 'm';
  }

  function buildHandoffPayload(job, targetProvider) {
    var j = job || {};
    var provider = targetProvider || (j.provider === 'gemini' ? 'claude' : 'gemini');
    var priorResult = j.result || '';
    var priorTask = j.current_task || '';
    var commit = j.commit ? '\nCommit: ' + j.commit : '';
    var remaining = Array.isArray(j.remaining_issues) && j.remaining_issues.length > 0
      ? '\nRemaining issues: ' + j.remaining_issues.join('; ')
      : '';

    var taskDesc = 'Continue task after ' + (j.provider || 'previous agent') + ':\n' +
      (priorResult ? priorResult : priorTask);

    var context = 'Prior task: ' + priorTask +
      (priorResult ? '\nResult: ' + priorResult : '') +
      commit + remaining;

    return {
      project: j.project || '',
      provider: provider,
      task: taskDesc,
      context: context
    };
  }

  var AgentHelpers = {
    sortAgents: sortAgents,
    groupAgentsByStatus: groupAgentsByStatus,
    formatAgentStatus: formatAgentStatus,
    formatProviderBadge: formatProviderBadge,
    formatElapsedTime: formatElapsedTime,
    buildHandoffPayload: buildHandoffPayload
  };

  global.AgentHelpers = AgentHelpers;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = AgentHelpers;
  }
})(typeof window !== 'undefined' ? window : globalThis);
