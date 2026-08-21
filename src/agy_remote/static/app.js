// agy-remote Mobile PWA Client Application with E2EE, Push & Media Attachments

let ws = null;
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
// Web Crypto API (Client-side AES-256-GCM E2EE)
// ----------------------------------------------------------------------------
async function initCrypto() {
  if (!e2eeKeyBase64 || !window.crypto?.subtle) return;
  try {
    // Decode base64url to raw bytes
    let b64 = e2eeKeyBase64.replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    const rawKey = Uint8Array.from(atob(b64), c => c.charCodeAt(0));

    cryptoKey = await window.crypto.subtle.importKey(
      'raw',
      rawKey,
      { name: 'AES-GCM', length: 256 },
      false,
      ['encrypt', 'decrypt']
    );
  } catch (e) {
    console.debug('E2EE key init error:', e);
  }
}

async function encryptData(obj) {
  if (!cryptoKey) return obj;
  const plaintext = new TextEncoder().encode(JSON.stringify(obj));
  const nonce = window.crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await window.crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: nonce },
    cryptoKey,
    plaintext
  );

  return {
    encrypted: true,
    nonce: btoa(String.fromCharCode(...nonce)),
    data: btoa(String.fromCharCode(...new Uint8Array(ciphertext)))
  };
}

async function decryptData(envelope) {
  if (!envelope || !envelope.encrypted || !cryptoKey) return envelope;
  try {
    const nonce = Uint8Array.from(atob(envelope.nonce), c => c.charCodeAt(0));
    const data = Uint8Array.from(atob(envelope.data), c => c.charCodeAt(0));
    const decrypted = await window.crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: nonce },
      cryptoKey,
      data
    );
    return JSON.parse(new TextDecoder().decode(decrypted));
  } catch (e) {
    console.warn('Decryption failed:', e);
    return envelope;
  }
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
    chip.innerHTML = `
      <span>📎 ${f.split('/').pop()}</span>
      <span onclick="removeAttachment(${idx})" style="cursor: pointer; font-weight: bold;">✕</span>
    `;
    attachmentsPreview.appendChild(chip);
  });
}

window.removeAttachment = function(idx) {
  attachedFiles.splice(idx, 1);
  renderAttachmentChips();
};

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
      console.error('Error handling WS event:', e);
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
    currentConversationId = data.active_conversation_id;
    currentSteps = data.steps || [];
    pendingApprovals = data.pending_approvals || [];
    updateHeader();
    renderAllMessages();
    renderConversations(data.conversations || []);
  } else if (type === 'session_switched') {
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
        <div class="thinking-header" onclick="toggleThinking(this)">
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

    // Markdown Content
    if (step.content && step.content.trim()) {
      const textDiv = document.createElement('div');
      textDiv.className = 'model-text-content';
      textDiv.innerHTML = renderMarkdown(step.content);
      modelDiv.appendChild(textDiv);
    }

    // Tool Calls with Diff Rendering
    if (step.tool_calls && step.tool_calls.length > 0) {
      step.tool_calls.forEach(tc => {
        const toolCard = document.createElement('div');
        toolCard.className = 'tool-card';
        const toolName = tc.name || tc.function?.name || 'tool_call';
        const toolArgs = tc.args || tc.function?.arguments || {};

        let bodyContent = '';
        if (toolArgs.TargetContent && toolArgs.ReplacementContent) {
          // Render visual diff for replace_file_content
          bodyContent = renderDiff(toolArgs.TargetContent, toolArgs.ReplacementContent, toolArgs.TargetFile);
        } else {
          bodyContent = `<div class="tool-body">${escapeHtml(JSON.stringify(toolArgs, null, 2))}</div>`;
        }

        toolCard.innerHTML = `
          <div class="tool-header">
            <span class="tool-name-tag">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              ${escapeHtml(toolName)}
            </span>
          </div>
          ${bodyContent}
        `;
        modelDiv.appendChild(toolCard);
      });
    }

    chatContainer.appendChild(modelDiv);
  }
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

  const cmdText = app.args?.CommandLine || app.args?.TargetFile || JSON.stringify(app.args || {});

  banner.innerHTML = `
    <div class="approval-title">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      Permission Required: ${escapeHtml(app.tool_name)}
    </div>
    <div class="approval-cmd">${escapeHtml(cmdText)}</div>
    <div class="approval-actions">
      <button class="btn-approve" onclick="respondApproval('${app.id}', 'allow')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
        Allow
      </button>
      <button class="btn-deny" onclick="respondApproval('${app.id}', 'deny')">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        Deny
      </button>
    </div>
  `;

  chatContainer.appendChild(banner);
}

// Send Approval Response
window.respondApproval = async function(approvalId, decision) {
  triggerVibrate(40);
  const payload = {
    action: 'approve_tool',
    data: { approval_id: approvalId, decision: decision }
  };
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = cryptoKey ? await encryptData(payload) : payload;
    ws.send(JSON.stringify(msg));
  }
};

// Toggle Thinking Section
window.toggleThinking = function(header) {
  const content = header.nextElementSibling;
  const arrow = header.querySelector('span:last-child');
  if (content.style.display === 'none') {
    content.style.display = 'block';
    arrow.textContent = '▼';
  } else {
    content.style.display = 'none';
    arrow.textContent = '▶';
  }
};

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

// Auto Resize Input Area
function autoResizeInput() {
  promptInput.style.height = 'auto';
  promptInput.style.height = Math.min(promptInput.scrollHeight, 120) + 'px';
}

function scrollToBottom() {
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function renderMarkdown(text) {
  if (window.marked) return window.marked.parse(text);
  return escapeHtml(text).replace(/\n/g, '<br/>');
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
  if (currentConversationId) {
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

// Event Listeners
sendBtn.addEventListener('click', () => sendPrompt());

promptInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendPrompt();
  }
});

promptInput.addEventListener('input', autoResizeInput);
menuBtn.addEventListener('click', openDrawer);
closeDrawerBtn.addEventListener('click', closeDrawer);
drawerBackdrop.addEventListener('click', closeDrawer);

// Quick Action Chips
document.querySelectorAll('.chip-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const action = btn.getAttribute('data-action');
    if (action === 'plan') sendPrompt('/plan');
    else if (action === 'goal') sendPrompt('/goal');
    else if (action === 'schedule') sendPrompt('/schedule');
    else if (action === 'stop') sendPrompt('/exit');
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
  connectWebSocket();
})();
