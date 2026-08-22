import pytest

import agy_remote.config as config_mod


@pytest.fixture(autouse=True)
def reset_config_singleton(monkeypatch):
    """Ensure every test starts and finishes with a clean config singleton."""
    config_mod.config_instance = None
    yield
    config_mod.config_instance = None


@pytest.fixture(autouse=True)
def reset_supervisor_globals():
    """No test may inherit another's supervisor.

    `set_tmux_supervisor` / `set_pty_supervisor` write module-level globals, so
    a supervisor registered by an attach test outlived it -- and `has_session()`
    shells out to the real tmux, which on a developer's machine may well have a
    session by that name. The suite then passed or failed depending on what the
    developer happened to have running.
    """
    from agy_remote import pty_runner, tmux_runner

    tmux_runner.tmux_instance = None
    pty_runner.pty_instance = None
    yield
    tmux_runner.tmux_instance = None
    pty_runner.pty_instance = None
