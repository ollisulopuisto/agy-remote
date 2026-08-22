"""tmux session persistence runner for agy-remote."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
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

    def _attach_session(self) -> int:
        """Attach to tmux session in foreground, forwarding job-control signals cleanly."""
        old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            proc = subprocess.Popen(["tmux", "attach-session", "-t", self.session_name])
            while True:
                try:
                    wpid, status = os.waitpid(proc.pid, os.WUNTRACED)
                except InterruptedError:
                    continue
                except ChildProcessError:
                    break
                if wpid == proc.pid:
                    if os.WIFSTOPPED(status):
                        sig = os.WSTOPSIG(status)
                        signal.signal(sig, signal.SIG_DFL)
                        os.kill(os.getpid(), sig)
                        with contextlib.suppress(ProcessLookupError):
                            os.kill(proc.pid, signal.SIGCONT)
                    elif os.WIFEXITED(status):
                        return os.WEXITSTATUS(status)
                    elif os.WIFSIGNALED(status):
                        return -os.WTERMSIG(status)
        finally:
            signal.signal(signal.SIGINT, old_sigint)
        return 0

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
            # Disable VSUSP in the pane so Ctrl-Z does not suspend agy into an
            # unrecoverable hang without a shell to fg it.
            safe_cmd = f"stty susp undef 2>/dev/null; exec {cmd_str}"
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", self.session_name, safe_cmd],
                check=True,
            )

        # Attach to the session in the current terminal with signal handling
        return self._attach_session()

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


def sessions_running(command: str = "agy") -> list[str]:
    """tmux sessions with a pane whose foreground command is `command`.

    A session name proves nothing about what is inside it, so the pane's
    running command is what identifies an agent worth adopting. `list-panes -a`
    covers every session on the tmux server, including ones started long before
    this process existed -- which is the entire point of adoption.

    No tmux server at all exits non-zero; that is an empty list, not an error.
    """
    res = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_current_command}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return []

    found: list[str] = []
    for line in (res.stdout or "").splitlines():
        name, _, running = line.strip().partition(" ")
        if name and running == command and name not in found:
            found.append(name)
    return found


def capture_pane(session_name: str) -> list[str] | None:
    """The visible pane content as plain lines, or None if the session is gone.

    Read rather than emulated: there is no pty to listen on for a session this
    process did not start, and tmux is already keeping the screen for us.
    """
    res = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", session_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    return (res.stdout or "").split("\n")


def pane_geometry(session_name: str) -> dict[str, int] | None:
    """Size and cursor of the session's active pane, or None if it is gone."""
    res = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            session_name,
            "-F",
            "#{pane_height} #{pane_width} #{cursor_y} #{cursor_x}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    try:
        rows, cols, cy, cx = (int(v) for v in (res.stdout or "").split())
    except ValueError:
        return None
    return {"rows": rows, "cols": cols, "cursor_y": cy, "cursor_x": cx}


#: Distinguishes "read $TMUX yourself" from an explicit "there is no $TMUX",
#: which is a real answer: it means the caller is not inside tmux.
_FROM_ENV: str = "\x00from-env"


def session_id_from_env(tmux_env: str | None = _FROM_ENV) -> str | None:  # type: ignore[assignment]
    """The tmux session id of the process asking, from `$TMUX` alone.

    tmux exports `socket_path,server_pid,session_id` into every process in a
    pane, so a PreToolUse hook can say which session it belongs to without
    running anything. That matters: the hook runs on every single tool call,
    and shelling out to `tmux display-message` would put a subprocess in that
    path just to learn something already in the environment.

    Anything unrecognisable is None -- no claim is better than a wrong one,
    since the claim decides which server gets to approve a command.
    """
    raw = os.environ.get("TMUX") if tmux_env is _FROM_ENV else tmux_env
    if not raw:
        return None
    parts = raw.split(",")
    if len(parts) < 3 or not parts[-1].isdigit():
        return None
    return parts[-1]


def session_id_of(session_name: str) -> str | None:
    """The numeric id tmux gave a session, as `$TMUX` reports it.

    `#{session_id}` is `$3`; the environment carries the bare `3`.
    """
    res = subprocess.run(
        ["tmux", "display-message", "-p", "-t", session_name, "-F", "#{session_id}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    value = (res.stdout or "").strip().lstrip("$")
    return value if value.isdigit() else None


tmux_instance: TmuxSupervisor | None = None


def get_tmux_supervisor() -> TmuxSupervisor | None:
    """Get global active tmux supervisor."""
    return tmux_instance


def set_tmux_supervisor(sup: TmuxSupervisor) -> None:
    """Set global active tmux supervisor."""
    global tmux_instance
    tmux_instance = sup
