"""Adopting an agy session that was already running.

You cannot attach to another process's controlling terminal after the fact, so
an agy started in a plain shell can be read (its transcript is on disk) and can
raise approvals (its hook finds the server through the state file) but can never
be typed at. Inside tmux it can: `send-keys` writes to the pane and
`capture-pane` reads it back, both by session name, from a process that has
nothing to do with the one that started it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

import agy_remote.cli  # noqa: F401
from agy_remote import config as config_mod
from agy_remote import tmux_runner
from agy_remote.config import RemoteConfig
from agy_remote.screen import TmuxScreen

cli_mod = sys.modules["agy_remote.cli"]


class FakeRun:
    """Stands in for `subprocess.run`, answering by argv."""

    def __init__(self, answers: dict[str, str], fail: set[str] | None = None) -> None:
        self.answers = answers
        self.fail = fail or set()
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):  # noqa: ANN001, ANN204
        self.calls.append(list(argv))
        verb = argv[1] if len(argv) > 1 else ""

        class Result:
            returncode = 1 if verb in self.fail else 0
            stdout = self.answers.get(verb, "")
            stderr = ""

        return Result()


# ---------------------------------------------------------------------------
# Finding the session
# ---------------------------------------------------------------------------


def test_agy_is_found_by_pane_not_by_session(monkeypatch):
    """A session is the wrong unit: keys go to its *active* pane.

    A real desk had three agy panes inside one interactive session (0:3.0,
    0:4.0, 0:9.0). Collapsing those to the session name "0" meant `send-keys -t
    0` would type into whichever window happened to be on screen -- a shell,
    most likely, since the person was working in it.
    """
    fake = FakeRun(
        {"list-panes": "work:0.0 work agy\nnotes:1.0 notes zsh\n0:3.0 0 agy\n0:9.0 0 agy\nbuild:0.0 build vim\n"}
    )
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)

    found = tmux_runner.panes_running("agy")
    assert [p["target"] for p in found] == ["work:0.0", "0:3.0", "0:9.0"]
    assert found[1]["session"] == "0"
    # Asked tmux about every session, not just the current one.
    assert any("-a" in call for call in fake.calls), fake.calls


def test_no_tmux_server_is_not_an_error(monkeypatch):
    """`tmux list-panes` exits non-zero when no server is running at all."""
    fake = FakeRun({}, fail={"list-panes"})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)

    assert tmux_runner.panes_running("agy") == []


def test_keys_and_screen_address_the_pane(monkeypatch):
    """Adopting a pane means typing into that pane, not its neighbour."""
    fake = FakeRun({"has-session": ""})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)

    sup = tmux_runner.TmuxSupervisor(session_name="0", target="0:3.0")
    assert sup.inject_input("hello") is True
    assert sup.send_key("shift_tab") is True

    targeted = [c for c in fake.calls if "send-keys" in c]
    assert targeted, fake.calls
    for call in targeted:
        assert "0:3.0" in call, call
        assert call[call.index("-t") + 1] == "0:3.0"

    tmux_runner.capture_pane("0:3.0")
    capture = next(c for c in fake.calls if "capture-pane" in c)
    assert capture[capture.index("-t") + 1] == "0:3.0"


# ---------------------------------------------------------------------------
# Reading the screen back
# ---------------------------------------------------------------------------


def test_the_screen_is_read_from_the_pane_and_carries_the_mode():
    """Shift+Tab's only report is the status bar, which lives in the pane."""
    frames = [
        ["> build me a thing", "", "  ~/src            accept-edits        2 files"],
        ["> build me a thing", "", "  ~/src            plan                2 files"],
    ]
    taken: list[int] = []

    def capture(session: str) -> list[str] | None:
        taken.append(len(taken))
        return frames[min(len(taken) - 1, len(frames) - 1)]

    screen = TmuxScreen("work", capture=capture, geometry=lambda s: {"rows": 24, "cols": 80}, min_interval=0.0)

    first = screen.snapshot()
    assert first["mode"] == "accept-edits"
    assert first["rows"] == 24 and first["cols"] == 80
    assert first["lines"][0] == "> build me a thing"

    assert screen.snapshot()["mode"] == "plan"


def test_an_unchanged_screen_is_not_rebroadcast():
    """The watch loop asks three times a second; a still screen must cost nothing."""
    screen = TmuxScreen(
        "work",
        capture=lambda s: ["nothing is happening"],
        geometry=lambda s: {"rows": 24, "cols": 80},
        min_interval=0.0,
    )

    assert screen.take_dirty_snapshot() is not None
    assert screen.take_dirty_snapshot() is None
    assert screen.take_dirty_snapshot() is None


def test_a_session_that_disappeared_reports_no_screen():
    """Someone can kill the session out from under us at any moment."""
    screen = TmuxScreen("gone", capture=lambda s: None, geometry=lambda s: None, min_interval=0.0)

    assert screen.take_dirty_snapshot() is None
    assert screen.snapshot()["lines"] == []


def test_captures_are_throttled_below_the_watch_loop_interval():
    """Every capture is two subprocess spawns; the loop ticks every 0.3s."""
    calls: list[int] = []

    def capture(session: str) -> list[str]:
        calls.append(1)
        return [f"frame {len(calls)}"]

    screen = TmuxScreen("work", capture=capture, geometry=lambda s: {"rows": 24, "cols": 80}, min_interval=60.0)

    screen.take_dirty_snapshot()
    screen.take_dirty_snapshot()
    screen.take_dirty_snapshot()

    assert len(calls) == 1, "throttle let extra captures through"


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _stub_attach(monkeypatch, tmp_path: Path, port: int) -> dict[str, list]:
    seen: dict[str, list] = {"banner": [], "screens": [], "served": []}

    def fake_config(**kw) -> RemoteConfig:
        return RemoteConfig(
            brain_dir=tmp_path,
            host="127.0.0.1",
            port=port,
            auth_token="secret123",
            tailscale_ip=None,
            tailscale_bin=None,
            lan_ip="127.0.0.1",
        )

    class RecordingManager:
        terminal = None

        def attach_screen(self, mirror) -> None:  # noqa: ANN001
            seen["screens"].append(mirror)

    class FakeApp:
        class state:
            session_manager = RecordingManager()

    monkeypatch.setattr(cli_mod, "get_config", fake_config)
    monkeypatch.setattr(cli_mod, "_setup_tls", lambda cfg, tls: None)
    monkeypatch.setattr(cli_mod, "_warn_if_hooks_unwired", lambda: None)
    monkeypatch.setattr(cli_mod, "_warn_if_second_instance", lambda cfg: None)
    monkeypatch.setattr(cli_mod, "print_banner", lambda cfg, mode="": seen["banner"].append(mode))
    monkeypatch.setattr(cli_mod, "create_app", lambda cfg: FakeApp)
    monkeypatch.setattr(cli_mod, "_serve_forever", lambda cfg, app: seen["served"].append(cfg))
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", tmp_path / "runtime.json")
    # Adoption publishes a server registration; without this the suite writes
    # into the real registry under ~/.gemini and leaves entries for ports that
    # only ever existed in a test.
    monkeypatch.setattr(config_mod, "SERVER_REGISTRY_DIR", tmp_path / "servers")
    return seen


def test_attach_adopts_the_session_and_never_creates_one(monkeypatch, tmp_path: Path):
    """The whole point: the agy on screen keeps running, ours is not started."""
    fake = FakeRun({"list-panes": "work:0.0 work agy\n", "has-session": ""})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    seen = _stub_attach(monkeypatch, tmp_path, 8791)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--host", "127.0.0.1", "-p", "8791"])

    assert res.exit_code == 0, res.output
    assert not any("new-session" in call for call in fake.calls), fake.calls
    # Typing goes to the adopted session, and its screen is mirrored back.
    adopted = tmux_runner.get_tmux_supervisor()
    assert adopted is not None and adopted.session_name == "work"
    assert seen["screens"] and seen["screens"][0].target == "work:0.0"
    assert seen["served"], "the server never started"


def test_attach_with_nothing_to_attach_to_says_so(monkeypatch, tmp_path: Path):
    """Exiting 0 with no session would look like it worked."""
    monkeypatch.setattr(tmux_runner.subprocess, "run", FakeRun({"list-panes": "notes:0.0 notes zsh\n"}))
    _stub_attach(monkeypatch, tmp_path, 8792)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--host", "127.0.0.1", "-p", "8792"])

    assert res.exit_code == 2, res.output
    assert "no tmux session" in res.output.lower()
    # Says how to make one, rather than leaving the user to guess.
    assert "tmux new-session" in res.output


def test_attach_will_not_guess_between_two_sessions(monkeypatch, tmp_path: Path):
    """Picking the wrong one silently drives the wrong agent."""
    monkeypatch.setattr(
        tmux_runner.subprocess, "run", FakeRun({"list-panes": "work:0.0 work agy\nspike:1.0 spike agy\n"})
    )
    _stub_attach(monkeypatch, tmp_path, 8793)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--host", "127.0.0.1", "-p", "8793"])

    assert res.exit_code == 2, res.output
    assert "work" in res.output and "spike" in res.output
    assert "--session" in res.output


def test_attach_takes_the_session_it_was_given(monkeypatch, tmp_path: Path):
    """A named session is adopted even when the pane command is not agy.

    `agy` under a wrapper, a shell, or a different name is still the session
    the user means -- they named it.
    """
    fake = FakeRun({"list-panes": "work:0.0 work zsh\n", "has-session": ""})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    seen = _stub_attach(monkeypatch, tmp_path, 8794)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--session", "work", "--host", "127.0.0.1", "-p", "8794"])

    assert res.exit_code == 0, res.output
    # No agy pane to aim at, so the session itself is the target -- the same
    # behaviour as before panes were addressed, and the best available here.
    assert seen["screens"] and seen["screens"][0].target == "work"


def test_attach_refuses_a_session_that_does_not_exist(monkeypatch, tmp_path: Path):
    """A typo must not start a server that can never type anywhere."""
    fake = FakeRun({"list-panes": "work:0.0 work agy\n"}, fail={"has-session"})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    _stub_attach(monkeypatch, tmp_path, 8795)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--session", "typo", "--host", "127.0.0.1", "-p", "8795"])

    assert res.exit_code == 2, res.output
    assert "typo" in res.output


# ---------------------------------------------------------------------------
# Always-on: serve first, agy later
# ---------------------------------------------------------------------------


def test_wait_serves_even_with_nothing_to_adopt(monkeypatch, tmp_path: Path):
    """A boot service cannot refuse to start because you have not opened agy yet.

    Without `--wait` this exits 2, which is right when a person typed it and
    wrong for launchd: boot order is not something you get to depend on.
    """
    fake = FakeRun({"list-panes": "notes:0.0 notes zsh\n"})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    seen = _stub_attach(monkeypatch, tmp_path, 8796)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--wait", "--host", "127.0.0.1", "-p", "8796"])

    assert res.exit_code == 0, res.output
    assert seen["served"], "the server never started"
    # Nothing was adopted and nothing was created: it is just listening.
    assert not any("new-session" in call for call in fake.calls), fake.calls


@pytest.mark.asyncio
async def test_the_first_phone_to_connect_gets_a_session(tmp_path: Path):
    """Tapping the home-screen icon is the whole interaction.

    With the Mac always listening and no agy running, a connection has nothing
    to drive. Rather than showing an empty transcript, the first client to
    arrive gets a session started for it and adopted.
    """
    from agy_remote.config import RemoteConfig
    from agy_remote.session_manager import SessionManager

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="token", e2ee_enabled=False)
    mgr = SessionManager(cfg)
    started: list[int] = []

    async def ensure() -> None:
        started.append(1)

    mgr.ensure_session = ensure

    class _Ws:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, data) -> None:  # noqa: ANN001
            self.sent.append(data)

    await mgr.register_client(_Ws())
    assert started == [1], "no session was started for the first client"

    # A second device joins the one that is already there.
    await mgr.register_client(_Ws())
    assert started == [1], "a second device started another session"


@pytest.mark.asyncio
async def test_a_session_that_died_is_replaced_not_mourned(monkeypatch, tmp_path: Path):
    """The adopted session can be killed at any moment; the server outlives it.

    `--wait` exists so a session appears when someone wants one. Once adopted,
    the server held that name forever: kill the tmux session and the next phone
    to connect got a server that reported a supervisor it no longer had, typed
    into nothing, and mirrored an empty screen.
    """
    from agy_remote.config import RemoteConfig

    # `has-session` fails: the session named in the config is gone.
    fake = FakeRun({"list-panes": "", "display-message": "$9\n"}, fail={"has-session"})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    monkeypatch.setattr(config_mod, "SERVER_REGISTRY_DIR", tmp_path / "servers")

    cfg = RemoteConfig(brain_dir=tmp_path, port=8765, auth_token="t", tmux_session="dead-one")

    class Mgr:
        def attach_screen(self, mirror) -> None:  # noqa: ANN001
            self.mirror = mirror

    mgr = Mgr()
    await cli_mod._adopt_when_a_phone_arrives(cfg, mgr)

    assert cfg.tmux_session != "dead-one", "kept driving a session that no longer exists"
    assert any("new-session" in call for call in fake.calls), "no replacement was started"
    assert mgr.mirror.target == cfg.tmux_target


@pytest.mark.asyncio
async def test_a_living_session_is_left_alone(monkeypatch, tmp_path: Path):
    """Re-adopting a session that is fine would start a second agy beside it."""
    from agy_remote.config import RemoteConfig

    fake = FakeRun({"has-session": "", "list-panes": "alive:0.0 alive agy\n"})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    cfg = RemoteConfig(brain_dir=tmp_path, port=8765, auth_token="t", tmux_session="alive")

    await cli_mod._adopt_when_a_phone_arrives(cfg, object())

    assert cfg.tmux_session == "alive"
    assert not any("new-session" in call for call in fake.calls), fake.calls


def test_the_port_conflict_names_the_session_that_server_actually_drives(monkeypatch, tmp_path: Path):
    """ "tmux attach -t agy-remote" was a guess made from the port number.

    A server that adopted `work` was advertised at `agy-remote`, and a server
    whose session had since died sent the user to one that does not exist --
    which is exactly what happened: `can't find session: agy-remote`.
    """
    import json as _json
    import socket as _socket

    reg = tmp_path / "servers"
    reg.mkdir()
    monkeypatch.setattr(config_mod, "SERVER_REGISTRY_DIR", reg)
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", tmp_path / "runtime.json")
    monkeypatch.setattr(config_mod, "_pid_alive", lambda pid: True)

    held = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    held.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen(8)
    port = held.getsockname()[1]
    (reg / f"{port}.json").write_text(_json.dumps({"port": port, "pid": 4242, "tmux_session": "work"}))

    try:
        _stub_attach(monkeypatch, tmp_path, port)

        # The session it named is alive: point at it, by its real name.
        monkeypatch.setattr(tmux_runner.subprocess, "run", FakeRun({"has-session": ""}))
        alive = CliRunner().invoke(cli_mod.cli, ["serve", "--host", "127.0.0.1", "-p", str(port)], input="n\n")
        assert "tmux attach -t work" in alive.output, alive.output

        # And when that session is gone, do not send anyone to it.
        monkeypatch.setattr(tmux_runner.subprocess, "run", FakeRun({}, fail={"has-session"}))
        gone = CliRunner().invoke(cli_mod.cli, ["serve", "--host", "127.0.0.1", "-p", str(port)], input="n\n")
        assert "tmux attach" not in gone.output, gone.output
        assert "already in use" in gone.output
    finally:
        held.close()
