"""PTY runner for supervisor mode: runs agy CLI with dual-terminal/web input."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import struct
import termios
import tty
from collections.abc import Callable

from .keys import KEY_SEQUENCES

logger = __import__("logging").getLogger("agy_remote.pty")


class PtySupervisor:
    """Spawns an interactive agy CLI process in a pseudoterminal and multiplexes I/O."""

    def __init__(self, cmd: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.cmd = cmd or ["agy"]
        #: Extra environment for the child, telling its PreToolUse hook which
        #: server owns this session. Applied in the child after the fork.
        self.env = env or {}
        self.master_fd: int | None = None
        self.pid: int | None = None
        self.running: bool = False
        #: Size of the pty, copied from the desktop terminal at launch. The
        #: mirror must match it or every wrapped line lands in the wrong place.
        self.rows: int = 24
        self.cols: int = 80
        self._output_listeners: list[Callable[[bytes], None]] = []

    def add_output_listener(self, callback: Callable[[bytes], None]) -> None:
        """Receive every byte the CLI writes, as it is written.

        The supervisor is the only place these bytes exist: they are pty output,
        not transcript content, so anything that wants to know what is on the
        screen has to be handed them here.
        """
        self._output_listeners.append(callback)

    def _emit_output(self, data: bytes) -> None:
        """Fan out pty output; a broken listener must never kill the session."""
        for callback in self._output_listeners:
            try:
                callback(data)
            except Exception as e:  # noqa: BLE001 - a listener is not worth a dropped session
                logger.debug("Output listener failed: %s", e)

    def set_window_size(self, rows: int, cols: int) -> None:
        """Set terminal window size on the PTY."""
        self.rows, self.cols = rows, cols
        if self.master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            with contextlib.suppress(OSError):
                fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)

    def inject_input(self, text: str) -> None:
        """Inject a prompt or keystrokes into the running CLI session from mobile.

        The submit key must be CR, not LF. agy puts the tty in raw mode, where
        Enter is carriage return (0x0D); LF (0x0A) is Ctrl-J, which the input
        widget treats as "insert a line break". Sending LF therefore typed the
        prompt into agy's box and left it sitting there unsent.
        """
        if self.master_fd is None:
            return

        body = text.rstrip("\r\n")
        if body:
            os.write(self.master_fd, body.encode("utf-8"))
        os.write(self.master_fd, b"\r")

    def send_key(self, key: str) -> bool:
        """Press a single named key, for what text plus Enter cannot express.

        Returns False for an unknown name or a session that is not running, so
        the caller can tell "refused" from "delivered".
        """
        sequence = KEY_SEQUENCES.get(key)
        if sequence is None or self.master_fd is None:
            return False

        os.write(self.master_fd, sequence)
        return True

    @staticmethod
    def _become_session_leader(slave_fd: int) -> None:
        """Run in the child: take the pty as its controlling terminal.

        setsid() alone leaves the child in a session with no controlling
        terminal, and the interrupt character is not a byte the program reads --
        the line discipline turns it into SIGINT for the terminal's foreground
        process group. With no such group, Ctrl+C was swallowed and the
        session could not be interrupted.

        VSUSP (Ctrl+Z) is explicitly disabled: without a job-control shell
        managing the session, SIGTSTP suspends agy into an unrecoverable hang
        where no `fg` command is available to resume it.
        """
        os.setsid()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        except OSError as e:
            logger.debug("Could not claim controlling terminal: %s", e)

        try:
            attrs = termios.tcgetattr(slave_fd)
            vdisable = getattr(termios, "_POSIX_VDISABLE", b"\x00")
            attrs[6][termios.VSUSP] = vdisable
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not disable VSUSP on slave pty: %s", e)

    def start_sync(self) -> int:
        """Run the supervisor synchronously, capturing stdin/stdout of the active terminal."""
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd

        # Match initial window size if running inside a real TTY
        if os.isatty(0):
            try:
                ws = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws)
                self.rows, self.cols = struct.unpack("HHHH", ws)[:2]
            except Exception:
                pass

        pid = os.fork()
        if pid == 0:
            # Child process
            os.close(master_fd)
            self._become_session_leader(slave_fd)
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            try:
                os.environ.update(self.env)
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
                        self._emit_output(data)
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
                with contextlib.suppress(OSError):
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
