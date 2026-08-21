"""Two agy processes, two servers, two URLs.

Each server supervises its own agy, and each agy's PreToolUse hook must reach
*its own* server. The hook resolves one endpoint from one shared state file, so
without a per-process signal every approval on the host lands on whichever
server published that file -- the second phone would show nothing, and the
first would answer for a session it is not showing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

import agy_remote.cli  # noqa: F401
from agy_remote import config as config_mod
from agy_remote import hooks as hooks_mod
from agy_remote.config import RemoteConfig

cli_mod = sys.modules["agy_remote.cli"]


def _cfg(tmp_path: Path, port: int, **kw) -> RemoteConfig:
    base = dict(
        brain_dir=tmp_path,
        host="127.0.0.1",
        port=port,
        auth_token="shared-token",
        tailscale_ip=None,
        tailscale_bin=None,
        tailscale_dns_name=None,
        lan_ip="127.0.0.1",
    )
    base.update(kw)
    return RemoteConfig(**base)


def test_hook_prefers_the_server_that_launched_this_agy(monkeypatch, tmp_path: Path):
    """The env wins over the shared file: it is the only per-process signal."""
    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)
    # The first server owns the shared file.
    state_file.write_text(json.dumps({"auth_token": "shared-token", "base_url": "http://127.0.0.1:8765", "port": 8765}))

    monkeypatch.setenv("AGY_REMOTE_URL", "http://127.0.0.1:8766")
    monkeypatch.delenv("AGY_REMOTE_TOKEN", raising=False)

    base_url, token = hooks_mod.resolve_server_endpoint()

    assert base_url == "http://127.0.0.1:8766"
    # The token is a property of the host, so the second server accepts it too.
    assert token == "shared-token"


def test_hook_falls_back_to_the_shared_file_when_agy_was_started_by_hand(monkeypatch, tmp_path: Path):
    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)
    state_file.write_text(
        json.dumps({"auth_token": "shared-token", "base_url": "https://host.ts.net:8765", "port": 8765})
    )
    monkeypatch.delenv("AGY_REMOTE_URL", raising=False)
    monkeypatch.delenv("AGY_REMOTE_TOKEN", raising=False)

    assert hooks_mod.resolve_server_endpoint() == ("https://host.ts.net:8765", "shared-token")


def test_hook_reads_the_stored_token_when_no_server_published_state(monkeypatch, tmp_path: Path):
    """A second server does not publish state, so its hook has no file token.

    Minting one instead (as `get_config` would) 401s on every approval.
    """
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", tmp_path / "absent.json")
    creds = tmp_path / "credentials.json"
    creds.write_text(json.dumps({"auth_token": "stored-token", "e2ee_key": "k"}))
    monkeypatch.setattr(config_mod, "CREDENTIALS_FILE", creds)
    monkeypatch.setenv("AGY_REMOTE_URL", "http://127.0.0.1:8766")
    monkeypatch.delenv("AGY_REMOTE_TOKEN", raising=False)

    base_url, token = hooks_mod.resolve_server_endpoint()

    assert base_url == "http://127.0.0.1:8766"
    assert token == "stored-token"
    # Read-only: a hook must never mint credentials of its own.
    assert json.loads(creds.read_text())["auth_token"] == "stored-token"


def test_child_env_points_at_this_server_and_carries_no_secret(tmp_path: Path):
    """The URL is enough. The token would end up in argv under tmux, where
    every local user can read it out of `ps`."""
    env = cli_mod.agy_child_env(_cfg(tmp_path, 8766))

    assert env["AGY_REMOTE_URL"] == "http://127.0.0.1:8766"
    assert env["AGY_REMOTE_PORT"] == "8766"
    assert not any("token" in k.lower() for k in env)
    assert "shared-token" not in " ".join(env.values())


def test_child_env_uses_the_tls_name_so_the_hook_can_verify_the_certificate(tmp_path: Path):
    cfg = _cfg(tmp_path, 8766, tailscale_dns_name="host.ts.net")
    cfg.tls_cert = tmp_path / "cert.pem"
    cfg.tls_key = tmp_path / "key.pem"

    assert cli_mod.agy_child_env(cfg)["AGY_REMOTE_URL"] == "https://host.ts.net:8766"


def test_tmux_session_carries_the_env_into_agy(monkeypatch):
    """tmux does not inherit our environment, so it must be passed explicitly."""
    from agy_remote.tmux_runner import TmuxSupervisor

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 1 if cmd[1] == "has-session" else 0

        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("agy_remote.tmux_runner.is_tmux_available", lambda: True)

    sup = TmuxSupervisor(
        session_name="agy-remote-8766",
        cmd=["agy"],
        env={"AGY_REMOTE_URL": "http://127.0.0.1:8766"},
    )
    sup.start_or_attach()

    new_session = next(c for c in calls if c[1] == "new-session")
    assert "AGY_REMOTE_URL=http://127.0.0.1:8766" in " ".join(new_session)


def test_pty_supervisor_accepts_an_env_for_the_child():
    from agy_remote.pty_runner import PtySupervisor

    sup = PtySupervisor(cmd=["agy"], env={"AGY_REMOTE_URL": "http://127.0.0.1:8766"})
    assert sup.env == {"AGY_REMOTE_URL": "http://127.0.0.1:8766"}


def test_qr_can_target_a_second_instance(monkeypatch, tmp_path: Path):
    """`agy-remote qr` reads the shared state file, which the second server
    does not own -- without --port it can only ever pair the first one."""
    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)
    state_file.write_text(json.dumps({"auth_token": "shared-token", "base_url": "http://127.0.0.1:8765", "port": 8765}))
    monkeypatch.setattr(cli_mod, "get_config", lambda **kw: _cfg(tmp_path, 8765))
    monkeypatch.setattr(cli_mod, "print_qr_code", lambda url: None)

    res = CliRunner().invoke(cli_mod.cli, ["qr", "--port", "8766"])

    assert res.exit_code == 0, res.output
    assert "8766" in res.output
    assert ":8765" not in res.output
