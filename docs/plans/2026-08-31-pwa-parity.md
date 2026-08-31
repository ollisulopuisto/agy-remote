# PWA Parity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bring the selected mobile features from agy-remote and opencode's PWA together: structured questions, export/share, usage context, split diffs, web push approvals, haptics, and secure QR/PWA pairing.

**Architecture:** Keep agy-remote's zero-dependency, phone-first PWA and its existing server-side security boundaries. Add agy-side features through its normalized session/API seams. Add opencode-side push, haptics, and install pairing through the existing typed HttpApi, app state, and CLI pairing layers; never edit generated client files directly. Use Tailscale as transport protection, while preserving agy-remote's payload E2EE where it already exists.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, vanilla JavaScript/CSS, pytest, Node test runner; OpenCode TypeScript, Effect HttpApi, SolidJS, Bun, package-local typecheck/unit tests, and generated SDK files.

---

### Task 1: Structured Question Dock in agy-remote

**Files:**
- Inspect/modify: `src/agy_remote/models.py`, `src/agy_remote/server.py`, `src/agy_remote/session_manager.py`, `src/agy_remote/static/format.js`, `src/agy_remote/static/app.js`, `src/agy_remote/static/style.css`
- Test: `tests/test_server.py`, `tests/test_session_manager.py`, `tests/js/format.test.mjs`, `tests/js/approval.test.mjs` (create if needed)

**Step 1: Verify the agy question payload and write failing tests**

Add tests for an `ask_question` request carrying question text, options, multi-select state, and a free-form answer. Assert that the server preserves the structured fields in the broadcast and that the client model can render/select them. Do not assume a payload shape: use the actual hook/TUI payload found during inspection; if agy exposes only text, first add a tested compatibility adapter that represents it as a single free-form question.

**Step 2: Run the focused tests and confirm RED**

Run:

```bash
uv run pytest tests/test_server.py tests/test_session_manager.py -k question -q
node --test tests/js/format.test.mjs tests/js/approval.test.mjs
```

Expected: the new structured-question tests fail because the current approval path keeps only `decision`/`reason` and the current UI has only Allow/Deny controls.

**Step 3: Implement the smallest compatible question path**

Add explicit question fields to the request/approval data only where the verified agy payload requires them. Preserve ordinary tool approvals unchanged. Carry selected option values and a custom answer through the response; validate the response at the server boundary and route it through the existing approval future/backend delivery path.

**Step 4: Implement the mobile dock**

Add a question banner variant with accessible radio or checkbox controls, a custom-answer input when allowed, and a submit action. Keep the existing approval banner for tools. Use textContent/escaped rendering and the existing event delegation. Add responsive styling for one-handed phone use.

**Step 5: Run focused and full agy tests**

Run the focused tests from Step 2, then `uv run pytest` and `node --test tests/js`.

---

### Task 2: Session Markdown Export and Web Share

**Files:**
- Modify: `src/agy_remote/server.py`, `src/agy_remote/session_manager.py`, `src/agy_remote/static/index.html`, `src/agy_remote/static/app.js`, `src/agy_remote/static/style.css`
- Test: `tests/test_server.py`, `tests/test_session_manager.py`, `tests/js/sessions.test.mjs`

**Step 1: Write failing serializer and endpoint tests**

Cover user messages, model replies, thinking, tool calls, system/scaffolding steps, empty content, escaping-sensitive text, inactive sessions, unknown sessions, authentication, and a stable `Content-Disposition` filename. Assert Markdown is generated from normalized transcript data rather than raw untrusted HTML.

**Step 2: Run RED**

Run:

```bash
uv run pytest tests/test_server.py tests/test_session_manager.py -k export -q
```

Expected: the export route/serializer is missing.

**Step 3: Implement a deterministic Markdown export**

Add one manager/backend-neutral serializer over `TranscriptStep` values and `GET /api/conversations/{conversation_id}/export`. Reuse active steps for the active session and backend loading for inactive sessions. Return `text/markdown`, a safe filename, and 404 for unknown conversations. Do not add a new persistence format.

**Step 4: Add download/share controls**

Add an Export action to the session UI. Fetch the Markdown with the existing auth mechanism, use `navigator.share` with a `File` when supported, and fall back to a Blob download. Report failures without discarding the current transcript. Add helper tests for filename and capability fallback.

**Step 5: Verify**

Run the focused tests, `uv run pytest`, `node --test tests/js`, and the configured Ruff checks.

---

### Task 3: Context and Usage Strip in agy-remote

**Files:**
- Inspect/modify: `src/agy_remote/models.py`, `src/agy_remote/backends.py`, `src/agy_remote/session_manager.py`, `src/agy_remote/static/app.js`, `src/agy_remote/static/index.html`, `src/agy_remote/static/style.css`
- Test: `tests/test_session_manager.py`, `tests/test_server.py`, `tests/js/sessions.test.mjs`

**Step 1: Establish the available agy data and write RED tests**

Inspect real transcript/hook records and existing normalized events. Test only values agy actually emits. If token/cost/context events are unavailable, expose an honest activity strip (step count, current state, last activity, and pending approval) instead of inventing token accounting; keep the data model ready for verified usage events later.

**Step 2: Run RED**

Run the new focused tests and confirm the summary/event fields are absent.

**Step 3: Add a normalized session status/usage summary**

Add a manager/backend method that returns the verified fields, include it in init/session-switch payloads, and refresh it on relevant step/approval events. Keep parsing cached and avoid rereading transcripts on every redraw.

**Step 4: Render a compact responsive strip**

Place the strip in the header or session toolbar. Show labels, values, and an accessible tooltip; hide unavailable values rather than displaying zero. Ensure it does not compete with prompt controls on small screens.

**Step 5: Verify**

Run focused tests, full agy tests, Node tests, and Ruff.

---

### Task 4: Unified/Split Review Diff Toggle in agy-remote

**Files:**
- Modify: `src/agy_remote/static/format.js`, `src/agy_remote/static/app.js`, `src/agy_remote/static/index.html`, `src/agy_remote/static/style.css`
- Test: `tests/js/gitdiff.test.mjs`, `tests/js/sessions.test.mjs`

**Step 1: Write failing parser/pairing tests**

Add tests for pairing delete/add lines, context alignment, hunk/meta lines, insertions, deletions, unequal regions, binary/empty chunks, and switching back to unified mode without changing server data.

**Step 2: Run RED**

Run:

```bash
node --test tests/js/gitdiff.test.mjs tests/js/sessions.test.mjs
```

Expected: split-mode state/pairing helpers are missing.

**Step 3: Implement pure split projection**

Add a pure parser/projection that converts the existing parsed unified lines into left/right rows. Keep the server response unchanged. Preserve escape-first rendering and the existing per-file stats.

**Step 4: Add the toggle and persistence**

Add an accessible Unified/Split control to the diff pane, persist only the view preference locally, and render the selected mode for all files. Make refresh retain the selected mode.

**Step 5: Verify**

Run all JS tests, the PWA asset tests, full pytest, and Ruff.

---

### Task 5: Web Push Approval Flow in OpenCode

**Repository:** `/Users/dst/Documents/koodi/opencode`

**Files:**
- Inspect/modify: `packages/opencode/package.json` or workspace dependency manifest, `packages/opencode/src/server/...`, `packages/opencode/src/cli/...`, `packages/app/src/entry.tsx`, `packages/app/src/pages/layout.tsx`, `packages/app/src/.../permission...`, service-worker/install entrypoints
- Modify API schemas in their owning package, then regenerate: `packages/client` via `bun run generate`
- Test: package-local server utility/API tests, app state tests, and new push protocol tests

**Step 1: Write failing server/store tests**

Test VAPID key generation/persistence with mode `0600`, subscription validation and persistence, removal of failed subscriptions, payload contents for `permission.asked`/`permission.v2.asked`, and no credentials in ordinary responses. Test endpoint auth and schema errors.

**Step 2: Run RED from package directories**

Run the smallest package-local test commands from `packages/opencode` and `packages/app`; never run OpenCode tests from repository root.

**Step 3: Implement the push service and typed API**

Add a server-side VAPID/subscription service using the repository's dependency direction. Add declared HttpApi endpoints for public key and subscription registration, translate expected errors into declared schemas, and trigger push for permission requests. Keep notification payloads minimal: session id, permission id, title/body, and deep-link data; never include tool arguments or transcript content.

**Step 4: Regenerate clients and wire the app/service worker**

Run `bun run generate` from `packages/client`. Add a settings opt-in, `PushManager.subscribe`, subscription POST, service-worker push handling, notification click deep-linking, and approval focus behavior. The existing in-page permission dock remains authoritative; push only surfaces it.

**Step 5: Verify**

Run package-local typechecks, unit tests, generated-code checks, and oxlint on changed files. Real-device delivery remains a manual acceptance test and must be called out separately.

---

### Task 6: Haptics in OpenCode

**Repository:** `/Users/dst/Documents/koodi/opencode`

**Files:**
- Modify: `packages/app/src/...` permission/question interaction components and shared client utility location
- Test: package-local app unit tests for capability-safe haptic invocation

**Step 1: Write failing utility/component tests**

Test that supported browsers receive a short vibration on approval/question interaction, unsupported browsers and denied contexts are harmless, and repeated render does not duplicate listeners.

**Step 2: Run RED**

Run the focused app unit test from `packages/app`.

**Step 3: Implement one capability-safe helper**

Use a narrow browser utility that checks `navigator.vibrate` before calling it. Invoke it from user interaction and push notification click paths, not from render effects. Do not hardcode user-visible copy.

**Step 4: Verify**

Run `bun typecheck`, focused/unit app tests, and oxlint on changed files.

---

### Task 7: QR Pairing and PWA Install Flow in OpenCode

**Repository:** `/Users/dst/Documents/koodi/opencode`

**Files:**
- Inspect/modify: `packages/opencode/src/cli/qr.ts`, `packages/app/src/entry.tsx`, `packages/app/src/utils/server.ts`, `packages/app/index.html`, manifest assets, service worker/install UI
- Test: `packages/opencode/test/cli/qr.test.ts`, app utility tests, UI asset tests

**Step 1: Write failing pairing/install tests**

Cover Tailscale HTTPS URL construction, encoded directory/session route preservation, credential extraction, URL scrubbing after persistence, and a client-built manifest whose `start_url` preserves the paired route without putting secrets in a server response. Cover an anonymous manifest fallback.

**Step 2: Run RED**

Run the CLI QR tests from `packages/opencode` and app tests from `packages/app`.

**Step 3: Preserve existing Tailscale QR behavior and add paired install metadata**

Keep `opencode serve` loopback binding and `tailscale serve` as the transport path. Build the manifest client-side from credentials already present in the QR launch URL, preserve the directory/session route, and scrub the URL only after local persistence. Never add the auth token to a server-served manifest or service-worker cache.

**Step 4: Add install affordance and service-worker scope checks**

Use the existing manifest/assets and add platform-safe install guidance or install prompt handling. Ensure notification clicks route to the paired session and that the service worker does not cache credential-bearing URLs.

**Step 5: Verify**

Run package-local typechecks/unit tests, CLI tests, generated client checks if API changes occurred, and lint. Record real iOS/Android install verification as manual acceptance.

---

### Final Verification

**agy-remote:**

```bash
uv run pytest
node --test "tests/js/*.test.mjs"
uv run ruff format --check .
uv run ruff check .
```

**opencode:** run only from package directories, per `AGENTS.md`:

```bash
cd /Users/dst/Documents/koodi/opencode/packages/app && bun typecheck && bun run test:unit
cd /Users/dst/Documents/koodi/opencode/packages/opencode && bun typecheck
cd /Users/dst/Documents/koodi/opencode/packages/client && bun run generate
```

Then run oxlint on every changed OpenCode file and perform manual device acceptance for Web Push, haptics, QR scan, and Add to Home Screen.
