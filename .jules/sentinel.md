# Sentinel Security Journal

## 2026-11-02 - Keystroke Injection via Missing E2EE Enforcement on POST /api/key
**Vulnerability:** When E2EE was enabled (`cfg.e2ee_enabled = True`), all content mutation endpoints (`/api/prompt`, `/api/sessions`, `/api/conversations/{id}/rename`, `/api/agents/jobs`, and WebSocket frames) enforced encrypted envelopes and rejected plaintext payloads with HTTP 400. However, `POST /api/key` directly accepted bare JSON payloads with only token authorization, creating an encryption downgrade hole that allowed an eavesdropper holding only the authentication token to inject keystrokes into live sessions without knowing the E2EE key.
**Learning:** Endpoints that execute actions or inject input into agent sessions must adhere uniformly to the encryption boundary. When an application provides alternate REST and WebSocket vectors for the same capability (`send_key`), partial E2EE enforcement on only one transport allows protocol downgrade attacks.
**Prevention:** Whenever E2EE mode is active, unconditionally gate all command and input injection endpoints with envelope verification, payload decryption (`decrypt_payload`), and replay protection guards (`ReplayGuard`).

## 2026-11-01 - Option Injection Hardening in Spawner Git Cloning
**Vulnerability:** Repository URLs and branch names supplied to the spawner (`POST /api/sessions`) could contain leading dashes or option injection strings (e.g., `ssh://-oProxyCommand=...`, `git@host:-oProxyCommand=...`, `--branch -u`). Although `--` was present before the URL, leading dashes in hostnames, scp paths, or branch parameters can trigger option injection in git or underlying ssh transport.
**Learning:** Git transport protocols (especially SSH and SCP-like syntax) interpret hostnames or paths beginning with `-` as command options for `ssh` (e.g., `-oProxyCommand=...`), potentially resulting in arbitrary code execution during clone operations.
**Prevention:** Strictly forbid leading dashes in repository URLs, extracted hostnames, SCP paths, and branch names in input validation routines before passing arguments to git.

## 2026-10-30 - Atomic Permissions on Uploaded Files
**Vulnerability:** In `/api/upload`, uploaded files were written with `open(dest, "wb")` and then tightened with `dest.chmod(0o600)`. Under standard umasks (e.g. 0o022), the file was created world/group readable (0o644) for a brief window before `chmod` ran, exposing potentially sensitive screenshots or uploaded images to other users on multi-user systems.
**Learning:** `open()-then-chmod` patterns suffer from a TOCTOU permissions race condition where files are created under the process umask before permissions are restricted.
**Prevention:** Always create sensitive files atomically with restricted modes at creation time using `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)` and `os.fdopen()`, matching the secure file creation pattern used across this codebase for runtime tokens, VAPID keys, and certificates.