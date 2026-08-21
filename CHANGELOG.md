# Changelog

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
