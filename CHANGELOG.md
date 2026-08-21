# Changelog

## v26.08.22.4 — Enter, not a line break

- **A prompt sent from the phone typed itself into `agy` and stopped there.**
  `PtySupervisor.inject_input` terminated the injected text with LF (`\n`).
  agy runs the tty in raw mode, where Enter is carriage return (`\r`) and LF is
  Ctrl-J — an "insert a line break" keystroke. The prompt appeared in agy's
  input box and was never submitted. The supervisor now strips any trailing
  newline and sends a single `\r`. The tmux path was already correct: it sends
  `Enter` through `tmux send-keys`, which is CR.

## v26.08.22.3 — Working approvals over HTTPS, and a fixed watcher

- **The PreToolUse hook posted `http://` to an HTTPS port.** The connection was
  refused, the hook fell back to `"ask"`, and no approval ever reached the
  phone. The server now publishes its own base URL — the MagicDNS name under
  TLS, since the certificate is issued for that name and `https://127.0.0.1`
  would fail verification.
- **The watcher loop burned a whole core.** `_watch_loop` called
  `get_latest_conversation_id()` every 0.3s, which fully parsed every
  `transcript.jsonl` in the brain directory: 0.45s of parsing across 637 MB,
  three times a second, forever. Measured 42–82% CPU on an idle server.
  Finding the newest conversation now uses mtimes alone, and summaries are
  cached per file and invalidated on `(mtime, size)`. Idle CPU is now 2–5%,
  and the test suite dropped from 17s to 1.7s.

Verified end to end: a prompt sent from the phone reaches `agy`, its
`run_command` is gated, the tool name and arguments arrive on the phone over a
sealed WebSocket, and the tap propagates back as the hook's decision.


## v26.08.22.2 — HTTPS, working hooks, and honest failure

### Web Crypto requires HTTPS

Payload encryption never worked from a phone, in any prior version. Browsers
expose `crypto.subtle` only in a **secure context** (HTTPS or localhost), so
over `http://<lan-ip>` the API does not exist. v26.08.21.5 degraded silently:
`initCrypto()` bailed out, `encryptData()` returned plaintext, and the server
accepted it — the session ran fully in the clear while the UI implied otherwise.

- Added `--tls`, which obtains a real Let's Encrypt certificate for this node's
  MagicDNS name via `tailscale cert`. Phones trust it with no warning and
  nothing to install, and pairing URLs switch to `https://` + the DNS name so
  the certificate matches. Used automatically when Tailscale is available.
- The client now detects an insecure context and explains it, instead of
  silently downgrading (old behaviour) or reporting an opaque "Frame rejected"
  (v26.08.22.1).
- The CLI warns at startup when serving plain HTTP with E2EE enabled.

### Remote tool approvals

- The installed hook command is now an absolute path. `agy-remote` lives in a
  project virtualenv that is not on `PATH`, so agy's `sh -c` could not launch
  the bare name and no approval ever reached the phone.
- The hook no longer falls back to `get_config()`, which shelled out to
  `tailscale ip` and `ifconfig` — about 2 seconds of latency on every tool call,
  to detect addresses a hook never uses.

### Fixes

- A restarting server no longer deletes the incoming server's runtime state.
  Shutdown cleared the file unconditionally, so on a quick restart the outgoing
  process wiped the new credentials and left `qr` and the hook with nothing.


## v26.08.22.1 — Security hardening

Full security audit and remediation. Every fix below ships with a regression
test in `tests/test_security.py` (verified red against v26.08.21.5, green now).

### Critical

- **Fixed stored XSS that exfiltrated the encryption key.** Model and tool
  output was parsed by a CDN-loaded Markdown library and assigned to
  `innerHTML`, so an agent induced via prompt injection to emit
  `<img src=x onerror=...>` could read `agy_e2ee_key` and `agy_remote_token`
  from `localStorage` — and the token grants code execution through the agent.
  Replaced with an escape-first local renderer; confirmed non-exploitable in a
  real browser.
- **Removed all third-party script origins.** The PWA pulled `marked` and five
  `highlight.js` bundles from a CDN (the latter never even used) into a page
  holding the encryption key. A CDN compromise was a full takeover. Now zero
  external origins, enforced by CSP and a test.
- **`--no-auth` is refused on non-loopback binds.** An unauthenticated listener
  on a LAN or tailnet address is remote code execution for anyone who can reach
  it.

### Encryption

- **Server-to-client traffic is now actually encrypted.** `broadcast()` and the
  `init` snapshot — transcripts, tool arguments, diffs, approval requests — were
  sent as cleartext despite E2EE being advertised as active. Only client-bound
  prompts and `pong` were sealed. All frames now go through `SessionManager.seal()`.
- **Added replay protection.** Envelopes carry a timestamp bound as GCM
  additional-authenticated-data plus a nonce cache (±300 s). A captured
  `send_prompt` frame can no longer be re-injected into the agent.
- **Closed the downgrade path.** Unsealed frames are rejected when E2EE is on,
  so holding the token without the key no longer suffices to drive the agent.
- **`decode_key()` validates key length** instead of silently accepting short keys.

### Authentication

- **Constant-time comparison on every entry point.** The WebSocket and
  PreToolUse hook endpoints used `!=`; only REST used `compare_digest`.
- **Fixed remote tool approvals, which never worked.** The hook runs in a
  separate process and called `get_config()`, minting a *fresh* random token
  that could never match the server's — every approval 401'd and silently fell
  back to `ask`. The server now publishes its token and port to a `0600` runtime
  state file that the hook reads.
- **Credentials are scrubbed from the URL** after load, keeping the token out of
  browser history and the address bar.

### Hardening

- Strict CSP plus `nosniff`, `DENY`, `no-referrer`, and `no-store` on every
  response; all inline event handlers replaced with delegated listeners.
- Removed wildcard CORS; added Host-header validation for no-auth (loopback) mode.
- Uploads: dropped `.svg`/`.bmp`, added magic-byte verification so a disguised
  payload cannot land in the workspace as `.png`; files written `0600`.
- `vapid.json` written `0600`, and pre-existing world-readable files repaired on load.
- `/api/status` no longer leaks version or feature flags before authentication.
- Conversation IDs are validated before a session switch.
- Narrowed the catch-all `except (WebSocketDisconnect, Exception)` that hid errors.
- `import agy_remote.server` no longer builds an app as a side effect.

### Documentation

- Rewrote the security section to describe the design accurately: this is
  pre-shared-key payload encryption between browser and server, not
  zero-knowledge E2EE. Documented the Tailscale subnet-router topology, where
  the final LAN hop is cleartext and payload encryption is what protects it.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Calendar Versioning (CalVer)](https://calver.org/) in the format `vYY.MM.DD.N`.

## [v26.08.21.5] - 2026-08-21

### Documentation
- Expanded [README.md](README.md) with comprehensive architecture diagrams (Mermaid), mobile PWA installation guides for iOS Safari and Android Chrome, CLI reference tables, environment variable options, and security threat models.
- Added step-by-step troubleshooting guides for Tailscale networking and iOS Web Push prerequisites.

## [v26.08.21.4] - 2026-08-21

### Security
- Added path traversal defenses in `get_transcript_path` for `conversation_id` validation and boundary enforcement.
- Hardened `/api/upload` with filename sanitization, image-only MIME/extension validation, path traversal verification, and a 25MB file size limit.
- Replaced token comparisons with `secrets.compare_digest` to eliminate timing side-channel attacks.
- Hardened CORS configuration to prevent unauthenticated cross-origin credential exposure.
- Enforced literal key escaping (`send-keys -l`) and safe shell joining in `TmuxSupervisor` to prevent keystroke injection.

## [v26.08.21.3] - 2026-08-21

### Added
- **End-to-End Encryption (E2EE)**: Full AES-256-GCM encryption with cryptographic keys embedded in the client-side URL hash fragment (`#key=...`) and Web Crypto API.
- **Self-Hosted Web Push Notifications**: Native lock-screen push alerts via local VAPID keys and W3C Web Push on iOS/Android PWA for tool permission requests and completions.
- **Persistent `tmux` Session Mode**: Native background session persistence (`agy-remote run --tmux`) allowing sessions to survive laptop sleep and terminal disconnects.
- **Mobile Photo / Screenshot Attachments**: Direct camera capture and gallery upload endpoint (`POST /api/upload`) into the agent context.
- **Visual Diff Viewer**: Interactive colored diff rendering for file replacements and tool edits in the mobile UI.

## [v26.08.21.2] - 2026-08-21

### Improved
- Comprehensive docstrings and type annotations across all core and server modules.
- Refined lint rules and test automation adhering to Ruff standards and CalVer versioning.

## [v26.08.21.1] - 2026-08-21

### Added
- Initial release of **`agy-remote`**, a mobile web remote control layer for Antigravity CLI (`agy`).
- Real-time log watcher and streaming parser for Antigravity CLI conversation transcripts (`transcript.jsonl`).
- Mobile-first Responsive Progressive Web App (PWA) with dark mode, collapsible thinking accordion, tool execution cards, and touch-friendly controls.
- Web Speech API integration for on-device voice dictation directly into active CLI prompts.
- Antigravity lifecycle hook integration (`PreToolUse`) enabling one-tap mobile tool execution approvals (`[Allow]` / `[Deny]`).
- Interactive PTY supervisor mode (`agy-remote run`) enabling simultaneous dual desktop terminal and mobile web prompting.
- Dynamic network detection with Tailscale IP, Local LAN, and interactive terminal ASCII QR code generation for instant mobile phone pairing.
- REST API and bi-directional WebSocket interface with token authentication.
