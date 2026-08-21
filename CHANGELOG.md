# Changelog

## v26.08.22.19 — Approvals fail loudly, plus fixes for the streamed certificate

- **Remote approvals could be silently unwired.** The whole approval flow hangs
  on `~/.gemini/config/hooks.json` pointing at a working `agy-remote` binary on
  *this* machine. A machine where `setup-hooks` was never run — or where the
  installed absolute path went stale (moved checkout, recreated venv, config
  copied from another machine) — fell back to agy asking in its own TUI, and
  the phone never saw the permission dialog, with nothing to say why. `run` and
  `serve` now check the hook wiring at startup and print exactly what is wrong
  and the command that fixes it.
- **The streamed TLS private key existed world-readable for a moment.** The
  stdout-streaming path (below) wrote the key with `write_text` and chmod'd it
  afterwards; under a permissive umask that is a window where the key is open.
  It is now created 0600 from the first byte, like every other key file here.
- Covers the features pulled in from the parallel session: Tailscale
  certificate obtained by streaming to stdout (`--cert-file - --key-file -`),
  bypassing the macOS sandbox that blocks Tailscale.app from writing into
  `~/.gemini`; and the tmux QR pause now auto-attaches after a countdown
  (`--qr-timeout`, default 30s, 0 = attach immediately), keypress still skips.

## v26.08.22.18 — Tailscale found wherever it lives

- **HTTPS silently degraded when `tailscale` was not on PATH.** The macOS app
  bundle, Homebrew, Flatpak, and Snap all put the CLI somewhere a plain
  `tailscale` lookup may miss, and without it no certificate could be issued —
  the server fell back to plain HTTP and the phone lost Web Crypto. The binary
  is now discovered across the standard install locations, with an explicit
  override via `--tailscale-path` (alias `--tailscale-bin`) on `run`/`serve`
  or the `AGY_REMOTE_TAILSCALE_BIN` environment variable.
- Discovery order: explicit flag → environment variables → `PATH` → known
  locations. A configured path that does not exist or is not executable warns
  and degrades instead of crashing.

## v26.08.22.17 — Expiry also ends live connections

- Closes the edge left in v26.08.22.16: a WebSocket authenticated before the
  pairing deadline kept its socket — streaming the transcript and accepting
  prompts on a credential no longer valid. The watcher now sweeps expired
  connections closed (code 1008) on its existing tick; the reconnect is then
  refused at the door by `token_ok`.

## v26.08.22.16 — Pairing expiry holds while the server runs

- **The 30-day TTL was only checked at startup**, so a server left running
  honored an expired pairing until its next restart — exactly the window the
  TTL exists to close (surfaced by the post-publication security review). The
  deadline now travels into the config and is enforced inside `token_ok`, per
  auth check, across every entry point: REST, WebSocket connects, and the hook.
- An explicit `--token`/`AGY_REMOTE_TOKEN` carries no deadline (it is the
  operator's own), and `AGY_REMOTE_CREDENTIAL_TTL_DAYS=0` still disables expiry.
- Known edge: a WebSocket authenticated before the deadline keeps its existing
  connection; new connections are refused.

## v26.08.22.15 — The QR survives until scanned under --tmux

- **`--tmux` hid the QR code the moment agy appeared.** `tmux attach-session`
  replaces the entire terminal with tmux's own screen, so the banner and QR
  printed just before the attach vanished behind agy before a phone could scan
  them. PTY mode was never affected: its output scrolls beneath the QR instead
  of replacing it. The launch now pauses on the QR until a key is pressed, then
  attaches; `agy-remote qr` still re-displays the code at any time.

## v26.08.22.14 — Docs for the public

- README brought up to date for publication: PTY mode is now the recommended
  quickstart (it is the mode the terminal mirror and key controls support),
  tmux mode documented as persistence with its mirror limitation stated, watcher
  mode labelled read-only-plus-approvals. Feature list covers the terminal
  mirror, key control, transcript cleanup, and expiring pairings. Real clone
  URL, license badges, and the Terminal Key Controls section added to the table
  of contents.

## v26.08.22.13 — Pairings expire, and a license

- **A pairing URL never expired.** The credential store (v26.08.22.5) made the
  token long-lived, which quietly turned every phone bookmark into a credential
  valid forever — a leaked QR screenshot or a lost phone stayed a way in until
  someone remembered `--rotate-token`. Credentials now carry their mint date and
  expire after 30 days (`AGY_REMOTE_CREDENTIAL_TTL_DAYS`, `0` to disable); the
  next launch re-pairs with a fresh QR. A store from before expiry existed is
  stamped rather than discarded, so existing pairings survive the upgrade, and
  an unreadable birthdate counts as expired, not eternal.
- MIT license added; author contact and a real LAN address scrubbed from
  metadata and test fixtures ahead of publishing.

## v26.08.22.12 — A new session looks like a new session

- **Every fresh session read as a continuation of the last one.** agy opens each
  new conversation with a `SYSTEM/CHECKPOINT` step announcing that "the earlier
  parts of this conversation have been truncated" — boilerplate whose own
  request list holds only that session's first prompt, whose log reference
  points at that session's own transcript, and which ends by telling the model
  not to acknowledge it. It is scaffolding for the model, and v26.08.22.9 (which
  stopped dropping unrecognised steps) put it on screen as conversation.
- Checkpoints and `SYSTEM_MESSAGE` steps are now marked as scaffolding
  server-side and labelled as agy's own on the phone, instead of appearing to be
  history.
- Every conversation now opens with a divider naming it: `Session 8511476a ·
  started 17:35 · 12 steps`. `init` and `session_switched` carry the summary,
  so a session the client has never seen still announces itself.

## v26.08.22.11 — One line per tool call

- **The transcript was a wall of plumbing.** The desktop shows a tool call as
  one line -- `Bash(git status)`, `Search(Grep search for season)` -- with its
  arguments and output behind `ctrl+o`. The phone rendered every argument and
  every byte of output inline, so a single `du` pushed the conversation off the
  screen. Tool calls now collapse to `name(the argument that matters)`, and the
  detail is one tap away.
- Tool *output* (agy's `GENERIC` steps) collapses to `output · N lines`; short
  single-line output still shows as itself, with nothing to expand.
- `SYSTEM/CHECKPOINT` steps -- which v26.08.22.9 stopped dropping, and which run
  to dozens of lines -- collapse to their first line.
- Collapsing uses native `<details>`, so it stays keyboard- and
  screenreader-operable without a hand-rolled toggle.
- Adds the project's first JavaScript tests, using node's built-in runner and a
  `vm` sandbox, so the PWA's pure formatting logic is covered without pulling in
  a test framework: `node --test "tests/js/*.test.mjs"`.

## v26.08.22.10 — Ctrl+C works again

- **Ctrl+C and Ctrl+Z did nothing in the supervised terminal.** They are not
  bytes a program reads: the pty's line discipline turns them into SIGINT and
  SIGTSTP for the foreground process group *of that terminal*. The child called
  `setsid()` and then dup'd the pty onto its standard descriptors without ever
  claiming it as its controlling terminal, so the session had no foreground
  group and both keystrokes were swallowed. The child now issues `TIOCSCTTY`.
- Note that a suspended agy cannot be resumed from the supervising terminal:
  it lives in its own session, so there is no job control over it. Ctrl+C is the
  one to reach for.

## v26.08.22.9 — Reading agy's records instead of dumping them

- **Every prompt arrived on the phone wrapped in agy's plumbing.** agy stores a
  prompt as `<USER_REQUEST>` plus `<ADDITIONAL_METADATA>` and
  `<USER_SETTINGS_CHANGE>` blocks it feeds back to the model, and the PWA
  rendered the lot: a one-line question filled the screen, and every conversation
  in the drawer was titled `<USER_REQUEST>`. The envelope is now unwrapped
  server-side. Only all-caps tag blocks are touched, so `a < b` and `<div>`
  survive, and model output is never rewritten.
- **Tool arguments were double-escaped.** agy stores each argument as a JSON
  string inside a JSON object, so `JSON.stringify` showed `"\"df -h\""`. Values
  are decoded server-side (strings only -- `"5000"` stays text) and rendered one
  per row instead of as a brace-wrapped blob.
- **Steps the client did not recognise vanished.** `appendStep` handled user and
  model steps and silently dropped everything else, so `SYSTEM/CHECKPOINT` never
  appeared and the phone quietly showed less than the desktop. Unknown steps now
  render as a plain system line.
- **The execution-mode badge read the wrong thing.** It scanned the whole screen,
  and agy announces a mode change as a line of text that stays in the scrollback
  after you cycle past it, so the announcement kept beating the status bar. Only
  the bar is read now, matched field by field against the real layout.

## v26.08.22.8 — The phone can see the terminal

- **Keys were being pressed at a screen nobody could see.** The PWA renders
  `transcript.jsonl`; agy's pickers, confirmations, autocomplete and the
  execution mode in its status bar are drawn on the terminal and never written
  to the transcript. The supervisor had those bytes all along and threw them
  away after echoing them to its own stdout.
- The pty stream is now fed through a terminal emulator **on the server**
  (`screen.py`, backed by `pyte`) and broadcast as a grid of plain text, so the
  phone renders panels without an emulator of its own and the PWA stays free of
  third-party scripts. Tap **Screen** to show it; the execution mode appears as
  a badge beside the title, which is what makes Shift+Tab something other than a
  blind toggle.
- The screen is broadcast only when it changes, on the watcher's existing tick,
  so a still terminal costs nothing. `GET /api/screen` returns the same snapshot,
  and the `init` payload carries it so a client connecting mid-panel sees the
  panel instead of waiting for a redraw that may never come.
- Mirrors the PTY supervisor only. Under `--tmux` the terminal belongs to tmux,
  which would need `capture-pane` polling instead.

## v26.08.22.7 — Keys the phone could not press

- **The phone could only send a line of text ending in Enter.** agy's execution
  mode is cycled with `Shift+Tab`, its panels close with `Esc`, and every
  selection list it draws is driven by the arrow keys; none of that is
  expressible as a prompt. Named keys now travel over the same sealed WebSocket,
  or `POST /api/key`, and both supervisors implement them: the PTY path writes
  the escape sequence, the tmux path calls `tmux send-keys`. Only names from the
  allowlist in `keys.py` are accepted — the pty is wired to a live agent session,
  so accepting raw bytes off the network would be accepting arbitrary keystrokes.
- **The quick-action chips fired commands agy does not have.** `/goal` and
  `/schedule` are not agy slash commands, and "Stop" sent `/exit`, which quits
  the session rather than halting the stream. The row is now the keys that
  actually control the TUI, plus `/help` and `/planning`.

## v26.08.22.6 — The phone follows the session you are actually in

- **Starting agy left the phone on an hours-old conversation.** The watcher
  latched onto the newest conversation once at startup and never moved again:
  the switch was guarded by `not self.active_conversation_id`, which is false
  the moment anything is active, so it could not fire after boot. Launching
  `agy-remote run` therefore showed the desktop working in a new session while
  the phone rendered a stale one, with no indication anything was wrong. The
  watcher now follows the newest conversation as it appears.
- Picking an older session on the phone pins it, so a new session starting does
  not yank the view away mid-read. Selecting the newest one resumes following.

## v26.08.22.5 — One command, one URL

- **Every launch invalidated the phone's saved URL.** The token and E2EE key
  were generated per process, and the runtime state file that published them is
  deleted on shutdown, so each restart minted new ones: the QR had to be
  rescanned, and any bookmark or installed PWA came back with a dead token. The
  workaround was to pass `--token` and `AGY_REMOTE_E2EE_KEY` on every start.
  Both are properties of the host, not of a process, so they are now minted once
  into `~/.gemini/antigravity-cli/agy-remote-credentials.json` (mode 0600) and
  reused. `agy-remote run` takes no arguments again.
- `--rotate-token` on `run` and `serve` discards the stored pair and issues a
  new one, revoking every paired device. `AGY_REMOTE_TOKEN` and
  `AGY_REMOTE_E2EE_KEY` still override the store for a single launch.

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
