"""Which server a hand-started agy's approvals reach.

`AGY_REMOTE_URL` is exported into an agy the server launched, so that agy's
hook always finds its own server. An agy nobody launched -- one adopted from
tmux, or started in another terminal -- has no such parent and fell back to the
host-wide state file, which exactly one server owns. With two servers running,
both sessions' approvals went to whichever wrote that file last.

Inside tmux the hook can identify itself for free: `$TMUX` is
`socket,server_pid,session_id`, inherited by every process in the pane. A
server that adopted a session publishes that id, and the hook matches on it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agy_remote import config as config_mod
from agy_remote import hooks as hooks_mod
from agy_remote.config import RemoteConfig


def _registry(monkeypatch, tmp_path: Path) -> Path:
    reg = tmp_path / "servers"
    monkeypatch.setattr(config_mod, "SERVER_REGISTRY_DIR", reg)
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", tmp_path / "runtime.json")
    return reg


def _cfg(tmp_path: Path, port: int, session: str | None, session_id: str | None) -> RemoteConfig:
    return RemoteConfig(
        brain_dir=tmp_path,
        port=port,
        auth_token="host-token",
        tailscale_ip=None,
        tailscale_bin=None,
        lan_ip="127.0.0.1",
        tmux_session=session,
        tmux_session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Identifying the session for free
# ---------------------------------------------------------------------------


def test_the_session_id_comes_out_of_the_environment():
    """No subprocess: the hook runs on every single tool call."""
    from agy_remote.tmux_runner import session_id_from_env

    assert session_id_from_env("/private/tmp/tmux-501/default,59421,4") == "4"
    assert session_id_from_env("/tmp/tmux-1000/default,900,0") == "0"
    # Not in tmux, or something unrecognisable: no claim either way.
    assert session_id_from_env(None) is None
    assert session_id_from_env("") is None
    assert session_id_from_env("nonsense") is None
    assert session_id_from_env("/tmp/sock,900,notanumber") is None


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_each_adopted_session_reaches_its_own_server(monkeypatch, tmp_path: Path):
    """Two servers, two tmux sessions: neither may answer for the other."""
    _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: True)

    config_mod.publish_server_registration(_cfg(tmp_path, 8765, "work", "3"))
    config_mod.publish_server_registration(_cfg(tmp_path, 8766, "spike", "7"))

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,59421,7")
    monkeypatch.delenv("AGY_REMOTE_URL", raising=False)
    url, token = hooks_mod.resolve_server_endpoint()
    assert ":8766" in url, url
    assert token == "host-token"

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,59421,3")
    assert ":8765" in hooks_mod.resolve_server_endpoint()[0]


def test_the_launching_server_still_wins(monkeypatch, tmp_path: Path):
    """`AGY_REMOTE_URL` is the only per-process signal and stays authoritative."""
    _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: True)
    config_mod.publish_server_registration(_cfg(tmp_path, 8766, "spike", "7"))

    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,59421,7")
    monkeypatch.setenv("AGY_REMOTE_URL", "https://mine:9999")
    assert hooks_mod.resolve_server_endpoint()[0] == "https://mine:9999"


def test_a_session_nobody_adopted_falls_back_to_the_shared_file(monkeypatch, tmp_path: Path):
    """One server and a hand-started agy is the ordinary case; keep it working."""
    _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: True)
    config_mod.publish_server_registration(_cfg(tmp_path, 8766, "spike", "7"))
    config_mod.write_runtime_state(_cfg(tmp_path, 8765, None, None))

    # In a tmux session no server claims.
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,59421,99")
    monkeypatch.delenv("AGY_REMOTE_URL", raising=False)
    assert ":8765" in hooks_mod.resolve_server_endpoint()[0]

    # And outside tmux entirely.
    monkeypatch.delenv("TMUX", raising=False)
    assert ":8765" in hooks_mod.resolve_server_endpoint()[0]


def test_a_dead_server_does_not_keep_answering(monkeypatch, tmp_path: Path):
    """Registrations outlive crashes; the pid is the only honest signal."""
    reg = _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: True)
    config_mod.publish_server_registration(_cfg(tmp_path, 8766, "spike", "7"))
    config_mod.write_runtime_state(_cfg(tmp_path, 8765, None, None))

    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: False)
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-501/default,59421,7")
    monkeypatch.delenv("AGY_REMOTE_URL", raising=False)

    # Falls through to the shared file rather than posting into the void.
    assert ":8766" not in hooks_mod.resolve_server_endpoint()[0]
    assert list(reg.glob("*.json")), "the stale file is evidence, not a bug"


def test_a_server_withdraws_its_registration_on_the_way_out(monkeypatch, tmp_path: Path):
    """A restarted server on the same port must not leave two claims."""
    reg = _registry(monkeypatch, tmp_path)
    cfg = _cfg(tmp_path, 8766, "spike", "7")
    config_mod.publish_server_registration(cfg)
    assert (reg / "8766.json").exists()

    config_mod.withdraw_server_registration(cfg.port, owner_pid=os.getpid())
    assert not (reg / "8766.json").exists()


def test_one_server_does_not_withdraw_another(monkeypatch, tmp_path: Path):
    """On a quick restart the outgoing process can outlive the incoming one."""
    reg = _registry(monkeypatch, tmp_path)
    config_mod.publish_server_registration(_cfg(tmp_path, 8766, "spike", "7"))
    written = json.loads((reg / "8766.json").read_text())

    config_mod.withdraw_server_registration(8766, owner_pid=written["pid"] + 1)
    assert (reg / "8766.json").exists()


def test_a_crashed_server_does_not_capture_hooks(monkeypatch, tmp_path: Path):
    """A state file outlives the server that wrote it.

    `runtime_state_owner` exists precisely because a crashed server leaves its
    credentials behind, but the hook read the file directly. It then posted
    every tool call to a port nobody is listening on -- fast to refuse today,
    but the moment anything else takes that port, agy blocks on it.
    """
    _registry(monkeypatch, tmp_path)
    monkeypatch.delenv("AGY_REMOTE_URL", raising=False)
    monkeypatch.delenv("TMUX", raising=False)

    cfg = _cfg(tmp_path, 8765, None, None)
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: True)
    config_mod.write_runtime_state(cfg)
    assert ":8765" in hooks_mod.resolve_server_endpoint()[0]

    # Same file, but the process it names is gone.
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: False)
    url, token = hooks_mod.resolve_server_endpoint()
    assert not token, f"handed the host token to a dead server: {url}"
