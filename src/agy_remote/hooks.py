"""Antigravity CLI hook integration for remote tool permissions."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import read_runtime_state

DEFAULT_PORT = 8765


def resolve_server_endpoint() -> tuple[str, str]:
    """Find the running server's base URL and token as cheaply as possible.

    The scheme matters: once the server serves HTTPS, posting http:// to it
    fails outright and the hook silently degrades to "ask", so no approval
    ever reaches the phone.

    This runs on every single tool call, so the no-server path must not fall
    back to get_config(): that shells out to `tailscale ip` and `ifconfig`,
    adding ~2s of latency per tool call to detect addresses a hook never uses.
    """
    state = read_runtime_state()
    if state:
        port = int(state.get("port", DEFAULT_PORT))
        base_url = state.get("base_url") or f"http://127.0.0.1:{port}"
        return base_url, state.get("auth_token", "")

    try:
        port = int(os.environ.get("AGY_REMOTE_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    return f"http://127.0.0.1:{port}", os.environ.get("AGY_REMOTE_TOKEN", "")


def resolve_hook_command() -> str:
    """Build an absolute command line agy's `sh -c` can actually execute.

    `agy-remote` normally lives in a project virtualenv that is not on PATH,
    so the bare name fails to launch and approvals never reach the phone.
    """
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if candidate and candidate.name.startswith("agy-remote") and candidate.exists():
        return f"{shlex.quote(str(candidate))} hook-pre-tool"

    found = shutil.which("agy-remote")
    if found:
        return f"{shlex.quote(str(Path(found).resolve()))} hook-pre-tool"

    # Last resort: the interpreter running us can always reach the module.
    return f"{shlex.quote(sys.executable)} -m agy_remote.cli hook-pre-tool"


def run_pre_tool_hook() -> None:
    """Invoked by Antigravity CLI PreToolUse hook on stdin."""
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            # Nothing to process
            print(json.dumps({"decision": "allow"}))
            return

        payload = json.loads(raw_input)

        # Use the running server's published credentials. get_config() would
        # mint a *fresh* random token in this separate process, which never
        # matches the server's and so 401s on every approval request.
        base_url, auth_token = resolve_server_endpoint()

        url = f"{base_url}/api/hook/pre-tool"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "X-Auth-Token": auth_token,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=310.0) as resp:
                resp_data = resp.read().decode("utf-8")
                # Output decision JSON to stdout for agy CLI
                print(resp_data)
        except urllib.error.URLError:
            # Server not running or unreachable: default to ask
            print(json.dumps({"decision": "ask", "reason": "agy-remote server unreachable"}))

    except Exception as e:
        # Failsafe: return ask or allow
        print(json.dumps({"decision": "ask", "reason": f"Hook error: {e}"}))


def hook_health(config_dir: Path | None = None) -> tuple[str, str | None]:
    """Whether remote approvals are actually wired on this machine.

    The hook command in hooks.json carries an absolute path captured at install
    time. A moved checkout, a recreated venv, or a config copied from another
    machine leaves it pointing at nothing -- agy then quietly falls back to
    asking in its own TUI, and the phone never sees the approval. `run` checks
    this at startup so the failure is loud instead of silent.

    Returns ("ok" | "missing" | "broken", detail).
    """
    if config_dir is None:
        config_dir = Path.home() / ".gemini" / "config"
    hooks_file = config_dir / "hooks.json"

    try:
        with open(hooks_file, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return "missing", None

    entries = data.get("remote-approval", {}).get("PreToolUse") or []
    for entry in entries:
        for hook in entry.get("hooks", []):
            command = hook.get("command", "")
            if "hook-pre-tool" not in command:
                continue
            try:
                binary = shlex.split(command)[0]
            except ValueError:
                return "broken", command
            if Path(binary).is_file() and os.access(binary, os.X_OK):
                return "ok", command
            return "broken", binary

    return "missing", None


def install_hooks_config(target_dir: Path | None = None) -> Path:
    """Install or update hooks.json in the target directory or global config."""
    if target_dir is None:
        target_dir = Path.home() / ".gemini" / "config"
    target_dir.mkdir(parents=True, exist_ok=True)
    hooks_file = target_dir / "hooks.json"

    hook_entry = {
        "matcher": "*",
        "hooks": [
            {
                "type": "command",
                "command": resolve_hook_command(),
                "timeout": 300,
            }
        ],
    }

    data: dict = {}
    if hooks_file.exists():
        try:
            with open(hooks_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    if "remote-approval" not in data:
        data["remote-approval"] = {}

    data["remote-approval"]["PreToolUse"] = [hook_entry]

    with open(hooks_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return hooks_file
