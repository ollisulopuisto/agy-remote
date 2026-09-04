"""PTY runner for supervisor mode: runs agy CLI with dual-terminal/web input."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import signal
import struct
import termios
import time
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

    def kill(self) -> bool:
        """Forcefully terminate the child process with SIGKILL."""
        if self.pid is None:
            return False
        try:
            os.kill(self.pid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return False

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

        SIGTSTP itself is set to SIG_IGN, which survives exec: disabling VSUSP
        only stops the *line discipline* from generating the signal, but a TUI
        that reads the 0x1a byte and politely raises SIGTSTP on itself still
        froze the whole session. Ignoring it closes that path too.
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

        with contextlib.suppress(OSError, ValueError):
            signal.signal(signal.SIGTSTP, signal.SIG_IGN)

    @staticmethod
    def _tty_is_ours() -> bool:
        """Whether this process is in the desktop terminal's foreground group.

        After Ctrl+Z and `bg` the supervisor keeps running but the shell owns
        the terminal: reading stdin from back there means SIGTTIN and another
        stop, and writing the mirror means scribbling over the user's prompt.
        """
        try:
            return os.tcgetpgrp(0) == os.getpgrp()
        except OSError:
            return True

    def _sync_winsize_from_tty(self) -> None:
        """Copy the desktop terminal's current size onto the pty."""
        try:
            ws = fcntl.ioctl(0, termios.TIOCGWINSZ, b"\x00" * 8)
            rows, cols = struct.unpack("HHHH", ws)[:2]
            self.set_window_size(rows, cols)
        except OSError:
            pass

    def _nudge_repaint(self) -> None:
        """Force the TUI to redraw after the terminal was someone else's.

        A resize wiggle (one column off, then back) raises real SIGWINCHes,
        which every full-screen TUI already handles; a same-size SIGWINCH or a
        Ctrl+L would each depend on the child noticing something optional.
        """
        rows, cols = self.rows, self.cols
        if cols > 1:
            self.set_window_size(rows, cols - 1)
        self.set_window_size(rows, cols)

    def _resume_foreground(self, pid: int) -> None:
        """Back in the foreground: raw mode again, and a screen worth looking at."""
        with contextlib.suppress(termios.error):
            tty.setraw(0)
        self._sync_winsize_from_tty()
        self._nudge_repaint()
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGCONT)

    def _suspend_to_shell(self, old_tty_attrs: list | None) -> None:
        """Desktop Ctrl+Z suspends the *supervisor*, never agy.

        Forwarding the byte was the wedge: agy sat on a pty with no shell, so
        anything that suspended it froze the session with the desktop terminal
        still in raw mode -- Ctrl+C then went nowhere either, and the only way
        out was a kill from another terminal. The supervisor, unlike agy, was
        started from a shell that can `fg` it, so it is the thing job control
        should act on. agy itself keeps running while we are gone.
        """
        if old_tty_attrs is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(0, termios.TCSADRAIN, old_tty_attrs)
        with contextlib.suppress(OSError):
            os.write(
                1,
                b"\r\n[agy-remote] suspended -- agy keeps running."
                b" `fg` returns; `bg` keeps serving the phone; `kill %1` ends it.\r\n",
            )
        prev = signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        try:
            os.kill(os.getpid(), signal.SIGTSTP)
        finally:
            signal.signal(signal.SIGTSTP, prev)

    @staticmethod
    def _reap_child(pid: int) -> int | None:
        """The child's exit code if it finished, resuming it if it merely stopped.

        The old WNOHANG-only wait was blind to a stopped child: an agy that
        suspended itself (a self-raised SIGTSTP is not covered by the VSUSP
        guard) left the loop spinning over a frozen screen forever. There is no
        shell to `fg` the child, so a stop is never legitimate here: answer
        every one with SIGCONT.
        """
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            return 0
        if wpid != pid:
            return None
        if os.WIFSTOPPED(status):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGCONT)
            return None
        return os.waitstatus_to_exitcode(status)

    def _hang_up_child(self, pid: int) -> None:
        """`run` owns this agy and dies with it.

        On the exit paths that are not the child exiting -- stdin EOF, a pty
        error, Ctrl+C delivered as a real SIGINT -- the child used to be left
        behind, wedged on a pty nobody reads and still owning the session as
        far as the server registry was concerned.
        """
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return
        except ChildProcessError:
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGHUP)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:
                    return
            except ChildProcessError:
                return
            time.sleep(0.05)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)

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

        stdin_tty = os.isatty(0)
        old_tty_attrs = None
        if stdin_tty:
            old_tty_attrs = termios.tcgetattr(0)
            tty.setraw(0)

        #: Whether the desktop terminal is currently ours. After Ctrl+Z + `bg`
        #: it belongs to the shell: the phone keeps its mirror, the desktop is
        #: left alone until `fg` hands the terminal back.
        foreground = True

        try:
            while self.running:
                if stdin_tty:
                    fg_now = self._tty_is_ours()
                    if fg_now and not foreground:
                        self._resume_foreground(pid)
                    foreground = fg_now

                fds = [master_fd]
                if not stdin_tty or foreground:
                    fds.insert(0, 0)
                r, _, _ = select.select(fds, [], [], 0.05)

                if 0 in r:
                    # User typed on the desktop terminal
                    data = os.read(0, 1024)
                    if not data:
                        break
                    if stdin_tty and b"\x1a" in data:
                        # Ctrl+Z belongs to the supervisor's own job control,
                        # never to agy: see _suspend_to_shell.
                        head, _, _rest = data.partition(b"\x1a")
                        if head:
                            os.write(master_fd, head)
                        self._suspend_to_shell(old_tty_attrs)
                        foreground = self._tty_is_ours()
                        if foreground:
                            self._resume_foreground(pid)
                        continue
                    os.write(master_fd, data)

                if master_fd in r:
                    # CLI output to terminal
                    try:
                        data = os.read(master_fd, 1024)
                        if not data:
                            break
                        if not stdin_tty or foreground:
                            os.write(1, data)
                        self._emit_output(data)
                    except OSError:
                        break

                code = self._reap_child(pid)
                if code is not None:
                    return code

        finally:
            if old_tty_attrs and os.isatty(0):
                # From the background this write needs SIGTTOU ignored, or the
                # cleanup itself would stop the process one last time.
                prev = signal.signal(signal.SIGTTOU, signal.SIG_IGN)
                with contextlib.suppress(termios.error):
                    termios.tcsetattr(0, termios.TCSADRAIN, old_tty_attrs)
                signal.signal(signal.SIGTTOU, prev)
            if self.master_fd:
                with contextlib.suppress(OSError):
                    os.close(self.master_fd)
            self.running = False
            self._hang_up_child(pid)

        return 0


pty_instance: PtySupervisor | None = None


def get_pty_supervisor() -> PtySupervisor | None:
    """Get active PTY supervisor instance if running."""
    return pty_instance


def set_pty_supervisor(sup: PtySupervisor) -> None:
    """Set global active PTY supervisor."""
    global pty_instance
    pty_instance = sup
