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


def resolve_server_endpoint() -> tuple[int, str]:
    """Find the running server's port and token as cheaply as possible.

    This runs on every single tool call, so the no-server path must not fall
    back to get_config(): that shells out to `tailscale ip` and `ifconfig`,
    adding ~2s of latency per tool call to detect addresses a hook never uses.
    """
    state = read_runtime_state()
    if state:
        return int(state.get("port", DEFAULT_PORT)), state.get("auth_token", "")

    try:
        port = int(os.environ.get("AGY_REMOTE_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    return port, os.environ.get("AGY_REMOTE_TOKEN", "")


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
        port, auth_token = resolve_server_endpoint()

        url = f"http://127.0.0.1:{port}/api/hook/pre-tool"
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
