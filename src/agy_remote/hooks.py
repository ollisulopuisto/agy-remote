"""Antigravity CLI hook integration for remote tool permissions."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .config import get_config, read_runtime_state


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
        state = read_runtime_state()
        if state:
            port = state.get("port", get_config().port)
            auth_token = state["auth_token"]
        else:
            config = get_config()
            port, auth_token = config.port, config.auth_token

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
                "command": "agy-remote hook-pre-tool",
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
