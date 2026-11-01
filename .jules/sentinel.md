# Sentinel Security Journal

## 2026-11-01 - Option Injection Hardening in Spawner Git Cloning
**Vulnerability:** Repository URLs and branch names supplied to the spawner (`POST /api/sessions`) could contain leading dashes or option injection strings (e.g., `ssh://-oProxyCommand=...`, `git@host:-oProxyCommand=...`, `--branch -u`). Although `--` was present before the URL, leading dashes in hostnames, scp paths, or branch parameters can trigger option injection in git or underlying ssh transport.
**Learning:** Git transport protocols (especially SSH and SCP-like syntax) interpret hostnames or paths beginning with `-` as command options for `ssh` (e.g., `-oProxyCommand=...`), potentially resulting in arbitrary code execution during clone operations.
**Prevention:** Strictly forbid leading dashes in repository URLs, extracted hostnames, SCP paths, and branch names in input validation routines before passing arguments to git.
