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


def test_only_sessions_actually_running_agy_are_offered(monkeypatch):
    """A tmux session is not evidence of an agent; the pane's command is."""
    fake = FakeRun(
        {
            "list-panes": "work agy\nnotes zsh\nagy-remote agy\nbuild vim\n",
        }
    )
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)

    assert tmux_runner.sessions_running("agy") == ["work", "agy-remote"]
    # Asked tmux about every session, not just the current one.
    assert any("-a" in call for call in fake.calls), fake.calls


def test_no_tmux_server_is_not_an_error(monkeypatch):
    """`tmux list-panes` exits non-zero when no server is running at all."""
    fake = FakeRun({}, fail={"list-panes"})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)

    assert tmux_runner.sessions_running("agy") == []


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
    fake = FakeRun({"list-panes": "work agy\n", "has-session": ""})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    seen = _stub_attach(monkeypatch, tmp_path, 8791)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--host", "127.0.0.1", "-p", "8791"])

    assert res.exit_code == 0, res.output
    assert not any("new-session" in call for call in fake.calls), fake.calls
    # Typing goes to the adopted session, and its screen is mirrored back.
    adopted = tmux_runner.get_tmux_supervisor()
    assert adopted is not None and adopted.session_name == "work"
    assert seen["screens"] and seen["screens"][0].session_name == "work"
    assert seen["served"], "the server never started"


def test_attach_with_nothing_to_attach_to_says_so(monkeypatch, tmp_path: Path):
    """Exiting 0 with no session would look like it worked."""
    monkeypatch.setattr(tmux_runner.subprocess, "run", FakeRun({"list-panes": "notes zsh\n"}))
    _stub_attach(monkeypatch, tmp_path, 8792)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--host", "127.0.0.1", "-p", "8792"])

    assert res.exit_code == 2, res.output
    assert "no tmux session" in res.output.lower()
    # Says how to make one, rather than leaving the user to guess.
    assert "tmux new-session" in res.output


def test_attach_will_not_guess_between_two_sessions(monkeypatch, tmp_path: Path):
    """Picking the wrong one silently drives the wrong agent."""
    monkeypatch.setattr(tmux_runner.subprocess, "run", FakeRun({"list-panes": "work agy\nspike agy\n"}))
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
    fake = FakeRun({"list-panes": "work zsh\n", "has-session": ""})
    monkeypatch.setattr(tmux_runner.subprocess, "run", fake)
    seen = _stub_attach(monkeypatch, tmp_path, 8794)

    res = CliRunner().invoke(cli_mod.cli, ["attach", "--session", "work", "--host", "127.0.0.1", "-p", "8794"])

    assert res.exit_code == 0, res.output
    assert seen["screens"] and seen["screens"][0].session_name == "work"


def test_attach_refuses_a_session_that_does_not_exist(monkeypatch, tmp_path: Path):
    """A typo must not start a server that can never type anywhere."""
    fake = FakeRun({"list-panes": "work agy\n"}, fail={"has-session"})
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
    fake = FakeRun({"list-panes": "notes zsh\n"})
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
