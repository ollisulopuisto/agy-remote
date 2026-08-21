// agy-remote Mobile PWA Client Application with E2EE, Push & Media Attachments

let ws = null;
let currentAgent = 'agy';
let currentConversationId = null;
let currentSteps = [];
let pendingApprovals = [];
let isRecording = false;
let recognition = null;
let autoScroll = true;
let attachedFiles = [];
let cryptoKey = null;

// Parse token and E2EE key from URL and Hash
const urlParams = new URLSearchParams(window.location.search);
let authToken = urlParams.get('token') || localStorage.getItem('agy_remote_token') || '';
if (urlParams.get('token')) {
  localStorage.setItem('agy_remote_token', authToken);
}

// Parse E2EE key from URL hash (e.g. #key=...)
function getHashKey() {
  const match = window.location.hash.match(/key=([^&]+)/);
  return match ? match[1] : (localStorage.getItem('agy_e2ee_key') || null);
}

let e2eeKeyBase64 = getHashKey();
if (e2eeKeyBase64) {
  localStorage.setItem('agy_e2ee_key', e2eeKeyBase64);
}

// Both credentials are now in localStorage, so scrub them out of the visible
// URL: the token would otherwise sit in browser history, in the address bar
// over someone's shoulder, and in any Referer this page emits.
if (urlParams.get('token') || window.location.hash.includes('key=')) {
  try {
    window.history.replaceState({}, document.title, window.location.pathname);
  } catch (e) {
    console.debug('Could not scrub credentials from URL:', e);
  }
}

// DOM Elements
const chatContainer = document.getElementById('chatContainer');
const promptInput = document.getElementById('promptInput');
const sendBtn = document.getElementById('sendBtn');
const micBtn = document.getElementById('micBtn');
const attachBtn = document.getElementById('attachBtn');
const fileInput = document.getElementById('fileInput');
const attachmentsPreview = document.getElementById('attachmentsPreview');
const pushBtn = document.getElementById('pushBtn');
const statusBadge = document.getElementById('statusBadge');
const statusText = document.getElementById('statusText');
const sessionTitle = document.getElementById('sessionTitle');
const sessionSubtitle = document.getElementById('sessionSubtitle');
const drawer = document.getElementById('drawer');
const drawerBackdrop = document.getElementById('drawerBackdrop');
const drawerList = document.getElementById('drawerList');
const menuBtn = document.getElementById('menuBtn');
const closeDrawerBtn = document.getElementById('closeDrawerBtn');

// ----------------------------------------------------------------------------
// Web Crypto API (AES-256-GCM payload encryption)
//
// Envelope v1: { encrypted, v, ts, nonce, data }
// `ts` is bound as GCM additional-authenticated-data and every accepted nonce
// is remembered, so a captured frame cannot be replayed back at either side.
// ----------------------------------------------------------------------------
const E2EE_VERSION = 1;
const E2EE_MAX_AGE_SECONDS = 300;
const seenNonces = new Map();

function envelopeAad(ts) {
  return new TextEncoder().encode(`agy-remote/v${E2EE_VERSION}/${ts}`);
}

function checkReplay(nonceB64, ts) {
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > E2EE_MAX_AGE_SECONDS) {
    throw new Error(`stale envelope (ts=${ts})`);
  }
  if (seenNonces.has(nonceB64)) {
    throw new Error('replayed envelope nonce');
  }
  if (seenNonces.size > 512) {
    const cutoff = now - E2EE_MAX_AGE_SECONDS;
    for (const [n, t] of seenNonces) {
      if (t < cutoff) seenNonces.delete(n);
    }
  }
  seenNonces.set(nonceB64, ts);
}

function b64encode(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

// Web Crypto is gated on secure contexts: https:// or localhost only. Over
// plain http:// to a LAN or tailnet address the API simply does not exist, so
// there is no way to decrypt anything. Say so plainly rather than failing with
// an opaque "frame rejected".
function cryptoUnavailableReason() {
  if (!e2eeKeyBase64) return 'No encryption key in this link. Re-scan the QR code.';
  if (!window.crypto?.subtle) {
    return window.isSecureContext
      ? 'This browser does not provide the Web Crypto API.'
      : 'Encryption needs HTTPS. This page was loaded over plain http://, where '
        + 'browsers disable the Web Crypto API entirely. Restart agy-remote with '
        + 'Tailscale HTTPS, or set AGY_REMOTE_NO_E2EE=1 to run without encryption.';
  }
  return null;
}

function showBlockingNotice(text) {
  statusBadge.className = 'status-badge disconnected';
  statusText.textContent = 'Not encrypted';
  sessionSubtitle.textContent = 'Cannot decrypt';
  chatContainer.innerHTML = '';
  const box = document.createElement('div');
  box.style.cssText =
    'margin:24px 12px;padding:16px;border:1px solid #f59e0b;border-radius:10px;'
    + 'background:rgba(245,158,11,0.08);color:#fcd34d;font-size:13px;line-height:1.55;';
  const title = document.createElement('div');
  title.style.cssText = 'font-weight:700;margin-bottom:8px;color:#fbbf24;';
  title.textContent = 'Encrypted session cannot be opened';
  const body = document.createElement('div');
  body.textContent = text;
  box.append(title, body);
  chatContainer.appendChild(box);
}

async function initCrypto() {
  if (!e2eeKeyBase64 || !window.crypto?.subtle) return;
  try {
    // Decode base64url to raw bytes
    let b64 = e2eeKeyBase64.replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    const rawKey = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    if (rawKey.length !== 32) throw new Error(`key must be 32 bytes, got ${rawKey.length}`);

    cryptoKey = await window.crypto.subtle.importKey(
      'raw',
      rawKey,
      { name: 'AES-GCM', length: 256 },
      false,
      ['encrypt', 'decrypt']
    );
  } catch (e) {
    console.warn('E2EE key init error:', e);
    cryptoKey = null;
  }
}

async function encryptData(obj) {
  if (!cryptoKey) return obj;
  const plaintext = new TextEncoder().encode(JSON.stringify(obj));
  const nonce = window.crypto.getRandomValues(new Uint8Array(12));
  const ts = Math.floor(Date.now() / 1000);
  const ciphertext = await window.crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: envelopeAad(ts) },
    cryptoKey,
    plaintext
  );

  return {
    encrypted: true,
    v: E2EE_VERSION,
    ts: ts,
    nonce: b64encode(nonce),
    data: b64encode(new Uint8Array(ciphertext))
  };
}

async function decryptData(envelope) {
  if (!envelope || !envelope.encrypted) return envelope;
  if (!cryptoKey) {
    // Sealed frame with no key: surface it instead of rendering an envelope.
    throw new Error('encrypted frame received but no E2EE key is loaded');
  }
  if (envelope.v !== E2EE_VERSION) {
    throw new Error(`unsupported envelope version ${envelope.v}`);
  }

  checkReplay(envelope.nonce, envelope.ts);

  const nonce = Uint8Array.from(atob(envelope.nonce), c => c.charCodeAt(0));
  const data = Uint8Array.from(atob(envelope.data), c => c.charCodeAt(0));
  const decrypted = await window.crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: nonce, additionalData: envelopeAad(envelope.ts) },
    cryptoKey,
    data
  );
  return JSON.parse(new TextDecoder().decode(decrypted));
}

// ----------------------------------------------------------------------------
// Web Push Notifications
// ----------------------------------------------------------------------------
async function setupPushNotifications() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    if (pushBtn) pushBtn.style.display = 'none';
    return;
  }

  pushBtn?.addEventListener('click', async () => {
    try {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        alert('Notification permission denied');
        return;
      }

      const res = await fetch('/api/push/vapid-public-key');
      const { public_key } = await res.json();

      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(public_key)
      });

      await fetch(`/api/push/subscribe?token=${encodeURIComponent(authToken)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub)
      });

      pushBtn.style.color = 'var(--success)';
      alert('✓ Lock-screen Push Notifications enabled!');
    } catch (e) {
      console.warn('Failed subscribing to push:', e);
    }
  });
}

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  const outputArray = new Uint8Array(rawData.length);
  for (let i = 0; i < rawData.length; ++i) {
    outputArray[i] = rawData.charCodeAt(i);
  }
  return outputArray;
}

// ----------------------------------------------------------------------------
// Voice Dictation (Web Speech API)
// ----------------------------------------------------------------------------
function setupSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    micBtn.style.display = 'none';
    return;
  }

  recognition = new SpeechRecognition();
  recognition.continuous = false;
  recognition.interimResults = true;
  recognition.lang = navigator.language || 'en-US';

  recognition.onstart = () => {
    isRecording = true;
    micBtn.classList.add('recording');
  };

  recognition.onresult = (event) => {
    let transcript = '';
    for (let i = event.resultIndex; i < event.results.length; ++i) {
      transcript += event.results[i][0].transcript;
    }
    promptInput.value = transcript;
    autoResizeInput();
  };

  recognition.onerror = (event) => {
    console.warn('Speech recognition error:', event.error);
    isRecording = false;
    micBtn.classList.remove('recording');
  };

  recognition.onend = () => {
    isRecording = false;
    micBtn.classList.remove('recording');
  };

  micBtn.addEventListener('click', () => {
    if (isRecording) {
      recognition.stop();
    } else {
      promptInput.focus();
      recognition.start();
    }
  });
}

// ----------------------------------------------------------------------------
// Image / Screenshot Attachments
// ----------------------------------------------------------------------------
function setupAttachments() {
  attachBtn?.addEventListener('click', () => fileInput.click());

  fileInput?.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch(`/api/upload?token=${encodeURIComponent(authToken)}`, {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      if (data.relative_path) {
        attachedFiles.push(data.relative_path);
        renderAttachmentChips();
      }
    } catch (err) {
      console.warn('Upload error:', err);
    }
  });
}

function renderAttachmentChips() {
  attachmentsPreview.innerHTML = '';
  attachedFiles.forEach((f, idx) => {
    const chip = document.createElement('div');
    chip.className = 'attachment-chip';
    const label = document.createElement('span');
    label.textContent = `📎 ${f.split('/').pop()}`;
    const remove = document.createElement('span');
    remove.textContent = '✕';
    remove.style.cssText = 'cursor: pointer; font-weight: bold;';
    remove.addEventListener('click', () => removeAttachment(idx));
    chip.append(label, remove);
    attachmentsPreview.appendChild(chip);
  });
}

function removeAttachment(idx) {
  attachedFiles.splice(idx, 1);
  renderAttachmentChips();
}

// ----------------------------------------------------------------------------
// WebSocket Connection
// ----------------------------------------------------------------------------
function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const tokenParam = authToken ? `?token=${encodeURIComponent(authToken)}` : '';
  const wsUrl = `${protocol}//${window.location.host}/ws${tokenParam}`;

  statusBadge.className = 'status-badge disconnected';
  statusText.textContent = 'Connecting...';

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    statusBadge.className = 'status-badge';
    statusText.textContent = cryptoKey ? 'E2EE Live' : 'Live';
  };

  ws.onmessage = async (event) => {
    try {
      let payload = JSON.parse(event.data);
      if (payload.encrypted) {
        payload = await decryptData(payload);
      }
      handleServerEvent(payload);
    } catch (e) {
      // Drop the frame: a failed tag check, a replay or a stale timestamp all
      // mean this did not come from the server we paired with.
      console.warn('Rejected WS frame:', e.message);
      statusText.textContent = 'Frame rejected';
    }
  };

  ws.onclose = () => {
    statusBadge.className = 'status-badge disconnected';
    statusText.textContent = 'Disconnected';
    setTimeout(connectWebSocket, 2000);
  };
}

// Handle Server Push Events
function handleServerEvent(event) {
  const { event: type, data } = event;

  if (type === 'init') {
    applyTerminal(data.terminal);
    currentAgent = data.agent || 'agy';
    currentConversation = data.conversation || null;
    currentConversationId = data.active_conversation_id;
    currentSteps = data.steps || [];
    pendingApprovals = data.pending_approvals || [];
    updateHeader();
    renderAllMessages();
    renderConversations(data.conversations || []);
  } else if (type === 'session_switched') {
    currentConversation = data.conversation || null;
    currentConversationId = data.conversation_id;
    currentSteps = data.steps || [];
    pendingApprovals = data.pending_approvals || [];
    updateHeader();
    renderAllMessages();
  } else if (type === 'step_added') {
    if (data.conversation_id === currentConversationId) {
      currentSteps.push(data.step);
      appendStep(data.step);
      if (autoScroll) scrollToBottom();
    }
  } else if (type === 'terminal_screen') {
    applyTerminal(data);
  } else if (type === 'approval_request') {
    pendingApprovals.push(data);
    triggerVibrate([80, 40, 100]);
    renderApprovalBanner(data);
    scrollToBottom();
  } else if (type === 'approval_resolved') {
    pendingApprovals = pendingApprovals.filter(a => a.id !== data.id);
    const elem = document.getElementById(`approval-${data.id}`);
    if (elem) elem.remove();
  }
}

// Render All Steps
function renderAllMessages() {
  chatContainer.innerHTML = '';

  if (currentSteps.length === 0) {
    chatContainer.innerHTML = `
      <div style="text-align: center; color: var(--text-muted); margin-top: 40px; font-size: 13px;">
        <p>No active steps in this session.</p>
        <p style="margin-top: 6px; font-size: 11px;">Send a prompt below to begin!</p>
      </div>
    `;
    return;
  }

  chatContainer.appendChild(sessionDivider());
  currentSteps.forEach(step => appendStep(step));
  pendingApprovals.forEach(app => renderApprovalBanner(app));
  scrollToBottom();
}

// Append a Single Step
function appendStep(step) {
  const stepType = step.type || 'UNKNOWN';
  const source = step.source || 'UNKNOWN';

  if (stepType === 'USER_INPUT' || source === 'USER_INPUT' || source === 'USER_EXPLICIT') {
    const userDiv = document.createElement('div');
    userDiv.className = 'message-user';
    userDiv.textContent = step.content || '';
    chatContainer.appendChild(userDiv);
    return;
  }

  if (stepType === 'PLANNER_RESPONSE' || source === 'MODEL') {
    const modelDiv = document.createElement('div');
    modelDiv.className = 'message-model';

    // Thinking Box (Collapsible)
    if (step.thinking && step.thinking.trim()) {
      const thinkingBox = document.createElement('div');
      thinkingBox.className = 'thinking-box';
      thinkingBox.innerHTML = `
        <div class="thinking-header" data-toggle="thinking">
          <span class="thinking-title">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>
            Thinking Process
          </span>
          <span style="font-size: 10px;">▶</span>
        </div>
        <div class="thinking-content" style="display: none;">${escapeHtml(step.thinking)}</div>
      `;
      modelDiv.appendChild(thinkingBox);
    }

    // Markdown content. A GENERIC step is not the model talking -- it is the
    // output of the tool it just ran, which is the bulkiest thing in a
    // transcript and the least often wanted, so it collapses to a line count.
    if (step.content && step.content.trim()) {
      const textDiv = document.createElement('div');
      textDiv.className = 'model-text-content';
      textDiv.innerHTML = renderMarkdown(step.content);

      if (stepType === 'GENERIC' && AgyFormat.isCollapsible(step.content)) {
        modelDiv.appendChild(collapsed(AgyFormat.outputSummary(step.content), textDiv, 'output-card'));
      } else {
        modelDiv.appendChild(textDiv);
      }
    }

    // Tool calls: one line each, arguments and diff behind an expand, the way
    // the desktop shows them. Rendering every argument inline buried the
    // conversation under a single `du`.
    if (step.tool_calls && step.tool_calls.length > 0) {
      step.tool_calls.forEach(tc => {
        const toolName = tc.name || tc.function?.name || 'tool_call';
        const toolArgs = tc.args || tc.function?.arguments || {};

        let bodyContent = '';
        if (toolArgs.TargetContent && toolArgs.ReplacementContent) {
          bodyContent = renderDiff(toolArgs.TargetContent, toolArgs.ReplacementContent, toolArgs.TargetFile);
        } else {
          bodyContent = `<div class="tool-body">${renderToolArgs(toolArgs)}</div>`;
        }

        const toolCard = document.createElement('details');
        toolCard.className = 'tool-card';
        toolCard.innerHTML = `
          <summary class="tool-header">
            <span class="tool-name-tag">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              ${escapeHtml(AgyFormat.toolSummary(toolName, toolArgs))}
            </span>
          </summary>
          ${bodyContent}
        `;
        modelDiv.appendChild(toolCard);
      });
    }

    chatContainer.appendChild(modelDiv);
    return;
  }

  // Anything else -- SYSTEM/CHECKPOINT today, whatever agy adds tomorrow -- was
  // being dropped without a trace, so the phone quietly showed less than the
  // desktop. Show it plainly rather than pretending it does not exist.
  const content = (step.content || '').trim();
  if (!content) return;

  const kind = stepType.toLowerCase().replace(/_/g, ' ');
  // agy's checkpoints announce "earlier parts of this conversation have been
  // truncated" at the start of a brand new session. Saying whose words these
  // are stops a fresh session from reading as a continuation of the last.
  const label = step.scaffolding
    ? `agy scaffolding · ${kind}`
    : `${kind}: ${AgyFormat.firstLine(content)}`;
  const body = document.createElement('div');
  body.className = 'system-body';
  body.textContent = content;

  if (AgyFormat.isCollapsible(content)) {
    chatContainer.appendChild(collapsed(label, body, 'message-system'));
    return;
  }

  const systemDiv = document.createElement('div');
  systemDiv.className = 'message-system';
  systemDiv.textContent = label;
  chatContainer.appendChild(systemDiv);
}

// Which session you are looking at, and when it began.
function sessionDivider() {
  const div = document.createElement('div');
  div.className = 'session-divider';

  const started = currentConversation && currentConversation.created_at
    ? new Date(currentConversation.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : '';

  div.textContent = AgyFormat.sessionLabel(currentConversation, started) ||
    (currentConversationId ? `Session ${currentConversationId.slice(0, 8)}` : 'No active session');
  return div;
}

// A <details> block: summary line visible, everything else one tap away. Native
// disclosure rather than a hand-rolled toggle, so it stays keyboard- and
// screenreader-operable for free.
function collapsed(summaryText, bodyElement, className) {
  const box = document.createElement('details');
  box.className = className;

  const summary = document.createElement('summary');
  summary.textContent = summaryText;
  box.appendChild(summary);
  box.appendChild(bodyElement);
  return box;
}

// Tool arguments, one per line. JSON.stringify of the whole object buried the
// command under braces and escaped quotes; the server has already decoded the
// values agy stored as JSON strings.
function renderToolArgs(args) {
  const entries = Object.entries(args || {});
  if (entries.length === 0) return '<span class="arg-empty">no arguments</span>';

  return entries.map(([key, value]) => {
    const shown = typeof value === 'string' ? value : JSON.stringify(value);
    return `<div class="arg-row"><span class="arg-key">${escapeHtml(key)}</span>` +
           `<span class="arg-value">${escapeHtml(shown)}</span></div>`;
  }).join('');
}

// Render Visual Diff
function renderDiff(target, replacement, filepath) {
  const targetLines = target.split('\n');
  const replLines = replacement.split('\n');
  let html = `<div class="diff-container"><div class="diff-file-title">📝 ${escapeHtml(filepath || 'File Edit')}</div>`;

  targetLines.forEach(l => {
    html += `<div class="diff-line diff-del">- ${escapeHtml(l)}</div>`;
  });
  replLines.forEach(l => {
    html += `<div class="diff-line diff-add">+ ${escapeHtml(l)}</div>`;
  });

  html += '</div>';
  return html;
}

// Render Interactive Tool Approval Banner
function renderApprovalBanner(app) {
  const existing = document.getElementById(`approval-${app.id}`);
  if (existing) return;

  const banner = document.createElement('div');
  banner.id = `approval-${app.id}`;
  banner.className = 'approval-banner';

  const cmdText =
    app.args?.CommandLine ||
    app.args?.TargetFile ||
    app.args?.command ||
    app.args?.title ||
    app.args?.pattern ||
    (typeof app.args === 'string' ? app.args : JSON.stringify(app.args || {}));

  const alwaysBtn =
    currentAgent === 'opencode'
      ? `<button class="btn-always" data-approval-id="${escapeHtml(app.id)}" data-decision="always">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="13 17 18 12 13 7"/><polyline points="6 17 11 12 6 7"/></svg>
          Always
        </button>`
      : '';

  banner.innerHTML = `
    <div class="approval-title">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      Permission Required: ${escapeHtml(app.tool_name)}
    </div>
    <div class="approval-cmd">${escapeHtml(cmdText)}</div>
    <div class="approval-actions">
      <button class="btn-approve" data-approval-id="${escapeHtml(app.id)}" data-decision="allow">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
        Allow
      </button>
      ${alwaysBtn}
      <button class="btn-deny" data-approval-id="${escapeHtml(app.id)}" data-decision="deny">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        Deny
      </button>
    </div>
  `;

  chatContainer.appendChild(banner);
}

// Send Approval Response
async function respondApproval(approvalId, decision) {
  triggerVibrate(40);
  const payload = {
    action: 'approve_tool',
    data: { approval_id: approvalId, decision: decision }
  };
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = cryptoKey ? await encryptData(payload) : payload;
    ws.send(JSON.stringify(msg));
  }
}

// Toggle Thinking Section
function toggleThinking(header) {
  const content = header.nextElementSibling;
  const arrow = header.querySelector('span:last-child');
  if (content.style.display === 'none') {
    content.style.display = 'block';
    arrow.textContent = '▼';
  } else {
    content.style.display = 'none';
    arrow.textContent = '▶';
  }
}

// One delegated listener for dynamically-rendered controls, so no generated
// markup needs an inline handler (which the CSP forbids).
chatContainer.addEventListener('click', async (e) => {
  const copyBtn = e.target.closest('.copy-code-btn');
  if (copyBtn) {
    const textToCopy = copyBtn.getAttribute('data-copy');
    if (textToCopy && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(textToCopy);
        triggerVibrate(20);
        const originalText = copyBtn.textContent;
        copyBtn.textContent = '✓ Copied';
        copyBtn.style.color = 'var(--success)';
        setTimeout(() => {
          copyBtn.textContent = originalText;
          copyBtn.style.color = '';
        }, 1800);
      } catch (err) {
        console.warn('Copy failed:', err);
      }
    }
    return;
  }

  const header = e.target.closest('[data-toggle="thinking"]');
  if (header) {
    toggleThinking(header);
    return;
  }
  const btn = e.target.closest('[data-approval-id]');
  if (btn) {
    respondApproval(btn.dataset.approvalId, btn.dataset.decision);
  }
});

// Send Prompt
async function sendPrompt(text) {
  let prompt = (text || promptInput.value).trim();
  if (!prompt && attachedFiles.length === 0) return;

  if (attachedFiles.length > 0) {
    prompt += `\n\n[Attached files: ${attachedFiles.join(', ')}]`;
    attachedFiles = [];
    renderAttachmentChips();
  }

  triggerVibrate(25);

  const payload = {
    action: 'send_prompt',
    data: { prompt: prompt }
  };

  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = cryptoKey ? await encryptData(payload) : payload;
    ws.send(JSON.stringify(msg));
  }

  promptInput.value = '';
  autoResizeInput();
  scrollToBottom();
}

// The mirrored terminal. The server runs the emulator and sends a grid of
// plain text, so the panels agy draws -- pickers, confirmations, the mode in
// the status bar -- are visible here without shipping an emulator to the phone.
let currentConversation = null;

let terminalVisible = false;
try {
  terminalVisible = localStorage.getItem('agy-remote.screen') === '1';
} catch (e) {
  terminalVisible = false;
}

function applyTerminal(snapshot) {
  if (!snapshot) return;

  const badge = document.getElementById('modeBadge');
  if (badge) {
    badge.textContent = snapshot.mode || '';
    badge.hidden = !snapshot.mode;
  }

  const panel = document.getElementById('terminalPanel');
  const screen = document.getElementById('terminalScreen');
  if (!panel || !screen) return;

  panel.hidden = !terminalVisible;
  if (terminalVisible) {
    // textContent, never innerHTML: this is output from whatever agy just ran.
    screen.textContent = (snapshot.lines || []).join('\n').replace(/\s+$/, '');
  }
}

function toggleTerminal() {
  terminalVisible = !terminalVisible;
  try {
    localStorage.setItem('agy-remote.screen', terminalVisible ? '1' : '0');
  } catch (e) {
    /* private browsing: the toggle just does not persist */
  }
  const panel = document.getElementById('terminalPanel');
  if (panel) panel.hidden = !terminalVisible;
  if (terminalVisible) refreshTerminal();
}

async function refreshTerminal() {
  const payload = { action: 'request_screen', data: {} };
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = cryptoKey ? await encryptData(payload) : payload;
    ws.send(JSON.stringify(msg));
  }
}

// Press a single key in the supervised terminal. agy's execution mode
// (Shift+Tab), its panels (Esc) and its selection lists (arrows, Enter) are
// unreachable through a prompt line, which always ends in a submit.
async function sendKey(key) {
  triggerVibrate(15);

  const payload = { action: 'send_key', data: { key: key } };
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = cryptoKey ? await encryptData(payload) : payload;
    ws.send(JSON.stringify(msg));
  }
}

// Auto Resize Input Area
function autoResizeInput() {
  promptInput.style.height = 'auto';
  promptInput.style.height = Math.min(Math.max(promptInput.scrollHeight, 52), 220) + 'px';
}

function scrollToBottom() {
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

// Escape-first Markdown renderer.
//
// Model and tool output is attacker-influenceable: an agent that reads a web
// page or a file can be induced to emit an XSS payload. Because the result of
// this function is assigned to innerHTML, it MUST NOT be able to emit markup
// that came from `text`. Everything is HTML-escaped up front, and only the
// tags this function itself introduces can survive.
function renderMarkdown(text) {
  if (!text) return '';

  const codeBlocks = [];
  // Stash fenced code first so its contents are never treated as markup.
  let out = String(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const cleanCode = code.replace(/\n$/, '');
    const i = codeBlocks.push(
      `<div class="code-block-wrapper">` +
      `<button class="copy-code-btn" data-copy="${escapeHtml(cleanCode)}" title="Copy code">Copy</button>` +
      `<pre class="md-code"><code>${escapeHtml(cleanCode)}</code></pre></div>`
    ) - 1;
    return `\u0000CODE${i}\u0000`;
  });

  out = escapeHtml(out);

  const inline = [];
  out = out.replace(/`([^`\n]+)`/g, (_, code) => {
    const i = inline.push(`<code class="md-inline">${code}</code>`) - 1;
    return `\u0000IC${i}\u0000`;
  });

  out = out
    .replace(/^######\s+(.*)$/gm, '<h6>$1</h6>')
    .replace(/^#####\s+(.*)$/gm, '<h5>$1</h5>')
    .replace(/^####\s+(.*)$/gm, '<h4>$1</h4>')
    .replace(/^###\s+(.*)$/gm, '<h3>$1</h3>')
    .replace(/^##\s+(.*)$/gm, '<h2>$1</h2>')
    .replace(/^#\s+(.*)$/gm, '<h1>$1</h1>')
    .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/^\s*[-*+]\s+(.*)$/gm, '<li>$1</li>')
    .replace(/^\s*\d+\.\s+(.*)$/gm, '<li>$1</li>');

  out = out.replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m.replace(/\n/g, '')}</ul>`);

  // Links: only http(s), and the href is rebuilt from an escaped, validated
  // string so `javascript:` and friends can never appear.
  out = out.replace(/\[([^\]\n]+)\]\((https?:&#x2F;&#x2F;[^)\s]+|https?:\/\/[^)\s]+)\)/g, (m, label, href) => {
    const clean = href.replace(/&#x2F;/g, '/');
    if (!/^https?:\/\//i.test(clean)) return m;
    return `<a href="${escapeHtml(clean)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });

  out = out.replace(/\n/g, '<br/>');
  out = out.replace(/\u0000IC(\d+)\u0000/g, (_, i) => inline[Number(i)]);
  out = out.replace(/(<br\/>)?\u0000CODE(\d+)\u0000/g, (_, br, i) => codeBlocks[Number(i)]);
  return out;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function triggerVibrate(pattern = [60, 40, 80]) {
  if (navigator.vibrate) {
    try { navigator.vibrate(pattern); } catch (e) {}
  }
}

function updateHeader() {
  if (currentConversation && currentConversation.title) {
    sessionSubtitle.textContent = currentConversation.title;
  } else if (currentConversationId) {
    sessionSubtitle.textContent = `Session: ${currentConversationId.slice(0, 8)}...`;
  } else {
    sessionSubtitle.textContent = 'No Active Session';
  }
}

function renderConversations(convs) {
  drawerList.innerHTML = '';
  convs.forEach(c => {
    const item = document.createElement('div');
    item.className = `session-item ${c.id === currentConversationId ? 'active' : ''}`;
    item.onclick = async () => {
      const payload = {
        action: 'switch_conversation',
        data: { conversation_id: c.id }
      };
      if (ws && ws.readyState === WebSocket.OPEN) {
        const msg = cryptoKey ? await encryptData(payload) : payload;
        ws.send(JSON.stringify(msg));
      }
      closeDrawer();
    };

    const timeStr = c.updated_at ? new Date(c.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
    item.innerHTML = `
      <div class="session-item-title">${escapeHtml(c.title || c.id)}</div>
      <div class="session-item-meta">${timeStr} • ${c.step_count} steps</div>
    `;
    drawerList.appendChild(item);
  });
}

function openDrawer() {
  drawer.classList.add('open');
  drawerBackdrop.classList.add('open');
}

function closeDrawer() {
  drawer.classList.remove('open');
  drawerBackdrop.classList.remove('open');
}

// Mobile Visual Viewport Handling (iOS & Android virtual keyboard)
function syncViewportHeight() {
  if (window.visualViewport) {
    const height = window.visualViewport.height;
    document.documentElement.style.setProperty('--app-height', `${height}px`);
  } else {
    document.documentElement.style.setProperty('--app-height', `${window.innerHeight}px`);
  }
}

if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', () => {
    syncViewportHeight();
    if (autoScroll) scrollToBottom();
  });
  window.visualViewport.addEventListener('scroll', () => {
    // Prevent iOS rubberband scroll from shifting the fixed UI out of view
    window.scrollTo(0, 0);
  });
}
window.addEventListener('resize', syncViewportHeight);
window.addEventListener('orientationchange', () => {
  setTimeout(syncViewportHeight, 200);
});
syncViewportHeight();

// Event Listeners
sendBtn.addEventListener('click', () => sendPrompt());

promptInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendPrompt();
  }
});

promptInput.addEventListener('input', autoResizeInput);
promptInput.addEventListener('focus', () => {
  setTimeout(() => {
    syncViewportHeight();
    if (autoScroll) scrollToBottom();
  }, 300);
});

menuBtn.addEventListener('click', openDrawer);
closeDrawerBtn.addEventListener('click', closeDrawer);
drawerBackdrop.addEventListener('click', closeDrawer);

// Swipe gesture to close drawer
let drawerTouchStartX = 0;
let drawerTouchStartY = 0;
drawer.addEventListener('touchstart', (e) => {
  drawerTouchStartX = e.touches[0].clientX;
  drawerTouchStartY = e.touches[0].clientY;
}, { passive: true });

drawer.addEventListener('touchend', (e) => {
  const diffX = e.changedTouches[0].clientX - drawerTouchStartX;
  const diffY = e.changedTouches[0].clientY - drawerTouchStartY;
  if (diffX < -50 && Math.abs(diffX) > Math.abs(diffY)) {
    closeDrawer();
  }
}, { passive: true });

// Quick Action Chips: either a named keypress or a literal slash command.
document.querySelectorAll('.chip-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.getAttribute('data-toggle') === 'screen') {
      toggleTerminal();
      return;
    }
    const key = btn.getAttribute('data-key');
    if (key) {
      sendKey(key);
      return;
    }
    const cmd = btn.getAttribute('data-cmd');
    if (cmd) sendPrompt(cmd);
  });
});

// Register Service Worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(err => {
    console.debug('ServiceWorker registration skipped:', err);
  });
}

// Initialize all modules
(async () => {
  await initCrypto();
  setupSpeechRecognition();
  setupPushNotifications();
  setupAttachments();

  // If the server is sealing frames we cannot open, connecting only produces a
  // stream of rejections. Explain the cause instead.
  let serverUsesE2ee = true;
  try {
    const res = await fetch(`/api/status?token=${encodeURIComponent(authToken)}`);
    const status = await res.json();
    serverUsesE2ee = status.e2ee_enabled !== false;
  } catch (e) {
    console.debug('Could not read server status:', e);
  }

  const reason = cryptoUnavailableReason();
  if (serverUsesE2ee && reason) {
    showBlockingNotice(reason);
    return;
  }

  connectWebSocket();
})();
