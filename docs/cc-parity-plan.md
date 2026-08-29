# agy-remote × Claude Code — parity plan

Status: proposal · 2026-08-29 · Researched: CC docs (overview, commands, remote-control,
mobile, what's-new w13–w34) + Antigravity CLI reference v1.1.22

## 1. Scope

`agy-remote` is the remote-control layer over `agy`. Parity means the *remote loop* —
directing, watching, approving, and spawning local agent sessions from a phone — at
Claude Code Remote Control level.

| Category | Rule |
| :--- | :--- |
| Remote loop (CC Remote Control / mobile) | Build to parity |
| Agent capabilities (plan mode, subagents, MCP, hooks, permissions, rewind) | `agy` already ships them (`/planning`, `/agents`, `/tasks`, MCP, hooks, `/permissions`, `/rewind`). Parity = *surface in the PWA*, not reimplement |
| Anthropic-cloud features (web sessions, teleport, Routines, Slack, Artifacts, Trusted Devices, GitHub Actions) | Out of scope — self-hosted, zero-cloud, air-gap capable is the product promise |

The two priority wishes (everything else is layered under them):

- **W1 — Start a session from the phone**: phone says "clone repo X, do Y" → the always-on
  Mac Studio clones it, launches `agy` on it, streams it back. No cloud container; the
  Studio hosts agy and the files.
- **W2 — Agents talk to each other** via a mailbox (tmp files, not pipes).

## 2. W1 — Start a session from the phone

### Design

One always-on server (launchd, today's `attach --wait` posture) supervises **N** agy
sessions instead of one. The phone is the dispatcher:

```
PWA "New session" ──POST /api/sessions {repo, branch, task, name}──▶ server
  1. validate repo URL (scheme allowlist: https / git / scp-ssh; no file://)
  2. workdir = $AGY_REMOTE_PROJECTS_DIR/<sanitized-name>   (default ~/Projects/agy-remote)
  3. git clone [--branch b] <url> <workdir>        (timeout, stderr → phone on failure)
  4. tmux new-session -d -s agy-remote-<slug> -c <workdir>
       env AGY_REMOTE_URL=… AGY_REMOTE_SESSION_ID=<id>  (existing hook routing, unchanged)
  5. adopt: register supervisor + screen mirror (reuse attach machinery)
  6. wait for TUI ready (screen stable / prompt box visible), inject seed prompt
  7. events: session_spawning{stage: cloning|starting|ready} → session_created
     + push notification "Session ready: <name>"
```

Clone strategy — **server pre-clones by default** (deterministic, no approval round-trip,
agy finds code already there; the task prompt is just the user's ask). A "let agy clone
it" toggle instead seeds the prompt with the clone instruction, so the clone becomes a
normal approvable tool call the user sees on the phone. Either way the phone ends up
watching agy work on a freshly cloned repo.

### Work items

- **1.1 Multi-session core** — ✅ Completed (v26.08.29.4). Dropped singleton assumptions:
  `SessionRecord{id, tmux_name, pane_target, workdir, conversation_id, busy}`, dynamic
  registry in `SessionManager`, decoupled `TmuxSupervisor` registry in `tmux_runner.py`,
  and per-session prompt/key/screen routing across backends and REST/WS endpoints.
- **1.2 `POST /api/sessions`** (spawner above) — M. URL validation + dir sanitisation
  (alnum/-/_; collision → `-2`), clone subprocess with timeout, spawn+adopt via existing
  `start_detached`/attach path, readiness poll before seeding. Reuse: `tmux_runner.py`,
  `backends.py::send_prompt`.
- **1.3 PWA "New session" sheet** — M. Fields: repo URL, branch (opt), task (opt), name
  (opt); live stage chip (cloning → starting → ready) from `session_spawning`; failure
  shows git stderr. New row appears in the drawer with a status dot.
- **1.4 Stop + session status** — S/M. Stop button in composer sends `escape` (agy's
  `Esc` halts the active stream — better than the current `interrupt` Ctrl+C which
  agy maps to *exit*!). Drawer rows: busy spinner (transcript quiescence), pending
  approval count, last activity.

### Security

Repo URL: scheme allowlist only; no `file://`, no embedded credentials parsed (document
if a token URL is used). Workdir fixed under the projects root, never a parameter.
Same trust model as prompt injection: the paired phone is a full operator.

## 3. W2 — Agent-to-agent messaging (mailbox, not pipes)

### Why files, not pipes

A running agent's stdin is its pty, owned by tmux/the supervisor — you cannot splice a
FIFO into it mid-session, and a FIFO as a mailbox is worse than a file anyway: it blocks
readers, holds no history, and dies with a crash. A plain JSONL file that the server
tails is *exactly* the proven `transcript.jsonl` pattern: append-only, durable,
inspectable, no open file descriptors, works across restarts.

### Design

```
~/.gemini/antigravity-cli/agy-remote/mailbox/<session-id>.jsonl
  one line per message: {"id": "…", "from": "…", "to": "…", "text": "…", "ts": "…"}
```

- **Send**: tiny `agy-msg` CLI shipped in the same package: `agy-msg to <target> "text"`.
  The calling agent invokes it as an ordinary bash tool call; `from` comes from
  `AGY_REMOTE_SESSION_ID` (exported into each agy at spawn — env flows into the tools agy
  spawns). It appends one validated line to the target's inbox. No helper wanted? The
  agent can `echo '{"to": …, "text": …}' >> …/<target>.jsonl` directly — document it in
  an AGENTS.md snippet.
- **Receive**: the 300 ms watch loop already running per session tails its own inbox;
  new line → injected as a prompt through the normal path, wrapped in an envelope the
  PWA renders distinctly from human prompts:
  `[message from agy-work (session 3): …]`
- **Human in the loop**: sending is a bash tool call, so it passes the existing PreToolUse
  approval gate — the phone shows `agy-msg to agy-work "…"` and approves/denies. First
  "always allow" makes steady-state chatter free. This is the safety spine: agent A
  cannot talk to B without the human having permitted the channel.
- **Loop protection**: 4 KB cap per message; per-pair rate limit (e.g. 10/10 min);
  ping-pong counter — if A↔B have exchanged N consecutive agent messages without a human
  prompt, the server pauses that pair's delivery and pushes a "agents are looping" alert.
  Per-pair mute toggle in the PWA.
- **Visibility**: PWA badge when agent-originated traffic flows; session rows show
  `⇄ agy-work ×3`; messages render as a distinct transcript style (server tags the
  synthetic prompt so the PWA can label it "from agy-work", not "you").

### Notes

- This is the agy-remote analogue of CC's cross-session messaging (shipped 2026) and
  agent-teams; agy also has native `/teamwork-preview` (paid) — the mailbox works today
  regardless of agy's roadmap and coexists with it.
- Delivery respects the prompt queue (Phase 3): a message arriving mid-turn is queued,
  not interleaved.

## 4. Gap analysis vs CC remote control (researched)

✅ parity · 🟡 partial · ❌ missing

| # | CC feature | S | Notes |
| :--- | :--- | :-: | :--- |
| 1 | Real-time transcript + thinking | ✅ | plus server-side terminal mirror (CC has no raw TUI view) |
| 2 | Approvals on phone, per session | ✅ | core strength |
| 3 | Prompt queue mid-turn, visible on device | ❌ | typed straight into TUI; no busy detection, no queue UI |
| 4 | Approvals survive transient disconnect | 🟡 | `can_hold_approval()` gives up when zero clients → `ask` fallback; CC holds until answered |
| 5 | Reconnect durability (per-client outbox) | 🟡 | PWA resyncs via `init`, but no bounded server-side outbox for events during a dead socket |
| 6 | Push: finished / action-required toggles | 🟡 | approval push ✅; completion push is a heuristic; no per-event toggles |
| 7 | Push presence suppression (skip when at machine) | ❌ | CC checks terminal presence + `CLAUDE_CLIENT_PRESENCE_FILE` |
| 8 | Notification tap → the raising session | ❌ | `sw.js` click opens bare `/`; `data.approval_id` unused |
| 9 | Start session from device | ❌ | **W1** |
| 10 | Session list with status; rename; archive | 🟡 | drawer lists + switches; no busy dot, no rename (agy has `/rename`!), no archive |
| 11 | Model picker from device | ❌ | blind keys into agy's `/model` panel only |
| 12 | Permission-mode selector | 🟡 | blind `Shift+Tab` cycle; agy mode is visible on screen |
| 13 | Slash commands from device | 🟡 | works (it's a TUI) but no palette, no distinct rendering |
| 14 | Session-level git diff pane | ❌ | per-tool-call diffs only; no working-tree diff view |
| 15 | Stop turn | 🟡 | `interrupt` key exists; no Stop button (and agy's Ctrl+C exits!) |
| 16 | Subagents/background tasks visible + stoppable | ❌ | agy has `/tasks` + `/agents` panels; PWA shows whatever transcript carries |
| 17 | Plan review + approve-to-execute | 🟡 | agy `/planning` exists; plan not rendered as approvable card |
| 18 | Attachments: photos + files as @refs | 🟡 | images only; path appended as text |
| 19 | Terminal "check in from your phone" hints | ❌ | QR at startup only |
| 20 | `/context`, `/usage`, cost visibility | ❌ | agy has both commands; PWA header shows none |
| 21 | Cross-session messaging | ❌ | **W2** |
| 22 | Voice dictation | ✅ | PWA-native; CC has CLI-only |
| 23 | CI/automation ingress (headless, Actions, channels) | 🟡 | `POST /api/prompt` + hook REST exist; undocumented; no completion callback |

## 5. Phased roadmap

Effort: S ≈ half day · M ≈ 1–2 d · L ≈ 3–5 d.

### Phase 1 — Start from phone (W1)
1.1 multi-session core (L) → 1.2 `POST /api/sessions` + spawner (M) →
1.3 PWA new-session sheet (M) → 1.4 Stop (Esc) + session status (S/M).
**Exit:** tap icon → form → `agy` working on a freshly cloned repo, streamed to the phone.

### Phase 2 — Agents talk (W2)
2.1 mailbox + watcher delivery + envelope (M) → 2.2 `agy-msg` CLI + env wiring +
AGENTS.md snippet (S) → 2.3 loop protection + PWA visibility (M).
**Exit:** A and B coordinate (e.g. "tests are red, see log" → B reacts), human approves
the channel once, sees everything, can pause a loop.

### Phase 3 — Remote-loop hardening (CC baseline)
- **Prompt queue** (gap 3, M/L): busy detection from transcript `RUNNING` steps /
  pending approvals / quiescence timer; per-session FIFO; "queued" chips with cancel;
  agent messages (W2) use the same queue. `session_manager.py`, `transcript.py`,
  `models.py` (`prompt_queued`/`prompt_delivered` events).
- **Hold approvals through blips** (gap 4, M): hold within the 240 s budget if a client
  was connected in the last N minutes; re-broadcast on reconnect. Keep fast `ask` when
  nobody ever connected (documented design: never stall an unwatched agent).
- **Per-client outbox** (gap 5, M): bounded (~500) replay buffer, drained on reconnect
  before live flow. `server.py` WS handler.
- **Push parity** (gaps 6–8, M): two toggles in the bell menu (persisted with the
  subscription); presence suppression (connected + tab visible ⇒ suppress); `sw.js`
  notificationclick → `#session=<id>&focus=<approval_id>`; completion push from the
  quiescence detector with last assistant line as body.
- **"Always allow" that means it** (M): persist a rule in agy's permissions config
  (the file `/permissions` manages — verify format) keyed on tool + arg match; banner
  states the scope ("always for `git push`").

### Phase 4 — Steering from the device
- Mode selector (S): segmented default/accept-edits/plan; send N×`shift_tab` toward
  target; mode badge already screen-parsed.
- Model picker (M): machine-readable model list (verify agy config) else parse the
  `/model` panel from the screen mirror; session-scoped like CC's device pick.
- Slash palette (S/M): `/` in composer → filtered list of safe commands (agy's actual
  set: `/clear /new /context /usage /diff /fast /fork /rewind /rename /resume /planning
  /tasks /agents /mcp /permissions`); one-tap send; distinct transcript styling.
- Git diff pane (M): tab beside Screen; server runs `git diff` (clean tree ⇒ diff vs
  merge-base of default branch); existing diff renderer; refresh on transcript write.
  Project dir fixed at server start — same trust model as prompt injection.
- Plan card (M): detect plan output (verify shape in transcript/artifacts); render as
  card with **Execute** (mode selector exits plan mode).
- Tasks chip (M): parse subagent/background-task steps from the transcript (verify
  shape); list running ones; focus + stop (Esc targets the turn).

### Phase 5 — Session management & polish
- Rename from phone (S): drawer rename → inject agy's `/rename <name>` (agy supports it;
  syncs to `/resume` for free).
- Export (S): `GET /api/conversations/{id}/export` → Markdown; Web Share API in PWA.
- Archive (S): move brain dir to `.archived/` sibling (verify enumeration), drawer filter.
- Status strip (S/M): `/usage` + credits surfaced in the header (agy has both).
- Terminal hints (S): `run` prints "Still working — check in from your phone: <url>" on
  long turns, "Approve tool calls from your phone" after repeated local approvals.
- Automation API (S, docs): document the existing REST as a local CI ingress
  (`POST /api/prompt`, approvals) + optional completion callback URL.
- Attachments beyond images (M): generic file upload → `@path` reference in the prompt.

## 6. Verify against agy (open questions)

1. Does `agy` accept a positional initial prompt (`agy "task"`)? (affects seeding —
   check `agy --help`; fallback is TUI-ready detection + injected prompt.)
2. Permissions config file format (for "always allow" persistence).
3. Transcript shapes for subagent / background-task / plan events.
4. Brain dir: is title writable? (probably moot — `/rename` covers it)
5. TUI "ready" signal for seeding (stable screen poll vs first idle).
6. Does the hook payload carry a tool id for targeted stop of a specific subagent?

## 7. Non-goals (deliberate)

Cloud sessions / `--cloud` / teleport, Routines, Slack & Telegram channels, claude.ai
Artifacts, Trusted Devices (org SSO), GitHub Actions, desktop app. The Mac Studio is the
cloud: always-on, tmux-persistent, tailnet-reachable. Anything here that needs an
Anthropic account or Anthropic servers is out by design.