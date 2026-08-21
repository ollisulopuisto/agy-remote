# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Calendar Versioning (CalVer)](https://calver.org/) in the format `vYY.MM.DD.N`.

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
