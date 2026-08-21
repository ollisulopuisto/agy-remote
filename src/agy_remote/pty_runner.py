"""PTY runner for supervisor mode: runs agy CLI with dual-terminal/web input."""

from __future__ import annotations

import os
import pty
import select
import struct
import tty
from collections.abc import Callable

logger = __import__("logging").getLogger("agy_remote.pty")


class PtySupervisor:
    """Spawns an interactive agy CLI process in a pseudoterminal and multiplexes I/O."""

    def __init__(self, cmd: list[str] | None = None) -> None:
        self.cmd = cmd or ["agy"]
        self.master_fd: int | None = None
        self.pid: int | None = None
        self.running: bool = False
        self._input_listeners: list[Callable[[str], None]] = []

    def set_window_size(self, rows: int, cols: int) -> None:
        """Set terminal window size on the PTY."""
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            try:
                import fcntl
                import termios

                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
            except Exception:
                pass

    def inject_input(self, text: str) -> None:
        """Inject a prompt or keystrokes into the running CLI session from mobile."""
        if self.master_fd is not None:
            if not text.endswith("\n"):
                text += "\n"
            os.write(self.master_fd, text.encode("utf-8"))

    def start_sync(self) -> int:
        """Run the supervisor synchronously, capturing stdin/stdout of the active terminal."""
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd

        # Match initial window size if running inside a real TTY
        if os.isatty(0):
            try:
                import fcntl
                import termios

                ws = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
            except Exception:
                pass

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            try:
                os.execvp(self.cmd[0], self.cmd)
            except Exception as e:
                print(f"Failed to execute {' '.join(self.cmd)}: {e}")
                os._exit(1)

        # Parent process
        os.close(slave_fd)
        self.pid = pid
        self.running = True

        old_tty_attrs = None
        if os.isatty(0):
            old_tty_attrs = termios.tcgetattr(0)
            tty.setraw(0)

        try:
            while self.running:
                r, _, _ = select.select([0, master_fd], [], [], 0.05)
                if 0 in r:
                    # User typed on Mac desktop terminal
                    data = os.read(0, 1024)
                    if not data:
                        break
                    os.write(master_fd, data)

                if master_fd in r:
                    # CLI output to terminal
                    try:
                        data = os.read(master_fd, 1024)
                        if not data:
                            break
                        os.write(1, data)
                    except OSError:
                        break

                # Check child exit
                wpid, status = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)

        finally:
            if old_tty_attrs and os.isatty(0):
                termios.tcsetattr(0, termios.TCSADRAIN, old_tty_attrs)
            if self.master_fd:
                import contextlib

                with contextlib.suppress(Exception):
                    os.close(self.master_fd)
            self.running = False

        return 0


pty_instance: PtySupervisor | None = None


def get_pty_supervisor() -> PtySupervisor | None:
    """Get active PTY supervisor instance if running."""
    return pty_instance


def set_pty_supervisor(sup: PtySupervisor) -> None:
    """Set global active PTY supervisor."""
    global pty_instance
    pty_instance = sup
