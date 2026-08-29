"""Start an agy session from the phone.

The phone says "clone repo X, do Y". The server -- the always-on machine that
hosts agy and the files -- clones the repo, launches agy on it, adopts it for
supervision, and streams it back. No cloud container: the Mac Studio is the
cloud.

Design decisions (docs/cc-parity-plan.md, W1):

- The server pre-clones by default: deterministic, no approval round-trip, and
  agy finds the code already there. "Let agy clone it" instead seeds a prompt
  with the clone instruction, so the clone becomes a normal approvable tool
  call the human sees on the phone.
- The workdir is fixed under the projects root and is never a client
  parameter; the name is sanitized to an alphabet that cannot escape it.
- Repo URLs are scheme-allowlisted: https, git, ssh, and the scp-like
  `git@host:org/repo` form. `file://` and local paths never reach git.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import RemoteConfig, agy_child_env, publish_server_registration
from .screen import TmuxScreen
from .tmux_runner import (
    TmuxSupervisor,
    capture_pane,
    is_tmux_available,
    session_id_of,
    set_tmux_supervisor,
)

logger = logging.getLogger("agy_remote.spawner")

#: A cold clone of a real repository can take a while on a tailnet; past this
#: the phone is left hanging with no answer at all.
CLONE_TIMEOUT = 600.0
#: agy must be up and drawing before we type into it; past this the session
#: still exists (watchable in the drawer) but the seed prompt is not typed.
READY_TIMEOUT = 90.0
#: How many consecutive identical captures count as "the TUI is quiet".
READY_STABLE_POLLS = 3
READY_POLL_INTERVAL = 1.5

ALLOWED_SCHEMES = {"https", "git", "ssh"}
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_SCP_LIKE_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:\S+$")
_NAME_INVALID_RE = re.compile(r"[^A-Za-z0-9_-]+")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class SpawnerError(Exception):
    """A spawn that cannot proceed, with a message the phone can read."""


class SpawnerBusyError(SpawnerError):
    """A second spawn while the first is running: a retry, not a fix."""


# ---------------------------------------------------------------------------
# Validation and naming: everything the client controls is checked here,
# before a byte of it reaches a shell or a path.
# ---------------------------------------------------------------------------


def validate_repo_url(url: str) -> str:
    """Allowlist the schemes that may be cloned.

    The URL travels to git as a single argv element, never through a shell,
    so the check is about where git will *look*: file:// and local paths reach
    the filesystem, everything else must be a real remote.
    """
    cleaned = (url or "").strip()
    if not cleaned or any(c.isspace() for c in cleaned):
        raise SpawnerError("repo URL is empty or unparseable")

    if _SCHEME_RE.match(cleaned):
        scheme = urllib.parse.urlsplit(cleaned).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise SpawnerError(f"URL scheme {scheme!r} is not allowed (use https, git or ssh)")
        return cleaned

    # The scp-like `git@host:org/repo` form: no scheme, host before the colon.
    if _SCP_LIKE_RE.match(cleaned):
        return cleaned

    raise SpawnerError(f"unrecognised repo URL {cleaned!r}: use an https, git or ssh URL")


def sanitize_name(name: str) -> str:
    """A session name that is safe as a directory name and a tmux session name."""
    return _NAME_INVALID_RE.sub("-", (name or "").strip()).strip("-_")[:40].strip("-_")


def default_name_for_url(url: str) -> str:
    """The repository the URL points at, as a name: last path segment minus .git."""
    path = url.rstrip("/").rsplit(":", 1)[-1]  # the host part of an scp-like URL
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    if segment.lower().endswith(".git"):
        segment = segment[:-4]
    return sanitize_name(segment)


def projects_root() -> Path:
    """Where phone-spawned workdirs live. Fixed by the operator, not the phone."""
    env = os.environ.get("AGY_REMOTE_PROJECTS_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / "Projects" / "agy-remote"


def unique_workdir(root: Path, name: str) -> Path:
    """`name`, or `name-2`, `name-3` ...: a second clone never clobbers the first."""
    candidate = root / name
    n = 2
    while candidate.exists():
        candidate = root / f"{name}-{n}"
        n += 1
    return candidate


@dataclass
class SpawnPlan:
    """Everything decided at request time; the run itself only executes it."""

    repo_url: str
    branch: str | None
    task: str | None
    name: str
    workdir: Path
    tmux_session: str
    let_agent_clone: bool = False

    @property
    def seed(self) -> str | None:
        """The prompt typed once the TUI is ready, or None to leave it idle."""
        parts: list[str] = []
        if self.let_agent_clone:
            clone = f"Clone {self.repo_url}"
            if self.branch:
                clone += f" (branch {self.branch})"
            clone += " into this directory and set it up."
            parts.append(clone)
        if self.task:
            parts.append(self.task.strip())
        return " ".join(parts) or None


# ---------------------------------------------------------------------------
# The spawner
# ---------------------------------------------------------------------------


class SessionSpawner:
    """Owns "start a session from the phone" for one server.

    One spawn at a time: two parallel spawns would race for the single
    supervisor slot this build supervises with, so the second is refused
    plainly rather than interleaved.
    """

    def __init__(self, cfg: RemoteConfig, mgr: Any, notifier: Any = None) -> None:
        self.cfg = cfg
        self.mgr = mgr
        #: The push manager, or a stand-in in tests: `send_notification(title, body, data)`.
        self.notifier = notifier
        self._task: asyncio.Task[None] | None = None

    @property
    def spawning(self) -> bool:
        return self._task is not None and not self._task.done()

    def create(self, req: Any) -> dict[str, Any]:
        """Validate the request, kick off the work, and answer with its identity.

        The answer is the *acceptance* of the spawn: the work proceeds in the
        background and reports over events (`session_spawning` stages, then
        `session_created`). A clone can take minutes, and a phone that holds
        an HTTP request that long is a phone that has dropped the request.
        """
        if self.spawning:
            raise SpawnerBusyError("a session is already being spawned; wait for it to finish or fail")

        plan = self._plan(req)
        self._task = asyncio.get_running_loop().create_task(self._run(plan))
        return {
            "status": "spawning",
            "name": plan.name,
            "workdir": str(plan.workdir),
            "tmux_session": plan.tmux_session,
        }

    def _plan(self, req: Any) -> SpawnPlan:
        if not is_tmux_available():
            raise SpawnerError("tmux is not installed on this machine; sessions started from the phone need it")
        if shutil.which("git") is None:
            raise SpawnerError("git is not installed on this machine; the repository cannot be cloned")

        url = validate_repo_url(req.repo_url)

        branch = (req.branch or "").strip() or None
        if branch and not _BRANCH_RE.match(branch):
            raise SpawnerError(f"invalid branch name {branch!r}")

        name = sanitize_name(req.name or "") or default_name_for_url(url)
        if not name:
            raise SpawnerError("could not derive a session name from the repository URL")

        workdir = unique_workdir(projects_root(), name)
        return SpawnPlan(
            repo_url=url,
            branch=branch,
            task=(req.task or "").strip() or None,
            name=name,
            workdir=workdir,
            tmux_session=f"agy-remote-{workdir.name}",
            let_agent_clone=bool(req.let_agent_clone),
        )

    # -- the run: clone, start, adopt, wait, seed ---------------------------

    async def _run(self, plan: SpawnPlan) -> None:
        try:
            plan.workdir.parent.mkdir(parents=True, exist_ok=True)
            plan.workdir.mkdir(parents=True, exist_ok=True)

            if not plan.let_agent_clone:
                await self._clone(plan)

            await self.mgr.broadcast(self._stage_event(plan, "starting"))
            supervisor = self._start_tmux(plan)
            self._adopt(plan, supervisor)

            ready = await self._wait_for_ready(plan)
            if ready and plan.seed:
                # The TUI is quiet, so this lands in the prompt box, not on a
                # half-drawn screen. One line: tmux send-keys -l plus Enter.
                supervisor.inject_input(plan.seed)

            await self.mgr.broadcast(
                {
                    "event": "session_created",
                    "data": {
                        "name": plan.name,
                        "workdir": str(plan.workdir),
                        "tmux_session": plan.tmux_session,
                        "ready": ready,
                    },
                }
            )
            self._notify(
                f"Session ready: {plan.name}",
                plan.task or plan.workdir.name,
                {"type": "session_ready", "session": plan.tmux_session},
            )
            if not ready:
                logger.info("agy in %s was still starting when the wait ended", plan.tmux_session)
        except Exception as e:
            logger.warning("Session spawn failed: %s", e)
            await self.mgr.broadcast(self._stage_event(plan, "failed", error=str(e)))
        finally:
            self._task = None

    async def _clone(self, plan: SpawnPlan) -> None:
        await self.mgr.broadcast(self._stage_event(plan, "cloning"))
        cmd = ["git", "clone", "--progress"]
        if plan.branch:
            cmd += ["--branch", plan.branch]
        # `--` before the URL: a URL that begins with `-` is a path, not an option.
        cmd += ["--", plan.repo_url, str(plan.workdir)]
        try:
            proc = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=CLONE_TIMEOUT)
        except subprocess.TimeoutExpired as e:
            raise SpawnerError(f"cloning timed out after {int(CLONE_TIMEOUT)}s") from e

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            # The phone is the audience: the last lines are where git states the reason.
            raise SpawnerError(detail[-800:] or f"git clone failed with exit code {proc.returncode}")

    def _start_tmux(self, plan: SpawnPlan) -> TmuxSupervisor:
        supervisor = TmuxSupervisor(
            session_name=plan.tmux_session,
            cmd=["agy"],
            env=agy_child_env(self.cfg, session_id=plan.tmux_session),
            workdir=str(plan.workdir),
        )
        if supervisor.has_session():
            # The workdir is fresh, so a live session by this name is somebody
            # else's: adopting it would type our prompt into their agent.
            raise SpawnerError(f"a tmux session named {plan.tmux_session!r} already exists")
        if not supervisor.start_detached():
            raise SpawnerError("could not start the tmux session")
        return supervisor

    def _adopt(self, plan: SpawnPlan, supervisor: TmuxSupervisor) -> None:
        """Hand the new session to the server: typing, screen, hook routing.

        This build supervises one session at a time, so adopting replaces the
        previous adoption. The previous agy keeps running in tmux; its
        approvals still reach the phone (hooks route by session), only the
        driving moves with the adoption.
        """
        self.cfg.tmux_session = plan.tmux_session
        self.cfg.tmux_target = plan.tmux_session
        self.cfg.tmux_session_id = session_id_of(plan.tmux_session)
        set_tmux_supervisor(supervisor)
        self.mgr.attach_screen(TmuxScreen(plan.tmux_session))
        # Re-publish: the registration is what lets the new session's PreToolUse
        # hook find this server rather than the shared state file's owner.
        publish_server_registration(self.cfg)

    async def _wait_for_ready(self, plan: SpawnPlan) -> bool:
        """A TUI is ready when its screen goes quiet.

        agy clears, draws its prompt box, and settles. Three consecutive
        identical captures is that settled state; nothing earlier is, because
        the first capture may be a blank half-boot. If the session dies while
        we watch, that is an error, not a timeout.
        """
        deadline = time.monotonic() + READY_TIMEOUT
        last: str | None = None
        stable = 0
        while time.monotonic() < deadline:
            snapshot = capture_pane(plan.tmux_session)
            if snapshot is None:
                # The pane is gone: a timeout would report a session that no longer exists.
                raise SpawnerError("the tmux session died while agy was starting")
            text = "\n".join(snapshot)
            if text.strip() and text == last:
                stable += 1
                if stable >= READY_STABLE_POLLS:
                    return True
            else:
                stable = 0
            last = text
            await asyncio.sleep(READY_POLL_INTERVAL)
        return False

    # -- reporting ----------------------------------------------------------

    def _stage_event(self, plan: SpawnPlan, stage: str, error: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "stage": stage,
            "name": plan.name,
            "workdir": str(plan.workdir),
            "tmux_session": plan.tmux_session,
        }
        if error:
            data["error"] = error
        return {"event": "session_spawning", "data": data}

    def _notify(self, title: str, body: str, data: dict[str, Any]) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.send_notification(title=title, body=body, data=data)
        except Exception as e:  # a dead push provider must not sink the session
            logger.debug("Push for spawned session failed: %s", e)
