# agy-remote × opencode PWA — parity status

Status: 2026-08-30 · after the v26.08.30.105 exchange · complement to
`cc-parity-plan.md` (Claude Code), which stays the longer-horizon roadmap.

## What "parity" means here

`opencode serve` hosts its own SolidJS web/PWA frontend; `agy-remote` is the
Python server + zero-dependency PWA for `agy`. Parity is two-directional:
features the opencode PWA has that this PWA lacks, and the reverse. The
transport/trust models differ (AES-GCM pre-shared payload encryption vs.
Tailscale + basic auth), so parity is about *features*, not protocols.

## Landed (this exchange, both directions)

| Feature | Direction | Where |
| :--- | :--- | :--- |
| Mid-turn prompt queue (chips, cancel, FIFO, quiescence + approval-aware busy window) | opencode followup dock → agy-remote | `session_manager.py` (`submit_prompt`, `deliver_due_prompts`), `app.js` queued chips |
| Working-tree diff pane (per-file chunks, add/del stats, untracked included) | opencode `working-tree-diff-v2` → agy-remote | `gitdiff.py`, `GET /api/git-diff`, Diff chip |
| File tree pane (dirs-first browsing of the session workdir, breadcrumbs, opens the file viewer) | opencode `file-tree` → agy-remote | `list_host_dir`, `GET /api/files`, Files chip |
| Alert sounds (WebAudio-synthesized: approval/complete/error + drawer toggle) | opencode `sound.ts` → agy-remote | `sounds.js` |
| Voice dictation wired into the composer (mic control, finals appended to text) | agy-remote → opencode | opencode `packages/app/src/utils/dictation.ts` + `prompt-input-v2.tsx`; the previously dead `getSpeechRecognitionCtor` adapter is now used |
| Two pre-existing reds fixed in opencode's app suite | hygiene | i18n parity (fork slash-menu keys missing from 62 locales) and `pa-PK` locale detection (ICU emits script `Aran`; aliased to `Arab`) |

Committed on the agy-remote side as v26.08.30.105 (file tree pane landed in
v26.08.31.107); opencode-side changes (dictation, i18n keys, session-ui
`dictationControl` slot, `microphone` icon) are committed in the opencode
fork, tests green, oxlint/tsgo clean.

Execution plan for the remaining gaps: `docs/plans/2026-08-31-pwa-parity.md`.

## Remaining gaps — opencode PWA has, agy-remote lacks

Ranked by remote-loop value. (The full list is large: tabs with drag & drop,
~60-locale i18n, settings UI, virtualized timeline, context-usage tab,
MCP/provider dialogs. The ones below are the ones worth chasing for a
phone-first remote.)

1. **Question dock** — opencode renders agent questions as structured
   radio/checkbox choices with custom answers. agy-remote shows
   `ask_question` gates as plain approval banners. Needs: hook payload
   carries option shapes (verify), models + banner variant.
2. **Permission rules ("Allow always")** — opencode persists per-pattern
   auto-accept. agy-remote has per-device auto-accept-all; a scoped rule
   needs agy's permissions file format (open question 2 in cc-parity-plan).
3. **Todo/plan progress dock** — needs verified transcript shapes for agy's
   planning steps (open question 3).
4. **Session export** — `GET /api/conversations/{id}/export` → Markdown +
   Web Share; transcript is already on disk.
5. **Context/usage strip** — `/usage` surfaced in the header (open question:
   transcript shape for cost events).

## Remaining gaps — agy-remote has, opencode PWA lacks

1. **VAPID Web Push (the headline gap, by opencode's own README)** —
   "lock-screen push when a tool needs approval" is explicitly listed as
   what you give up. Design, ready to execute next session:
   - Dependency: `web-push` (installs fine; verified).
   - Server util in `packages/opencode`: VAPID keypair persisted next to
     the auth state, subscription store (JSON, `0600`), `sendPush()` on
     `permission.v2.asked` (and turn-complete quiescence).
   - Two HttpApi endpoints (`GET push/vapid-public-key`,
     `POST push/subscribe`) + `bun run generate` from `packages/client`
     (mandated when HttpApi changes).
   - App: `sw.js` (push + `notificationclick` → `#session=...&focus=...`
     deep link, mirroring agy-remote's), PushManager subscribe flow behind
     a settings toggle; auth rides the existing token.
   - Constraint: needs a real device/browser to verify delivery; do not
     ship the wiring untested.
2. **Payload encryption / expiring pairings** — architectural (PSK AES-GCM
   vs. transport trust); only worth revisiting if opencode is exposed over
   a subnet router.
3. **Haptics** — one-line parity (`navigator.vibrate` on approval
   interaction) once push lands; low value alone.

## Verification commands

```bash
# agy-remote
uv run pytest && node --test "tests/js/*.test.mjs" && uv run ruff format --check . && uv run ruff check .

# opencode (from package dirs; never repo root)
cd packages/app && bun run typecheck && bun run test:unit
cd packages/session-ui && bun run typecheck
cd packages/ui && bun run typecheck
bunx oxlint <changed files>
```
