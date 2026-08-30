// agy-remote Mobile PWA Client Application with E2EE, Push & Media Attachments

let ws = null;
let currentAgent = '';
let currentConversationId = null;
let currentSteps = [];
let pendingApprovals = [];
let isRecording = false;
let recognition = null;
let autoScroll = true;
let attachedFiles = [];
let cryptoKey = null;
let agentTraffic = [];

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

// Build the paired manifest on the device, before anyone can install the app.
// An installed iOS web app gets its own storage container -- nothing this tab
// saved comes with it -- and launches at the manifest's start_url, so a bare
// "/" produced an icon that opened unpaired. The credentials go into a
// client-built data: URI rather than a server response: the E2EE key must
// never appear on the wire, which is the whole reason it travels in the QR
// fragment. The server's /manifest.json stays anonymous.
(async () => {
  const key = e2eeKeyBase64;
  if (!authToken || !key) return;
  const link = document.querySelector('link[rel="manifest"]');
  if (!link) return;
  try {
    const base = await fetch('/manifest.json').then(r => r.json());
    const paired = window.AgyFormat.pairedManifest(base, authToken, key);
    if (paired) {
      link.href = 'data:application/manifest+json,' + encodeURIComponent(paired);
    }
  } catch (e) {
    // The anonymous manifest still installs; it just opens unpaired.
    console.debug('Could not build the paired manifest:', e);
  }
})();

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
const fileViewer = document.getElementById('fileViewer');
const fileViewerBackdrop = document.getElementById('fileViewerBackdrop');
const fileViewerName = document.getElementById('fileViewerName');
const fileViewerPath = document.getElementById('fileViewerPath');
const fileViewerBody = document.getElementById('fileViewerBody');
const fileViewerClose = document.getElementById('fileViewerClose');

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

// One identity per install. The server counts devices, not sockets, so the
// reconnect races and suspended reloads this page leaves behind cannot
// inflate the peer badge; the id survives reloads in localStorage, and only
// a fresh install counts as a new device -- which is what the badge means.
function getDeviceId() {
  let id = null;
  try {
    id = localStorage.getItem('agy-device-id');
  } catch (e) { /* private mode: an id is minted that just never persists */ }
  if (!id) {
    id = (crypto.randomUUID && crypto.randomUUID())
      || ('d' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
    try {
      localStorage.setItem('agy-device-id', id);
    } catch (e) { /* the id lives for this page load only */ }
  }
  return id;
}

function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const params = new URLSearchParams();
  if (authToken) params.set('token', authToken);
  params.set('device', getDeviceId());
  const wsUrl = `${protocol}//${window.location.host}/ws?${params.toString()}`;

  statusBadge.className = 'status-badge disconnected';
  statusText.textContent = 'Connecting...';

  // A previous socket is closed, not abandoned, and its handlers are
  // detached: a late close event from it must not schedule a second
  // reconnect behind this one -- that race was one way the device count
  // used to climb.
  if (ws) {
    const prev = ws;
    prev.onclose = null;
    prev.onopen = null;
    try { prev.close(); } catch (e) { /* already gone */ }
  }

  const socket = new WebSocket(wsUrl);
  ws = socket;

  socket.onopen = () => {
    statusBadge.className = 'status-badge';
    statusText.textContent = cryptoKey ? 'E2EE Live' : 'Live';
    startHeartbeat(socket);
    reportFocusState();
  };

  socket.onmessage = async (event) => {
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

  socket.onclose = () => {
    // A newer connection owns the status bar and the reconnect; this socket
    // was superseded while it was still coming up.
    if (ws !== socket) return;
    stopHeartbeat();
    statusBadge.className = 'status-badge disconnected';
    statusText.textContent = 'Disconnected';
    setTimeout(connectWebSocket, 2000);
  };
}

// -- heartbeat ---------------------------------------------------------------
//
// The badge used to say "E2EE Live" over a socket with nothing underneath: iOS
// suspends a backgrounded PWA, and the socket it hands back reads OPEN while
// every write goes nowhere and `onclose` never fires. Prompts vanished into it.
// The server answers `ping` with `pong`, so a pong that stops coming back is
// what reveals it -- then close the socket and let the reconnect run.

const HEARTBEAT_MS = 15000;
const PONG_TIMEOUT_MS = 20000;
let heartbeatTimer = null;
let lastPongAt = null;

function startHeartbeat(socket) {
  stopHeartbeat();
  lastPongAt = Date.now();
  heartbeatTimer = setInterval(async () => {
    if (socket.readyState !== WebSocket.OPEN) return;
    if (window.AgyFormat.socketIsStale(lastPongAt, Date.now(), PONG_TIMEOUT_MS)) {
      statusBadge.className = 'status-badge disconnected';
      statusText.textContent = 'Connection lost — reconnecting';
      socket.close();
      return;
    }
    try {
      const payload = { action: 'ping' };
      const msg = cryptoKey ? await encryptData(payload) : payload;
      socket.send(JSON.stringify(msg));
    } catch (e) {
      socket.close();
    }
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

// Coming back to the app is when a suspended socket is most likely to be dead,
// and waiting a whole heartbeat to find out is a prompt lost in between.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    connectWebSocket();
    return;
  }
  lastPongAt = Date.now() - PONG_TIMEOUT_MS + 3000;  // answer within 3s or it is gone
});

// Handle Server Push Events
function handleServerEvent(event) {
  const { event: type, data } = event;

  if (type === 'pong') {
    lastPongAt = Date.now();
    return;
  }

  if (type === 'init') {
    applyTerminal(data.terminal);
    currentAgent = data.agent || '';
    applyAgentIdentity();
    currentConversation = data.conversation || null;
    currentConversationId = data.active_conversation_id;
    currentSteps = data.steps || [];
    pendingApprovals = data.pending_approvals || [];
    agentTraffic = data.agent_traffic || [];
    updateHeader();
    renderAllMessages();
    renderConversations(data.conversations || []);
    updateApprovalIndicators();
    checkUrlNavigation();
  } else if (type === 'session_switched') {
    currentConversation = data.conversation || null;
    currentConversationId = data.conversation_id;
    currentSteps = data.steps || [];
    pendingApprovals = data.pending_approvals || [];
    updateHeader();
    renderAllMessages();
    if (data.conversations) {
      renderConversations(data.conversations);
    } else {
      document.querySelectorAll('.session-item[data-conversation-id]').forEach(item => {
        item.classList.toggle('active', item.dataset.conversationId === currentConversationId);
      });
      updateSessionDots();
    }
    updateApprovalIndicators();
    checkUrlNavigation();
  } else if (type === 'peers') {
    applyPeerCount(data && data.count);
  } else if (type === 'agent_traffic') {
    agentTraffic = (data && data.pairs) || [];
    renderConversations(drawerConversations);
  } else if (type === 'session_spawning') {
    // A stage of this sheet's own spawn: the chip walks cloning -> starting,
    // and a failure shows git's stderr rather than a guess.
    if (!activeSpawn || !window.AgySessions.spawnEventMatches(data, activeSpawn)) return;
    const label = window.AgySessions.spawnStageLabel(data.stage, data.error);
    if (!label) return;
    if (data.stage === 'failed') {
      showSpawnFailure(label);
    } else {
      newSessionStage.textContent = label;
    }
  } else if (type === 'session_created') {
    if (!activeSpawn || !window.AgySessions.spawnEventMatches(data, activeSpawn)) return;
    finishSpawn(data);
  } else if (type === 'prompt_sent') {
    // The server took it but nothing was there to receive it: keep the text
    // and say so, rather than letting it read as delivered.
    if (!window.AgyFormat.promptWasDelivered(data && data.delivered_via)) {
      if (!promptInput.value) {
        promptInput.value = (data && data.prompt) || '';
        autoResizeInput();
      }
      reportUndelivered('Nothing received it — no agent session is attached.');
    }
  } else if (type === 'session_renamed') {
    // An agent that names a session after its first exchange leaves the
    // header on a placeholder for good without this.
    if (data.conversation_id === currentConversationId) {
      currentConversation = data.conversation || currentConversation;
      updateHeader();
    }
  } else if (type === 'step_added' || type === 'step_updated') {
    currentConversationId = window.AgyFormat.adoptConversationId(currentConversationId, data.conversation_id);
    // A step is activity, whatever the drawer's snapshot says: remember it so
    // the row's dot can go busy before the next /api/conversations refresh.
    if (data.conversation_id) {
      lastSightings[data.conversation_id] = Date.now();
      updateSessionDots();
    }
    // `step_updated` used to fall through to nothing. A backend that revises
    // a step as its text, tools and thinking arrive would leave an empty card
    // on screen until a session switch reloaded the transcript.
    if (data.conversation_id === currentConversationId) {
      const applied = window.AgyFormat.applyStepUpdate(currentSteps, data.step);
      const isNew = applied.index === currentSteps.length;
      currentSteps = applied.steps;
      if (isNew) {
        chatContainer.appendChild(stepNodes(data.step));
      } else {
        replaceStep(data.step);
      }
      if (autoScroll) scrollToBottom();
    }
  } else if (type === 'terminal_screen') {
    applyTerminal(data);
  } else if (type === 'approval_request') {
    // Auto-accept, when the operator switched it on: the gate is answered
    // allow the instant it arrives, no banner, no buzz-fit. The id guard
    // keeps a re-broadcast from answering twice.
    if (window.AgyFormat.autoAcceptDecision(autoAccept, data) && !autoAcceptedIds.has(data.id)) {
      autoAcceptedIds.add(data.id);
      respondApproval(data.id, 'allow');
      triggerVibrate([40, 30, 40]);
      statusText.textContent = `Auto-accepted ${data.tool_name || 'tool'}`;
      return;
    }
    // Every session's approvals arrive here so nothing is missed, but a banner
    // belongs to the session that raised it: drawn into another transcript it
    // reads as belonging to the work in front of you. Elsewhere it counts.
    if (!pendingApprovals.some(a => a.id === data.id)) {
      pendingApprovals.push(data);
      triggerVibrate([80, 40, 100]);
      if (data.conversation_id === currentConversationId) {
        renderApprovalBanner(data);
        scrollToBottom();
      }
      updateApprovalIndicators();
    }
  } else if (type === 'frame_rejected') {
    // The server threw our frame away. Without this the prompt just vanished.
    reportUndelivered(`Server rejected the message: ${data && data.reason ? data.reason : 'unknown reason'}`);
  } else if (type === 'approval_resolved') {
    pendingApprovals = pendingApprovals.filter(a => a.id !== data.id);
    updateApprovalIndicators();
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
  currentSteps.forEach(step => chatContainer.appendChild(stepNodes(step)));
  window.AgyFormat.approvalsForSession(pendingApprovals, currentConversationId).forEach(renderApprovalBanner);
  scrollToBottom();
}

// Append a Single Step
// The nodes one step draws, tagged with its id. A step can render more than
// one element, and a revision has to replace every one of them.
function stepNodes(step) {
  const frag = document.createDocumentFragment();
  appendStep(step, frag);
  Array.from(frag.children).forEach(node => {
    node.dataset.stepId = step.id || '';
  });
  return frag;
}

// Redraw a step already on screen: for a backend that fills a message in after
// creating it empty, this is what carries the actual answer.
//
// Rebuilding the node while the reader drags a selection across it throws the
// selection away -- and a streaming step redraws every few hundred characters,
// which made long answers nearly impossible to select on a phone. While a
// selection is live inside the transcript, redraws are held and applied the
// moment the reader lets go; the newest content of each held step wins.
let heldStepUpdates = null;

function selectionHoldsTranscript() {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return false;
  return chatContainer.contains(sel.anchorNode) && chatContainer.contains(sel.focusNode);
}

function flushHeldSteps() {
  if (!heldStepUpdates || selectionHoldsTranscript()) return;
  const held = heldStepUpdates;
  heldStepUpdates = null;
  held.forEach(applyReplaceStep);
}

document.addEventListener('selectionchange', flushHeldSteps);

function replaceStep(step) {
  if (!step.id) return;
  if (selectionHoldsTranscript()) {
    heldStepUpdates = heldStepUpdates || new Map();
    heldStepUpdates.set(step.id, step);
    return;
  }
  applyReplaceStep(step);
}

function applyReplaceStep(step) {
  const existing = Array.from(chatContainer.children).filter(n => n.dataset.stepId === step.id);
  const nodes = stepNodes(step);
  if (existing.length === 0) {
    chatContainer.appendChild(nodes);
    return;
  }
  chatContainer.insertBefore(nodes, existing[0]);
  existing.forEach(node => node.remove());
}

function appendStep(step, target) {
  target = target || chatContainer;
  const stepType = step.type || 'UNKNOWN';
  const source = step.source || 'UNKNOWN';

  if (stepType === 'USER_INPUT' || source === 'USER_INPUT' || source === 'USER_EXPLICIT') {
    const env = window.AgyFormat.parseEnvelope(step.content);
    if (env && env.isEnvelope) {
      const envelopeDiv = document.createElement('div');
      envelopeDiv.className = 'message-agent-envelope';
      envelopeDiv.innerHTML = `
        <div class="agent-envelope-header">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>
          <span>Message from <strong>${escapeHtml(env.from)}</strong></span>
        </div>
        <div class="agent-envelope-body">${escapeHtml(env.text)}</div>
      `;
      target.appendChild(envelopeDiv);
      return;
    }

    const userDiv = document.createElement('div');
    userDiv.className = 'message-user';
    userDiv.textContent = step.content || '';
    target.appendChild(userDiv);
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
      renderMermaidIn(textDiv);

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

    target.appendChild(modelDiv);
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
    target.appendChild(collapsed(label, body, 'message-system'));
    return;
  }

  const systemDiv = document.createElement('div');
  systemDiv.className = 'message-system';
  systemDiv.textContent = label;
  target.appendChild(systemDiv);
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

// Auto-accept: the operator's switch, persisted on the device. On, every
// arriving approval is answered allow the moment it arrives (see the
// `approval_request` branch); the id set keeps a re-broadcast from deciding
// twice. A tool called `rm -rf /` does not care which device answered it,
// which is exactly why the switch lives behind an explicit toggle and not a
// one-tap banner button.
let autoAccept = false;
try {
  autoAccept = localStorage.getItem('agy-auto-accept') === '1';
} catch (e) { /* private mode: the switch just does not persist */ }
const autoAcceptedIds = new Set();

const autoAcceptBtn = document.getElementById('autoAcceptBtn');

function applyAutoAcceptUI() {
  if (!autoAcceptBtn) return;
  autoAcceptBtn.classList.toggle('active', autoAccept);
  autoAcceptBtn.title = autoAccept
    ? 'Auto-accept is ON: tool approvals are allowed without asking. Tap to turn off.'
    : 'Auto-accept is OFF: every tool approval asks. Tap to allow all without asking.';
}

if (autoAcceptBtn) {
  autoAcceptBtn.addEventListener('click', () => {
    autoAccept = !autoAccept;
    try {
      localStorage.setItem('agy-auto-accept', autoAccept ? '1' : '0');
    } catch (e) { /* still works for this page load */ }
    applyAutoAcceptUI();
    statusText.textContent = autoAccept ? 'Auto-accept ON' : 'Auto-accept OFF';
  });
  applyAutoAcceptUI();
}

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

  // agy has no "approve future matching requests", so there is no Always.
  const alwaysBtn = '';

  // A banner is only ever drawn in the session that raised it, so it needs no
  // attribution. One that slipped through anyway says where it belongs rather
  // than borrowing the transcript around it.
  const origin = window.AgyFormat.approvalOrigin(app, currentConversationId);
  if (origin) banner.classList.add('approval-elsewhere');
  const originRow = origin
    ? `<div class="approval-origin">in another session: ${escapeHtml(origin)}</div>`
    : '';

  banner.innerHTML = `
    <div class="approval-title">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      Permission Required: ${escapeHtml(app.tool_name)}
    </div>
    ${originRow}
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
  const fileChip = e.target.closest('.file-ref-chip');
  if (fileChip && fileChip.dataset.filePath) {
    openFileViewer(fileChip.dataset.filePath);
    return;
  }
  const btn = e.target.closest('[data-approval-id]');
  if (btn) {
    respondApproval(btn.dataset.approvalId, btn.dataset.decision);
  }
});

// -- file viewer -------------------------------------------------------------
//
// The transcript references host files as [file:///...]; the server reads them
// for the phone within its sanctioned roots (GET /api/file, same token as
// everything else). A chip opens this overlay; nothing is fetched until then.

let fileViewerTicket = 0;

async function openFileViewer(path) {
  const ticket = ++fileViewerTicket;
  fileViewer.hidden = false;
  fileViewerBackdrop.hidden = false;
  requestAnimationFrame(() => {
    fileViewer.classList.add('open');
    fileViewerBackdrop.classList.add('open');
  });
  fileViewerName.textContent = path.slice(path.lastIndexOf('/') + 1) || path;
  fileViewerPath.textContent = path;
  fileViewerBody.textContent = 'Loading…';
  try {
    const res = await fetch(`/api/file?path=${encodeURIComponent(path)}&token=${encodeURIComponent(authToken)}`);
    if (ticket !== fileViewerTicket) return;
    const data = await res.json();
    if (!res.ok) {
      fileViewerBody.textContent = data && data.detail
        ? `Could not read file: ${data.detail}`
        : `Could not read file (HTTP ${res.status})`;
      return;
    }
    fileViewerName.textContent = data.name;
    fileViewerPath.textContent = data.truncated ? `${data.path} · truncated` : data.path;
    fileViewerBody.textContent = data.content;
  } catch (err) {
    if (ticket === fileViewerTicket) fileViewerBody.textContent = 'Could not reach the server.';
  }
}

function closeFileViewer() {
  fileViewerTicket++;
  fileViewer.classList.remove('open');
  fileViewerBackdrop.classList.remove('open');
  fileViewer.hidden = true;
  fileViewerBackdrop.hidden = true;
}

if (fileViewerClose) fileViewerClose.addEventListener('click', closeFileViewer);
if (fileViewerBackdrop) fileViewerBackdrop.addEventListener('click', closeFileViewer);

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

  // Name the session on screen: the active one moves on its own now that the
  // phone follows the desktop, so an unaddressed prompt can land in a session
  // its author will never see.
  const payload = {
    action: 'send_prompt',
    data: { prompt: prompt, conversation_id: currentConversationId }
  };

  let delivered = false;
  if (window.AgyFormat.promptRoute(ws && ws.readyState) === 'socket') {
    try {
      const msg = cryptoKey ? await encryptData(payload) : payload;
      ws.send(JSON.stringify(msg));
      delivered = true;
    } catch (e) {
      delivered = false;
    }
  }

  if (!delivered) {
    delivered = await sendPromptOverRest(prompt);
  }

  if (!delivered) {
    // Keep the text: losing it is worse than a stale draft in the box.
    promptInput.value = prompt;
    autoResizeInput();
    reportUndelivered('Not sent — no connection to the server.');
    return;
  }

  promptInput.value = '';
  autoResizeInput();
  scrollToBottom();
}

// The same server, over a request that reports its own failure. Used when the
// socket is not open, and as the fallback when writing to it throws. Sealed
// with the same envelope as the socket when a key is loaded -- this path
// exists for a dead socket, not for stepping around payload encryption, and
// the server refuses a bare body under E2EE for exactly that reason. The
// token rides in a header, not the query string.
async function sendPromptOverRest(prompt) {
  try {
    const payload = { prompt: prompt, conversation_id: currentConversationId };
    const body = cryptoKey ? await encryptData(payload) : payload;
    const res = await fetch('/api/prompt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': authToken },
      body: JSON.stringify(body)
    });
    return res.ok;
  } catch (e) {
    return false;
  }
}

// A prompt the server refused, or one that never reached it, has to say so --
// silence reads exactly like success.
function reportUndelivered(reason) {
  statusText.textContent = reason;
  triggerVibrate([40, 60, 40]);
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
  // Grow with the text, bounded by the viewport rather than a fixed pixel cap:
  // the chat above is `flex: 1` and yields as this grows, and the keyboard is
  // what actually limits how much room there is.
  const cap = Math.max(120, Math.round(window.innerHeight * 0.45));
  promptInput.style.height = 'auto';
  promptInput.style.height = Math.min(Math.max(promptInput.scrollHeight, 44), cap) + 'px';
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
  // Mermaid fences become placeholders instead: the diagram renderer takes
  // the raw text and produces an SVG (see renderMermaidIn).
  let out = String(text).replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
    const cleanCode = code.replace(/\n$/, '');
    if (window.AgyFormat.isMermaidLang(lang)) {
      const i = codeBlocks.push(`<div class="md-mermaid" data-diagram>${escapeHtml(cleanCode)}</div>`) - 1;
      return `\u0000CODE${i}\u0000`;
    }
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

  // File references -- [name](file:///abs/path), [file:///abs/path] or a
  // bare file:///abs/path -- become chips that open the file viewer. The
  // matcher lives in AgyFormat (parseFileRefs' rendering twin), so finding
  // and drawing can never drift apart. Built after escaping, like every
  // other markup here; the path is stashed in a data attribute (never an
  // href), so nothing is fetched until a chip is tapped, and the server
  // still refuses anything outside its roots.
  out = window.AgyFormat.replaceFileRefs(out, (ref) => {
    const path = ref.path.replace(/&amp;/g, '&');
    const name = ref.name.replace(/&amp;/g, '&');
    return `<button class="file-ref-chip" type="button" data-file-path="${escapeHtml(path)}">` +
      `${escapeHtml(name)}</button>`;
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

// -- mermaid -----------------------------------------------------------------
//
// agy is fond of ```mermaid fences; on the desktop they render, on the phone
// they were a wall of DSL. The bundle is vendored (the CSP permits no remote
// script origins, and this PWA must work air-gapped) and runs at
// securityLevel 'strict' -- it parses transcript text, which is
// attacker-influenceable, so its built-in sanitization is not optional.

let mermaidInitialized = false;
let mermaidSeq = 0;

function ensureMermaid() {
  if (!window.mermaid) return null;
  if (!mermaidInitialized) {
    window.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark' });
    mermaidInitialized = true;
  }
  return window.mermaid;
}

async function renderMermaidIn(root) {
  const mermaid = ensureMermaid();
  if (!mermaid) return;
  const nodes = root.querySelectorAll('.md-mermaid[data-diagram]');
  for (const node of nodes) {
    node.removeAttribute('data-diagram'); // claimed: a rerender must not redraw it
    const code = node.textContent;
    try {
      const { svg } = await mermaid.render(`mmd-${++mermaidSeq}`, code);
      // The node may have been detached while the diagram rendered -- a
      // rerender or a switch replaced it mid-await.
      if (node.isConnected) node.innerHTML = svg;
    } catch (err) {
      console.warn('Mermaid render failed:', err);
      if (node.isConnected) {
        node.classList.add('md-mermaid-failed');
        node.innerHTML = `<pre class="md-code"><code>${escapeHtml(code)}</code></pre>`;
      }
    }
  }
}

// Diagram pinch-zoom: a two-finger pinch scales the SVG (width, so the
// container's native scroll pans it), a double-tap resets. One finger keeps
// scrolling normally -- only the two-finger gesture is claimed.
const DIAGRAM_ZOOM_MIN = 1;
const DIAGRAM_ZOOM_MAX = 5;
const DIAGRAM_DOUBLE_TAP_MS = 300;

let diagramPinchStartDist = 0;
let diagramPinchStartScale = 1;
let diagramLastTap = { at: null, el: null };

function diagramTouchDist(touches) {
  const dx = touches[0].clientX - touches[1].clientX;
  const dy = touches[0].clientY - touches[1].clientY;
  return Math.hypot(dx, dy);
}

function setDiagramZoom(container, scale) {
  const svg = container.querySelector('svg');
  if (!svg) return;
  const next = AgyFormat.clampZoom(scale, DIAGRAM_ZOOM_MIN, DIAGRAM_ZOOM_MAX);
  container.dataset.zoom = String(next);
  if (next === 1) {
    delete container.dataset.baseWidth;
    svg.style.maxWidth = '';
    svg.style.width = '';
    return;
  }
  if (!container.dataset.baseWidth) {
    // The natural width, captured before any transform touched it.
    container.dataset.baseWidth = String(container.clientWidth);
  }
  svg.style.maxWidth = 'none';
  svg.style.width = Number(container.dataset.baseWidth) * next + 'px';
}

function resetDiagramZoom(container) {
  setDiagramZoom(container, 1);
}

chatContainer.addEventListener('touchstart', (e) => {
  const container = e.target.closest('.md-mermaid');
  if (!container) return;
  if (e.touches.length === 2) {
    diagramPinchStartDist = diagramTouchDist(e.touches);
    diagramPinchStartScale = parseFloat(container.dataset.zoom || '1');
  }
}, { passive: true });

chatContainer.addEventListener('touchmove', (e) => {
  const container = e.target.closest('.md-mermaid');
  if (!container || e.touches.length !== 2 || diagramPinchStartDist <= 0) return;
  // Ours now: without this the page zooms instead of the diagram.
  e.preventDefault();
  const factor = diagramTouchDist(e.touches) / diagramPinchStartDist;
  setDiagramZoom(container, diagramPinchStartScale * factor);
}, { passive: false });

chatContainer.addEventListener('touchend', (e) => {
  const container = e.target.closest('.md-mermaid');
  if (!container) return;
  if (e.touches.length > 0) return; // a finger is still down: the pinch continues
  if (diagramPinchStartDist > 0) {
    diagramPinchStartDist = 0; // the pinch just ended; not a tap
    return;
  }
  // Trackpad/pointer pinch is not the only zoom; a double-tap resets.
  const now = Date.now();
  if (diagramLastTap.el === container && AgyFormat.isDoubleTap(diagramLastTap.at, now, DIAGRAM_DOUBLE_TAP_MS)) {
    resetDiagramZoom(container);
    diagramLastTap = { at: null, el: null };
    return;
  }
  diagramLastTap = { at: now, el: container };
}, { passive: true });

// Desktop/trackpad equivalent: ctrl+wheel (the pinch gesture on a Mac
// trackpad) zooms the hovered diagram, plain scroll stays untouched.
chatContainer.addEventListener('wheel', (e) => {
  if (!e.ctrlKey) return;
  const container = e.target.closest('.md-mermaid');
  if (!container) return;
  e.preventDefault();
  const current = parseFloat(container.dataset.zoom || '1');
  setDiagramZoom(container, current * (e.deltaY < 0 ? 1.1 : 0.9));
}, { passive: false });

function triggerVibrate(pattern = [60, 40, 80]) {
  if (navigator.vibrate) {
    try { navigator.vibrate(pattern); } catch (e) {}
  }
}

// How many devices are on this server. Nothing here is per-device: one token,
// one key, no way to revoke a single phone -- so a count you did not expect is
// the only sign the pairing URL has escaped. Alert on it the first time it
// changes, then leave it in the header.
let peerCount = 1;

// Where the waiting requests are. Every session's approvals reach this client
// -- an agy blocked in another window has to be answerable -- but they are
// shown where they belong: a banner in their own transcript, a count on their
// row in the drawer, and one badge in the header saying to go look.
function updateApprovalIndicators() {
  const elsewhere = window.AgyFormat.approvalsElsewhere(pendingApprovals, currentConversationId);

  const badge = document.getElementById('approvalsElsewhereBadge');
  if (badge) {
    badge.textContent = elsewhere.count
      ? `${elsewhere.count} waiting · ${elsewhere.label}`
      : '';
    badge.hidden = !elsewhere.count;
  }

  const counts = window.AgyFormat.approvalCountsBySession(pendingApprovals);
  document.querySelectorAll('.session-item[data-conversation-id]').forEach(item => {
    const waiting = counts[item.dataset.conversationId] || 0;
    let dot = item.querySelector('.session-waiting');
    if (!waiting) {
      if (dot) dot.remove();
      return;
    }
    if (!dot) {
      dot = document.createElement('span');
      dot.className = 'session-waiting';
      item.querySelector('.session-item-title')?.appendChild(dot);
    }
    dot.textContent = waiting > 1 ? `${waiting} waiting` : 'waiting';
  });
  updateSessionDots();
}

function applyPeerCount(count) {
  const badge = document.getElementById('peerBadge');
  const notice = window.AgyFormat.peerNotice(count);
  if (badge) {
    badge.textContent = notice || '';
    badge.hidden = !notice;
  }
  if (notice && typeof count === 'number' && count > peerCount) {
    triggerVibrate([60, 50, 60, 50, 60]);
    statusText.textContent = notice;
  }
  if (typeof count === 'number') peerCount = count;
}

function applyAgentIdentity() {
  const { label, title } = window.AgyFormat.agentIdentity(currentAgent);
  const el = document.getElementById('agentName');
  if (el) el.textContent = label;
  document.title = title;
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

async function toggleMailboxMute(a, b, currentlyMuted) {
  const method = currentlyMuted ? 'DELETE' : 'POST';
  const url = `/api/mailbox/mute${authToken ? `?token=${encodeURIComponent(authToken)}` : ''}`;
  try {
    const res = await fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ a, b })
    });
    if (res.ok) {
      const data = await res.json();
      const existing = agentTraffic.find(p => (p.a === a && p.b === b) || (p.a === b && p.b === a));
      if (existing) {
        existing.muted = !currentlyMuted;
      }
      renderConversations(drawerConversations);
    }
  } catch (err) {
    console.error('Failed to toggle mailbox mute:', err);
  }
}

function renderConversations(convs) {
  drawerConversations = convs;
  drawerList.innerHTML = '';
  convs.forEach(c => {
    const item = document.createElement('div');
    item.className = `session-item ${c.id === currentConversationId ? 'active' : ''}`;
    item.dataset.conversationId = c.id;
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

    // Agent mailbox traffic badges (W2, item 2.3)
    const matchingPairs = [];
    const keysToCheck = [c.id, c.title, c.tmux_name].filter(Boolean);
    keysToCheck.forEach(k => {
      const pairs = window.AgyFormat.trafficForSession(agentTraffic, k);
      pairs.forEach(p => {
        if (!matchingPairs.some(mp => (mp.a === p.a && mp.b === p.b) || (mp.a === p.b && mp.b === p.a))) {
          matchingPairs.push(p);
        }
      });
    });

    if (matchingPairs.length > 0) {
      const trafficContainer = document.createElement('div');
      trafficContainer.className = 'session-item-traffic';
      matchingPairs.forEach(pair => {
        const pillData = window.AgyFormat.formatTrafficPill(pair, c.id) ||
                         window.AgyFormat.formatTrafficPill(pair, c.title);
        if (!pillData) return;
        const pill = document.createElement('span');
        pill.className = `traffic-pill ${pillData.state}`;
        pill.title = pillData.muted
          ? 'Mailbox muted. Tap to unmute.'
          : (pillData.looping ? 'Mailbox looping paused. Tap to mute.' : 'Agent mailbox active. Tap to mute.');
        pill.textContent = `${pillData.label}${pillData.muted ? ' (muted)' : (pillData.looping ? ' (looping)' : '')}`;
        pill.onclick = (e) => {
          e.stopPropagation();
          toggleMailboxMute(pair.a, pair.b, pair.muted);
        };
        trafficContainer.appendChild(pill);
      });
      item.appendChild(trafficContainer);
    }

    drawerList.appendChild(item);
  });
  updateApprovalIndicators();
  updateSessionDots();
}

// -- session status dots (W1, item 1.4) --
//
// One rule, drawn where it belongs: the drawer row. The rule is the shared
// `AgySessions.sessionStatus` applied to what the server sent (the
// transcript's `updated_at`) and what this client has seen since the drawer's
// snapshot (a step that arrived in the meantime); the fresher wins. The
// drawer is static markup, so while it is open a tick keeps the dot honest:
// a session that goes quiet has to settle busy -> active/idle on its own.
let drawerConversations = [];
let lastSightings = {};
let drawerDotTimer = null;
const DOT_TITLES = {
  approval: 'Waiting for approval',
  busy: 'Working',
  active: 'Active session',
  idle: 'Idle'
};

function updateSessionDots() {
  const counts = window.AgyFormat.approvalCountsBySession(pendingApprovals);
  const now = Date.now();
  const byId = {};
  drawerConversations.forEach(c => { byId[c.id] = c; });
  document.querySelectorAll('.session-item[data-conversation-id]').forEach(item => {
    const conv = byId[item.dataset.conversationId];
    if (!conv) return;
    const title = item.querySelector('.session-item-title');
    if (!title) return;
    const status = window.AgySessions.sessionStatusOf(
      conv,
      currentConversationId,
      counts[conv.id] || 0,
      now,
      lastSightings[conv.id] || null
    );
    let dot = title.querySelector('.session-dot');
    if (!dot) {
      dot = document.createElement('span');
      title.prepend(dot);
    }
    dot.className = `session-dot status-${status}`;
    dot.title = DOT_TITLES[status] || '';
  });
}

function startDrawerDotTimer() {
  if (drawerDotTimer !== null) clearInterval(drawerDotTimer);
  drawerDotTimer = setInterval(updateSessionDots, 5000);
}

function stopDrawerDotTimer() {
  if (drawerDotTimer !== null) {
    clearInterval(drawerDotTimer);
    drawerDotTimer = null;
  }
}

async function openDrawer() {
  drawer.classList.add('open');
  drawerBackdrop.classList.add('open');
  updateSessionDots();
  startDrawerDotTimer();
  try {
    const res = await fetch(`/api/conversations?token=${encodeURIComponent(authToken)}`);
    if (res.ok) {
      const convs = await res.json();
      renderConversations(convs);
    }
  } catch (e) {
    console.debug('Failed refreshing conversations in drawer:', e);
  }
}

function closeDrawer() {
  drawer.classList.remove('open');
  drawerBackdrop.classList.remove('open');
  stopDrawerDotTimer();
}

// ----------------------------------------------------------------------------
// New Session Sheet (W1, item 1.3)
//
// The sheet asks; the server does. It POSTs the form and then waits on the
// `session_spawning` / `session_created` events, which name the spawn they
// belong to -- only the spawn this sheet sent may drive its chip, so two
// phones on one server do not walk each other's progress.
// ----------------------------------------------------------------------------
const newSessionBtn = document.getElementById('newSessionBtn');
const newSessionSheet = document.getElementById('newSessionSheet');
const newSessionBackdrop = document.getElementById('newSessionBackdrop');
const newSessionRepo = document.getElementById('newSessionRepo');
const newSessionBranch = document.getElementById('newSessionBranch');
const newSessionTask = document.getElementById('newSessionTask');
const newSessionName = document.getElementById('newSessionName');
const newSessionProgress = document.getElementById('newSessionProgress');
const newSessionStage = document.getElementById('newSessionStage');
const newSessionError = document.getElementById('newSessionError');
const newSessionStartBtn = document.getElementById('newSessionStartBtn');
const newSessionCancelBtn = document.getElementById('newSessionCancelBtn');
const newSessionCloseBtn = document.getElementById('newSessionCloseBtn');

// The spawn this sheet sent, as the server named it (name, workdir,
// tmux_session), or null while the sheet is idle.
let activeSpawn = null;

function setSheetBusy(busy) {
  [newSessionRepo, newSessionBranch, newSessionTask, newSessionName, newSessionStartBtn, newSessionCancelBtn]
    .forEach(el => { el.disabled = busy; });
  newSessionProgress.hidden = !busy;
  if (!busy) {
    activeSpawn = null;
    newSessionError.hidden = true;
  }
}

function openNewSessionSheet() {
  closeDrawer();
  resetNewSessionSheet();
  newSessionSheet.hidden = false;
  newSessionBackdrop.classList.add('open');
  // The slide starts next frame, from below the viewport, once it is drawn.
  requestAnimationFrame(() => newSessionSheet.classList.add('open'));
  newSessionRepo.focus();
}

function closeNewSessionSheet() {
  if (activeSpawn) return; // a running spawn keeps the sheet until it settles
  newSessionSheet.classList.remove('open');
  newSessionBackdrop.classList.remove('open');
  setTimeout(() => {
    newSessionSheet.hidden = true;
    resetNewSessionSheet();
  }, 220);
}

function resetNewSessionSheet() {
  [newSessionRepo, newSessionBranch, newSessionName].forEach(el => { el.value = ''; });
  newSessionTask.value = '';
  newSessionStage.textContent = '';
  setSheetBusy(false);
}

function showSpawnFailure(message) {
  setSheetBusy(false);
  newSessionError.textContent = message;
  newSessionError.hidden = false;
}

async function startNewSession() {
  const built = window.AgySessions.buildSpawnRequest({
    repoUrl: newSessionRepo.value,
    branch: newSessionBranch.value,
    task: newSessionTask.value,
    name: newSessionName.value,
  });
  if (!built.ok) {
    newSessionError.textContent = built.error;
    newSessionError.hidden = false;
    return;
  }

  newSessionError.hidden = true;
  setSheetBusy(true);
  newSessionStage.textContent = 'Sending…';

  try {
    // Sealed like the prompts: the task is content the server would never
    // want on the wire in the clear. The 202 answer is plain.
    const body = cryptoKey ? await encryptData(built.payload) : built.payload;
    const res = await fetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Auth-Token': authToken },
      body: JSON.stringify(body)
    });
    if (!res.ok) {
      let detail = `The server refused the spawn (HTTP ${res.status}).`;
      try {
        const data = await res.json();
        if (data && data.detail) detail = data.detail;
      } catch (e) {
        // The body was not JSON; the status line is the answer.
      }
      showSpawnFailure(detail);
      return;
    }
    activeSpawn = await res.json();
    newSessionStage.textContent = 'Cloning…';
  } catch (e) {
    showSpawnFailure('Could not reach the server to start the session.');
  }
}

// `session_created` for our spawn: the session exists and is being supervised.
// Its conversation appears the moment agy first speaks, and the server binds
// and switches to it then -- so the sheet's job is done; show Ready and let
// it go.
function finishSpawn(data) {
  newSessionStage.textContent = data && data.ready === false
    ? 'Started — still settling'
    : 'Ready';
  setSheetBusy(false);
  setTimeout(closeNewSessionSheet, 1400);
}

newSessionBtn.addEventListener('click', openNewSessionSheet);
newSessionStartBtn.addEventListener('click', startNewSession);
newSessionCancelBtn.addEventListener('click', closeNewSessionSheet);
newSessionCloseBtn.addEventListener('click', closeNewSessionSheet);
newSessionBackdrop.addEventListener('click', closeNewSessionSheet);
newSessionTask.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    startNewSession();
  }
});

// ----------------------------------------------------------------------------
// Slash-Command Menu
//
// "/" sits behind a long-press on iOS and a symbol layer elsewhere, so the
// commands live in a drill-down sheet instead: categories first, then
// commands with a line of what each does. The tree itself is AgyCommands
// (commands.js); this section only renders it and acts on a tap:
//
//   plain command   -> sent as a prompt, like the chips do
//   panel command   -> sent, and the terminal mirror opens, because
//                      /model /permissions /resume answer in a transient TUI
//                      panel the transcript never sees
//   argument command-> the composer is prefilled (`/rename `), and only the
//                      argument needs typing
// ----------------------------------------------------------------------------
const cmdMenuBtn = document.getElementById('cmdMenuBtn');
const cmdMenuSheet = document.getElementById('cmdMenuSheet');
const cmdMenuBackdrop = document.getElementById('cmdMenuBackdrop');
const cmdMenuTitle = document.getElementById('cmdMenuTitle');
const cmdMenuBody = document.getElementById('cmdMenuBody');
const cmdMenuBackBtn = document.getElementById('cmdMenuBackBtn');
const cmdMenuCloseBtn = document.getElementById('cmdMenuCloseBtn');

let cmdMenuCategory = null;

function openCmdMenu() {
  closeDrawer();
  cmdMenuCategory = null;
  renderCmdMenu();
  cmdMenuSheet.hidden = false;
  cmdMenuBackdrop.classList.add('open');
  requestAnimationFrame(() => cmdMenuSheet.classList.add('open'));
}

function closeCmdMenu() {
  cmdMenuSheet.classList.remove('open');
  cmdMenuBackdrop.classList.remove('open');
  setTimeout(() => {
    cmdMenuSheet.hidden = true;
    cmdMenuCategory = null;
    renderCmdMenu();
  }, 220);
}

function renderCmdMenu() {
  const categories = window.AgyCommands.categories();
  if (cmdMenuCategory === null) {
    cmdMenuTitle.textContent = 'Slash commands';
    cmdMenuBackBtn.hidden = true;
    cmdMenuBody.innerHTML = '';
    for (const cat of categories) {
      const row = document.createElement('button');
      row.className = 'cmd-row';
      row.innerHTML = `<span class="cmd-row-main"><span class="cmd-cat-title">${cat.title}</span>` +
        `<span class="cmd-cat-count">${cat.commands.length} commands</span></span>` +
        '<span class="cmd-chevron">&#8250;</span>';
      row.addEventListener('click', () => {
        cmdMenuCategory = cat.id;
        renderCmdMenu();
      });
      cmdMenuBody.appendChild(row);
    }
    return;
  }
  const cat = categories.find((c) => c.id === cmdMenuCategory);
  if (!cat) { cmdMenuCategory = null; renderCmdMenu(); return; }
  cmdMenuTitle.textContent = cat.title;
  cmdMenuBackBtn.hidden = false;
  cmdMenuBody.innerHTML = '';
  for (const cmd of cat.commands) {
    const row = document.createElement('button');
    row.className = 'cmd-row';
    const flags = [
      cmd.panel ? '<span class="cmd-flag" title="Answers in a terminal panel — the Screen mirror opens with it">panel</span>' : '',
      cmd.arg ? '<span class="cmd-flag" title="Needs an argument — the composer is prefilled">arg</span>' : '',
    ].join('');
    row.innerHTML = `<span class="cmd-row-main"><span class="cmd-name">${cmd.name}</span>` +
      `<span class="cmd-desc">${cmd.desc}</span></span><span class="cmd-flags">${flags}</span>`;
    row.addEventListener('click', () => runCommand(cmd));
    cmdMenuBody.appendChild(row);
  }
}

function runCommand(cmd) {
  closeCmdMenu();
  if (cmd.arg) {
    // Only the argument is typed: the command word itself came from the menu.
    promptInput.value = `${cmd.name} `;
    autoResizeInput();
    promptInput.focus();
    return;
  }
  if (cmd.panel && terminalPanel.hidden) toggleTerminal();
  sendPrompt(cmd.name);
}

cmdMenuBtn.addEventListener('click', openCmdMenu);
cmdMenuCloseBtn.addEventListener('click', closeCmdMenu);
cmdMenuBackdrop.addEventListener('click', closeCmdMenu);
cmdMenuBackBtn.addEventListener('click', () => {
  cmdMenuCategory = null;
  renderCmdMenu();
});

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

const elsewhereBadge = document.getElementById('approvalsElsewhereBadge');
if (elsewhereBadge) {
  elsewhereBadge.addEventListener('click', () => {
    const elsewhere = window.AgyFormat.approvalsElsewhere(pendingApprovals, currentConversationId);
    if (elsewhere.sessions && elsewhere.sessions.length === 1) {
      const targetId = elsewhere.sessions[0];
      const payload = {
        action: 'switch_conversation',
        data: { conversation_id: targetId }
      };
      if (ws && ws.readyState === WebSocket.OPEN) {
        if (cryptoKey) {
          encryptData(payload).then(msg => ws.send(JSON.stringify(msg)));
        } else {
          ws.send(JSON.stringify(payload));
        }
      }
    } else {
      openDrawer();
    }
  });
}
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

// Stop the turn: Esc halts agy's active stream. It is the safe interrupt --
// Ctrl+C ("interrupt") is the key agy maps to *exit*, and a phone that
// mistypes its way out of an agent session is a lost session.
const stopBtn = document.getElementById('stopBtn');
if (stopBtn) stopBtn.addEventListener('click', () => sendKey('escape'));

// Register Service Worker
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(err => {
    console.debug('ServiceWorker registration skipped:', err);
  });
}

// Navigation and Focus Support
async function switchConversation(id) {
  if (!id || id === currentConversationId) return;
  const payload = {
    action: 'switch_conversation',
    data: { conversation_id: id }
  };
  if (ws && ws.readyState === WebSocket.OPEN) {
    const msg = cryptoKey ? await encryptData(payload) : payload;
    ws.send(JSON.stringify(msg));
  }
}

function highlightApproval(approvalId) {
  if (!approvalId) return;
  setTimeout(() => {
    const elem = document.getElementById(`approval-${approvalId}`);
    if (elem) {
      elem.scrollIntoView({ behavior: 'smooth', block: 'center' });
      elem.classList.add('highlight-pulse');
      setTimeout(() => elem.classList.remove('highlight-pulse'), 3000);
    }
  }, 200);
}

function checkUrlNavigation() {
  const hash = window.location.hash;
  const search = window.location.search;
  const hashParams = new URLSearchParams(hash.replace(/^#/, ''));
  const searchParams = new URLSearchParams(search);

  const targetSession = hashParams.get('session') || searchParams.get('session');
  const targetFocus = hashParams.get('focus') || searchParams.get('focus');

  if (targetSession && targetSession !== currentConversationId) {
    switchConversation(targetSession);
  }
  if (targetFocus) {
    highlightApproval(targetFocus);
  }
}

function reportFocusState() {
  const isFocused = !document.hidden && document.hasFocus();
  if (ws && ws.readyState === WebSocket.OPEN) {
    const payload = {
      action: 'focus_state',
      data: {
        focused: isFocused,
        conversation_id: currentConversationId,
      }
    };
    if (cryptoKey) {
      encryptData(payload).then(msg => ws.send(JSON.stringify(msg)));
    } else {
      ws.send(JSON.stringify(payload));
    }
  }
}

window.addEventListener('focus', reportFocusState);
window.addEventListener('blur', reportFocusState);
document.addEventListener('visibilitychange', reportFocusState);
window.addEventListener('hashchange', checkUrlNavigation);

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'NAVIGATE') {
      const targetSession = event.data.session;
      const targetFocus = event.data.focus;
      if (targetSession && targetSession !== currentConversationId) {
        switchConversation(targetSession);
      }
      if (targetFocus) {
        highlightApproval(targetFocus);
      }
    }
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
