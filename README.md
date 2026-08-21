# 🚀 agy-remote

> **Self-Hosted, End-to-End Encrypted Mobile Web Remote & PWA for Antigravity CLI (`agy`)**  
> Access, monitor, and control your locally running `agy` sessions from your phone over Tailscale or Local Wi-Fi — with zero cloud lock-in, client-side AES-256-GCM encryption, self-hosted Web Push alerts, persistent `tmux` execution, collapsible reasoning, one-tap tool approvals, and voice dictation.

---

## ✨ Features

- 🔐 **End-to-End Encrypted (E2EE)**: 256-bit AES-GCM encryption with cryptographic keys shared via the URL hash fragment (`#key=...`). The key is strictly client-side and never sent to servers or proxies in HTTP headers.
- 🔔 **Self-Hosted Web Push Notifications**: Native iOS & Android lock-screen push alerts via local VAPID keys whenever `agy` needs tool approval or finishes a task.
- 🔄 **tmux Persistence**: Keep sessions running in the background across laptop sleep, screen locks, or closed terminals (`agy-remote run --tmux`).
- 📱 **Mobile PWA Interface**: Installable to your mobile Home Screen with responsive dark mode, collapsible thinking traces, and touch-friendly controls.
- 🛡️ **One-Tap Tool Permissions**: Forwards `PreToolUse` security prompts to your phone with haptic feedback to `[Allow]` or `[Deny]` commands.
- 📎 **Photo & Screenshot Upload**: Capture screenshots or camera photos directly from mobile into your workspace.
- 📝 **Visual Diff Viewer**: Interactive colored diffs for file edits.
- 🎙️ **Voice Dictation**: Dictate instructions into active prompts using mobile Web Speech recognition.
- 🔗 **Tailscale & LAN Ready**: Auto-detects Tailscale IP and prints an interactive **ASCII QR Code** in your terminal on launch.

---

## 📦 Installation & Requirements

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) for package management
- Optional: `tmux` for persistent background session management

```bash
cd agy-remote
uv sync
```

---

## 🚀 Quickstart

### 1. Launch with Persistent tmux Supervisor
Runs `agy` inside a background `tmux` session with simultaneous desktop and mobile control:

```bash
uv run agy-remote run --tmux
```

### 2. Launch with PTY Supervisor
Standard interactive dual-terminal/mobile mode:

```bash
uv run agy-remote run
```

### 3. Standalone Watcher Server
If `agy` is already running elsewhere:

```bash
uv run agy-remote serve
```

Scan the printed QR code with your phone's camera to immediately pair over Tailscale or your local Wi-Fi.

### 4. Setup Remote Tool Approvals
To enable remote one-tap permission prompts (`[Allow]` / `[Deny]`) when `agy` executes tools:

```bash
uv run agy-remote setup-hooks
```

---

## 🔒 Security Architecture

1. **100% Self-Hosted**: No third-party relay servers, cloud accounts, or external API dependencies.
2. **URL Hash E2EE**: The AES-GCM key lives in the browser's `#key=...` fragment and decrypts WebSocket events in the browser with Web Crypto API (`SubtleCrypto`).
3. **Tailscale Mesh**: Secure peer-to-peer WireGuard networking directly between your phone and your Mac.

---

## 🧪 Testing & Linting

```bash
uv run pytest
uv run ruff format .
uv run ruff check .
```

---

## 📄 License

MIT
