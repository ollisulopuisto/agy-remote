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

from .config import find_server_for_tmux_session, read_runtime_state, read_stored_token
from .tmux_runner import session_id_from_env

DEFAULT_PORT = 8765


def resolve_server_endpoint() -> tuple[str, str]:
    """Find the running server's base URL and token as cheaply as possible.

    The scheme matters: once the server serves HTTPS, posting http:// to it
    fails outright and the hook silently degrades to "ask", so no approval
    ever reaches the phone.

    This runs on every single tool call, so the no-server path must not fall
    back to get_config(): that shells out to `tailscale ip` and `ifconfig`,
    adding ~2s of latency per tool call to detect addresses a hook never uses.

    `AGY_REMOTE_URL` comes first because it is the only *per-process* signal
    available. The runtime state file is shared by the whole host, so with two
    servers running it would send both agy sessions' approvals to whichever one
    published it. A server exports the variable into the agy it launches, so
    each hook posts to the server that owns its session; an agy started by hand
    has no such parent and falls back to the file.
    """
    env_url = os.environ.get("AGY_REMOTE_URL")
    if env_url:
        return env_url, _hook_token()

    # An agy nobody launched has no `AGY_REMOTE_URL`, and the shared state file
    # can only name one server -- so with two running, both sessions' approvals
    # went to whichever wrote it last. Inside tmux this process can say which
    # session it belongs to, from `$TMUX` alone, and a server that adopted that
    # session says so in its registration.
    session_id = session_id_from_env()
    if session_id:
        owner = find_server_for_tmux_session(session_id)
        if owner:
            port = int(owner.get("port", DEFAULT_PORT))
            base_url = owner.get("base_url") or f"http://127.0.0.1:{port}"
            return base_url, str(owner.get("auth_token") or _hook_token())

    state = read_runtime_state()
    if state:
        port = int(state.get("port", DEFAULT_PORT))
        base_url = state.get("base_url") or f"http://127.0.0.1:{port}"
        return base_url, state.get("auth_token", "")

    # Nothing told us where the server is, so this endpoint is a guess. Never
    # hand the host token to a port we have not confirmed is ours; an
    # unauthenticated call fails closed to "ask", which is the right outcome.
    try:
        port = int(os.environ.get("AGY_REMOTE_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    return f"http://127.0.0.1:{port}", os.environ.get("AGY_REMOTE_TOKEN", "")


def _hook_token() -> str:
    """The token to authenticate with, without minting one.

    The token is a property of the host, not of a process: every server on this
    machine loads the same stored credentials, so the second server accepts it
    too. An explicit `--token` is the exception -- pass it to the hook through
    `AGY_REMOTE_TOKEN` rather than argv, which `ps` exposes to every local user.
    """
    env_token = os.environ.get("AGY_REMOTE_TOKEN")
    if env_token:
        return env_token
    state = read_runtime_state()
    if state and state.get("auth_token"):
        return str(state["auth_token"])
    # A second server publishes no state, so the stored host credential is all
    # its hook has. One file read, no network detection.
    return read_stored_token() or ""


def _uv_cache_dir() -> Path:
    """Where uv keeps its ephemeral environments."""
    env = os.environ.get("UV_CACHE_DIR")
    return Path(env) if env else Path.home() / ".cache" / "uv"


def _is_ephemeral(path: Path) -> bool:
    """Whether `path` lives in uv's cache, which `uv cache clean` deletes."""
    try:
        return path.resolve().is_relative_to(_uv_cache_dir().resolve())
    except OSError:
        return False


def _uv_tool_binary() -> Path | None:
    """The binary of a stable `uv tool install agy-remote`, if one exists."""
    env = os.environ.get("UV_TOOL_DIR")
    tool_dir = Path(env) if env else Path.home() / ".local" / "share" / "uv" / "tools"
    binary = tool_dir / "agy-remote" / "bin" / "agy-remote"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    return None


def resolve_hook_command() -> str:
    """Build a command line agy's `sh -c` can execute, today and next month.

    `agy-remote` normally lives in a project virtualenv that is not on PATH,
    so the bare name fails to launch and approvals never reach the phone --
    hence an absolute path.

    But not *any* absolute path: under `uvx agy-remote`, argv[0] and
    sys.executable both live in uv's cache, which `uv cache clean` deletes --
    quietly breaking approvals until the next setup-hooks. So an ephemeral
    location is passed over in favor of a stable `uv tool` install, then
    anything on PATH outside the cache, then `uvx` itself, which re-resolves
    on every call and therefore self-heals after a cache clean.
    """
    candidate = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if candidate and candidate.name.startswith("agy-remote") and candidate.exists() and not _is_ephemeral(candidate):
        return f"{shlex.quote(str(candidate))} hook-pre-tool"

    tool_binary = _uv_tool_binary()
    if tool_binary:
        return f"{shlex.quote(str(tool_binary))} hook-pre-tool"

    found = shutil.which("agy-remote")
    if found and not _is_ephemeral(Path(found)):
        return f"{shlex.quote(str(Path(found).resolve()))} hook-pre-tool"

    uvx = shutil.which("uvx")
    if uvx:
        return f"{shlex.quote(uvx)} agy-remote hook-pre-tool"

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
