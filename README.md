# 🚀 agy-remote

> **Mobile Web Remote Controller & PWA for Antigravity CLI (`agy`)**  
> Access, monitor, and control your locally running `agy` sessions from your phone over Tailscale or Local Wi-Fi — with rich markdown, syntax-highlighted code, collapsible reasoning, one-tap tool approvals, and voice dictation.

---

## ✨ Features

- 📱 **Mobile PWA Interface**: Native mobile web app (installable to iOS / Android Home Screen) that wraps text and diffs cleanly without terminal ANSI artifacts.
- ⚡ **Real-time Streaming**: Mirrors reasoning/thinking traces, model output tokens, and tool call executions as they happen via WebSockets.
- 🛡️ **One-Tap Tool Permissions**: Forwards `PreToolUse` security prompts to your phone so you can `[Allow]` or `[Deny]` commands on the go.
- 🎙️ **Voice Dictation**: Tap the mic button to dictate prompts using mobile Web Speech recognition.
- 🔗 **Tailscale & LAN Ready**: Auto-detects your Tailscale IP and displays a pairing **ASCII QR Code** in your terminal on launch.
- 🔄 **Dual Control (Supervisor Mode)**: Keep using your local desktop terminal while your phone acts as an active second screen.

---

## 📦 Installation & Requirements

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) for package management

```bash
cd agy-remote
uv sync
```

---

## 🚀 Quickstart

### 1. Launch with Active Antigravity CLI (Supervisor Mode)
Run your regular `agy` commands through the `agy-remote` supervisor. You get your standard desktop terminal session plus a live mobile bridge:

```bash
uv run agy-remote run
```

### 2. Standalone Server Mode (Log Watcher)
If you already have `agy` running in another terminal window:

```bash
uv run agy-remote serve
```

Scan the printed QR code with your phone's camera to immediately pair over Tailscale or your local Wi-Fi.

### 3. Setup Remote Tool Approvals
To enable remote one-tap permission prompts (`[Allow]` / `[Deny]`) when `agy` executes tools:

```bash
uv run agy-remote setup-hooks
```

---

## 🔒 Security & Networking

- **Local Execution**: All agent processes, files, git branches, and MCP servers run 100% on your machine.
- **Tailscale Mesh**: Recommended for remote access outside the house without opening public ports.
- **Token Auth**: Every session generates a secure token (`?token=...`) preventing unauthorized LAN access.

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
