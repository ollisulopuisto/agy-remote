"""The session spawner: the phone asks, the server clones, spawns, adopts."""

import asyncio
from pathlib import Path

import pytest

import agy_remote.spawner as spawner_mod
from agy_remote.config import RemoteConfig
from agy_remote.models import NewSessionRequest
from agy_remote.session_manager import SessionManager
from agy_remote.spawner import (
    SessionSpawner,
    SpawnerBusyError,
    SpawnerError,
    SpawnPlan,
    default_name_for_url,
    sanitize_name,
    unique_workdir,
    validate_repo_url,
)

# ---------------------------------------------------------------------------
# Validation and naming: everything the phone controls, checked before use.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/org/repo",
        "https://github.com/org/repo.git",
        "git://host/org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git@github.com:org/repo",
    ],
)
def test_allowed_repo_urls_pass(url):
    assert validate_repo_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "file:///Users/me/repo",
        "http://github.com/org/repo",
        "ftp://host/repo",
        "/Users/me/repo",
        "relative/path",
        "https://example.com/o/r extra",
    ],
)
def test_disallowed_repo_urls_are_rejected(url):
    with pytest.raises(SpawnerError):
        validate_repo_url(url)


def test_names_are_sanitized_to_the_safe_alphabet():
    assert sanitize_name("My Repo!") == "My-Repo"
    assert sanitize_name("  --weird--  ") == "weird"
    assert sanitize_name("a/b\\c") == "a-b-c"
    assert sanitize_name("!!!") == ""


def test_the_default_name_comes_from_the_repo_url():
    assert default_name_for_url("https://github.com/org/myrepo.git") == "myrepo"
    assert default_name_for_url("git@github.com:org/myrepo") == "myrepo"
    assert default_name_for_url("https://gitlab.com/group/sub/repo") == "repo"


def test_a_second_workdir_gets_a_suffix(tmp_path: Path):
    root = tmp_path / "projects"
    root.mkdir()
    (root / "repo").mkdir()
    assert unique_workdir(root, "repo") == root / "repo-2"
    (root / "repo-2").mkdir()
    assert unique_workdir(root, "repo") == root / "repo-3"


def _plan(**overrides) -> SpawnPlan:
    base = dict(
        repo_url="https://github.com/org/repo",
        branch=None,
        task=None,
        name="repo",
        workdir=Path("/tmp/projects/repo"),
        tmux_session="agy-remote-repo",
    )
    base.update(overrides)
    return SpawnPlan(**base)


def test_the_seed_is_the_task_when_the_server_clones():
    assert _plan(task="Fix the build").seed == "Fix the build"
    assert _plan().seed is None


def test_the_seed_carries_the_clone_instruction_when_the_agent_clones():
    seed = _plan(let_agent_clone=True, branch="dev", task="Fix the build").seed
    assert "Clone https://github.com/org/repo (branch dev)" in seed
    assert "Fix the build" in seed


# ---------------------------------------------------------------------------
# The run: fakes stand in for tmux and the screen, nothing real is started.
# ---------------------------------------------------------------------------


class _FakeTmux:
    def __init__(self, session_name, cmd=None, env=None, target=None, workdir=None):
        self.session_name = session_name
        self.cmd = cmd
        self.env = env or {}
        self.workdir = workdir
        self.injected: list[str] = []
        self._started.append(self)

    def has_session(self) -> bool:
        return False

    def start_detached(self) -> bool:
        return True

    def inject_input(self, text: str) -> bool:
        self.injected.append(text)
        return True


def _install_fakes(monkeypatch, capture_lines: list[str] | None = None):
    """Replace the tmux surface the spawner shells out through."""
    started: list[_FakeTmux] = []
    _FakeTmux._started = started
    monkeypatch.setattr(spawner_mod, "TmuxSupervisor", _FakeTmux)
    monkeypatch.setattr(spawner_mod, "is_tmux_available", lambda: True)
    monkeypatch.setattr(spawner_mod, "session_id_of", lambda name: "9")
    monkeypatch.setattr(spawner_mod, "publish_server_registration", lambda cfg: None)
    monkeypatch.setattr(spawner_mod, "capture_pane", lambda name: capture_lines or ["agy prompt", " idle"])
    monkeypatch.setattr(spawner_mod, "READY_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(spawner_mod, "READY_STABLE_POLLS", 2)
    return started


def _manager(tmp_path: Path, events: list):
    """A real manager with broadcast recorded instead of sent."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    async def record(payload):
        events.append(payload)

    mgr.broadcast = record
    return cfg, mgr


def test_a_spawn_clones_starts_adopts_and_seeds(tmp_path: Path, monkeypatch):
    events: list = []
    started = _install_fakes(monkeypatch)
    cfg, mgr = _manager(tmp_path, events)
    monkeypatch.setenv("AGY_REMOTE_PROJECTS_DIR", str(tmp_path / "projects"))

    spawner = SessionSpawner(cfg, mgr)

    async def fake_clone(plan):
        await mgr.broadcast(spawner._stage_event(plan, "cloning"))

    spawner._clone = fake_clone

    async def scenario():
        result = spawner.create(
            NewSessionRequest(repo_url="https://github.com/org/myrepo", task="Fix the build", name="My Repo!")
        )
        assert result["status"] == "spawning"
        assert result["name"] == "My-Repo"
        assert result["workdir"] == str(tmp_path / "projects" / "My-Repo")
        await spawner._task
        return result

    asyncio.run(scenario())

    workdir = tmp_path / "projects" / "My-Repo"
    assert workdir.is_dir()

    (supervisor,) = started
    assert supervisor.session_name == "agy-remote-My-Repo"
    assert supervisor.workdir == str(workdir)
    # The session id is how the agent signs its mailbox messages later.
    assert supervisor.env["AGY_REMOTE_SESSION_ID"] == "agy-remote-My-Repo"
    assert supervisor.injected == ["Fix the build"]

    # Adoption moved the server's routing onto the new session.
    assert cfg.tmux_session == "agy-remote-My-Repo"
    assert cfg.tmux_session_id == "9"
    assert mgr.terminal is not None

    stages = [e["data"]["stage"] for e in events if e["event"] == "session_spawning"]
    assert stages == ["cloning", "starting"]
    (created,) = [e for e in events if e["event"] == "session_created"]
    assert created["data"]["ready"] is True
    assert created["data"]["name"] == "My-Repo"


def test_a_spawn_registers_the_session_in_the_registry(tmp_path: Path, monkeypatch):
    """1.1 core: a phone-spawned session is a first-class registry entry, not just globals.

    New per-session routing (keys, prompts, screen) resolves through
    SessionManager's registry, so the spawner must register a SessionRecord
    it can look up by tmux name, with its supervisor and screen mirror.
    """
    events: list = []
    started = _install_fakes(monkeypatch)
    cfg, mgr = _manager(tmp_path, events)
    monkeypatch.setenv("AGY_REMOTE_PROJECTS_DIR", str(tmp_path / "projects"))

    spawner = SessionSpawner(cfg, mgr)
    spawner._clone = _no_clone

    async def scenario():
        spawner.create(NewSessionRequest(repo_url="https://github.com/org/myrepo", name="My Repo!"))
        await spawner._task

    asyncio.run(scenario())

    workdir = tmp_path / "projects" / "My-Repo"
    (supervisor,) = started

    (record,) = mgr.list_sessions()
    assert record.tmux_name == "agy-remote-My-Repo"
    assert str(record.workdir) == str(workdir)

    # Per-session routing must find this session's supervisor and screen by name.
    assert mgr.get_supervisor("agy-remote-My-Repo") is supervisor
    assert mgr.get_screen_mirror("agy-remote-My-Repo") is not None


def test_a_failed_clone_reports_and_leaves_nothing_started(tmp_path: Path, monkeypatch):
    events: list = []
    started = _install_fakes(monkeypatch)
    cfg, mgr = _manager(tmp_path, events)
    monkeypatch.setenv("AGY_REMOTE_PROJECTS_DIR", str(tmp_path / "projects"))

    spawner = SessionSpawner(cfg, mgr)

    async def failing_clone(plan):
        await mgr.broadcast(spawner._stage_event(plan, "cloning"))
        raise SpawnerError("fatal: repository not found")

    spawner._clone = failing_clone

    async def scenario():
        spawner.create(NewSessionRequest(repo_url="https://github.com/org/missing"))
        await spawner._task

    asyncio.run(scenario())

    assert started == []
    (failed,) = [e for e in events if e["event"] == "session_spawning" and e["data"]["stage"] == "failed"]
    assert "repository not found" in failed["data"]["error"]
    assert not any(e["event"] == "session_created" for e in events)
    # The slot is free again: a failed spawn must not wedge the server.
    assert spawner.spawning is False


def test_a_dying_tmux_session_is_a_failure_not_a_timeout(tmp_path: Path, monkeypatch):
    events: list = []
    _install_fakes(monkeypatch, capture_lines=None)
    cfg, mgr = _manager(tmp_path, events)
    monkeypatch.setenv("AGY_REMOTE_PROJECTS_DIR", str(tmp_path / "projects"))

    spawner = SessionSpawner(cfg, mgr)
    spawner._clone = _no_clone
    monkeypatch.setattr(spawner_mod, "capture_pane", lambda name: None)

    async def scenario():
        spawner.create(NewSessionRequest(repo_url="https://github.com/o/r"))
        await spawner._task

    asyncio.run(scenario())

    (failed,) = [e for e in events if e["event"] == "session_spawning" and e["data"]["stage"] == "failed"]
    assert "died" in failed["data"]["error"]


def test_a_second_spawn_while_one_runs_is_refused(tmp_path: Path, monkeypatch):
    events: list = []
    _install_fakes(monkeypatch)
    cfg, mgr = _manager(tmp_path, events)
    monkeypatch.setenv("AGY_REMOTE_PROJECTS_DIR", str(tmp_path / "projects"))

    spawner = SessionSpawner(cfg, mgr)

    async def slow(plan):
        await asyncio.sleep(30)

    spawner._run = slow

    async def scenario():
        spawner.create(NewSessionRequest(repo_url="https://github.com/o/r1"))
        assert spawner.spawning
        with pytest.raises(SpawnerBusyError):
            spawner.create(NewSessionRequest(repo_url="https://github.com/o/r2"))
        spawner._task.cancel()
        spawner._task = None

    asyncio.run(scenario())


async def _no_clone(plan):
    """A spawn with nothing to clone: the workdir exists, the agent clones."""


# ---------------------------------------------------------------------------
# The endpoint: sealing, auth, validation.
# ---------------------------------------------------------------------------


def test_sessions_endpoint_rejects_disallowed_urls(tmp_path: Path):
    from fastapi.testclient import TestClient

    from agy_remote.server import create_app

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True, e2ee_enabled=False)
    client = TestClient(create_app(cfg))
    headers = {"X-Auth-Token": "secret123"}

    resp = client.post("/api/sessions", json={"repo_url": "file:///etc"}, headers=headers)
    assert resp.status_code == 400
    assert "not allowed" in resp.json()["detail"]

    # No token, no spawn.
    assert client.post("/api/sessions", json={"repo_url": "https://x.com/o/r"}).status_code == 401


def test_sessions_endpoint_refuses_plaintext_when_e2ee_is_on(tmp_path: Path, monkeypatch):
    """The spawn request carries the task the agent will act on: same sealing rule as the prompt."""
    from fastapi.testclient import TestClient

    from agy_remote.crypto import decode_key, encrypt_payload
    from agy_remote.server import create_app

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    client = TestClient(create_app(cfg))
    headers = {"X-Auth-Token": "secret123"}

    plain = client.post("/api/sessions", json={"repo_url": "https://x.com/o/r"}, headers=headers)
    assert plain.status_code == 400
    assert "encrypt" in plain.json()["detail"].lower()

    # A sealed body gets through the envelope and reaches validation:
    # the stand-in tmux check refuses it, which proves the payload was opened.
    monkeypatch.setattr(spawner_mod, "is_tmux_available", lambda: False)
    sealed = encrypt_payload({"repo_url": "https://x.com/o/r"}, decode_key(cfg.e2ee_key))
    opened = client.post("/api/sessions", json=sealed, headers=headers)
    assert opened.status_code == 400
    assert "tmux" in opened.json()["detail"]
