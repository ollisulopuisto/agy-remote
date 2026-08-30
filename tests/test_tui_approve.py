"""The desktop side of a held permission gate.

When a phone is connected, the PreToolUse hook holds and the PWA shows the
banner -- but the terminal agy runs in went silent, so whoever sat there had
no idea anything was waiting. The server surfaces the gate in a tmux popup
running `agy-remote tui-approve`; these tests pin that command's behavior.
"""

from __future__ import annotations

import importlib
import io
import json
import urllib.error
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from agy_remote.cli import cli
from agy_remote.config import RemoteConfig
from agy_remote.server import create_app

# `agy_remote/__init__` rebinds the package attribute `cli` to the click
# Group, so `import agy_remote.cli` would hand back the Group, not the module.
cli_mod = importlib.import_module("agy_remote.cli")


@pytest.fixture
def popup_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    env = {
        "AGY_REMOTE_URL": "http://127.0.0.1:8090",
        "AGY_REMOTE_TOKEN": "secret123",
        "AGY_REMOTE_APPROVAL_ID": "ap-1",
        "AGY_REMOTE_TOOL_NAME": "bash",
        "AGY_REMOTE_TOOL_ARGS": '{"command": "rm -rf /"}',
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def _post_capture(monkeypatch: pytest.MonkeyPatch, status: int = 200) -> list[dict]:
    posted: list[dict] = []

    class _Response:
        def __enter__(self):
            return io.StringIO(json.dumps({"status": "ok"}))

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None):
        posted.append({"url": req.full_url, "body": json.loads(req.data), "headers": dict(req.header_items())})
        if status != 200:
            raise urllib.error.HTTPError(req.full_url, status, "conflict", {}, io.StringIO())
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return posted


def test_without_context_it_says_nothing_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("AGY_REMOTE_URL", "AGY_REMOTE_APPROVAL_ID"):
        monkeypatch.delenv(key, raising=False)
    result = CliRunner().invoke(cli, ["tui-approve"])
    assert result.exit_code == 0
    assert "nothing to answer" in result.output.lower()


def test_an_a_keypress_sends_allow(popup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _post_capture(monkeypatch)
    monkeypatch.setattr(cli_mod, "_read_keypress", lambda timeout: "a")
    result = CliRunner().invoke(cli, ["tui-approve"])
    assert result.exit_code == 0
    assert "allow" in result.output.lower()
    assert posted, "the keypress was swallowed"
    assert posted[0]["body"]["decision"] == "allow"
    assert posted[0]["url"].endswith("/api/approvals/ap-1/respond")
    headers = {k.lower(): v for k, v in posted[0]["headers"].items()}
    assert headers["x-auth-token"] == "secret123"


def test_a_d_keypress_sends_deny(popup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _post_capture(monkeypatch)
    monkeypatch.setattr(cli_mod, "_read_keypress", lambda timeout: "d")
    result = CliRunner().invoke(cli, ["tui-approve"])
    assert posted[0]["body"]["decision"] == "deny"
    assert result.exit_code == 0


def test_the_phone_winning_the_race_is_not_an_error(popup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _post_capture(monkeypatch, status=409)
    monkeypatch.setattr(cli_mod, "_read_keypress", lambda timeout: "a")
    result = CliRunner().invoke(cli, ["tui-approve"])
    assert result.exit_code == 0
    assert "phone" in result.output.lower() or "already" in result.output.lower()


def test_no_keypress_leaves_the_decision_to_the_phone(popup_env, monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _post_capture(monkeypatch)
    monkeypatch.setattr(cli_mod, "_read_keypress", lambda timeout: "")
    result = CliRunner().invoke(cli, ["tui-approve"])
    assert result.exit_code == 0
    assert not posted, "an untouched popup must not decide"
    assert "phone" in result.output.lower()


def test_the_pending_approval_endpoint_answers_the_desktop(tmp_path: Path) -> None:
    """tui-approve reads the gate it is being asked about from the server."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    client = TestClient(create_app(cfg))
    headers = {"X-Auth-Token": "secret123"}

    assert client.get("/api/approvals/ap-1", headers=headers).status_code == 404

    import asyncio

    mgr = client.app.state.session_manager
    asyncio.get_event_loop_policy()
    asyncio.run(mgr.register_approval("ap-1", "conv-1", "bash", {"command": "ls"}))

    resp = client.get("/api/approvals/ap-1", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["tool_name"] == "bash"
    assert resp.json()["status"] == "pending"
