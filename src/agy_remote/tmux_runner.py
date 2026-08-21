"""tmux session persistence runner for agy-remote."""

from __future__ import annotations

import shutil
import subprocess

from .keys import TMUX_KEY_NAMES

logger = __import__("logging").getLogger("agy_remote.tmux")


#: The session a default-port launch uses, kept unqualified so the habitual
#: `tmux attach -t agy-remote` keeps working.
DEFAULT_SESSION_NAME = "agy-remote"
DEFAULT_PORT = 8765


def session_name_for_port(port: int) -> str:
    """The tmux session belonging to the server on `port`.

    The name used to be hardcoded, so a second launch found `has_session()`
    true and attached to the *first* instance's agy instead of starting its
    own -- two terminals typing into one session.
    """
    if port == DEFAULT_PORT:
        return DEFAULT_SESSION_NAME
    return f"{DEFAULT_SESSION_NAME}-{port}"


def is_tmux_available() -> bool:
    """Check if tmux binary is installed in PATH."""
    return shutil.which("tmux") is not None


class TmuxSupervisor:
    """Manages persistent agy CLI sessions inside tmux."""

    def __init__(
        self,
        session_name: str = DEFAULT_SESSION_NAME,
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.session_name = session_name
        self.cmd = cmd or ["agy"]
        #: Extra environment for the session, telling its PreToolUse hook which
        #: server owns it. The tmux server is long-lived and does not inherit
        #: ours, so it travels in the command itself -- which `ps` exposes to
        #: every local user, so nothing secret may be put here.
        self.env = env or {}

    def has_session(self) -> bool:
        """Check if target tmux session is currently active."""
        res = subprocess.run(
            ["tmux", "has-session", "-t", self.session_name],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0

    def start_or_attach(self) -> int:
        """Start a new tmux session or attach to existing one in foreground."""
        if not is_tmux_available():
            raise RuntimeError("tmux is not installed or not in PATH")

        import shlex

        argv = self.cmd
        if self.env:
            argv = ["env", *(f"{k}={v}" for k, v in self.env.items()), *argv]
        cmd_str = shlex.join(argv)
        if not self.has_session():
            # Create detached session first
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name, cmd_str],
                check=True,
            )

        # Attach to the session in the current terminal
        return subprocess.run(
            ["tmux", "attach-session", "-t", self.session_name],
            check=False,
        ).returncode

    def inject_input(self, text: str) -> bool:
        """Send keystrokes or prompts into the running tmux session literals safely."""
        if not self.has_session():
            return False

        # Send text as raw literal characters first (-l) then Enter
        subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, "-l", text],
            capture_output=True,
            check=False,
        )
        res = subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, "Enter"],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0

    def send_key(self, key: str) -> bool:
        """Press a single named key inside the tmux session."""
        name = TMUX_KEY_NAMES.get(key)
        if name is None or not self.has_session():
            return False

        res = subprocess.run(
            ["tmux", "send-keys", "-t", self.session_name, name],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0


tmux_instance: TmuxSupervisor | None = None


def get_tmux_supervisor() -> TmuxSupervisor | None:
    """Get global active tmux supervisor."""
    return tmux_instance


def set_tmux_supervisor(sup: TmuxSupervisor) -> None:
    """Set global active tmux supervisor."""
    global tmux_instance
    tmux_instance = sup
