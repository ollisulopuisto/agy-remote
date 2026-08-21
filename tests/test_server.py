"""Unit tests for FastAPI REST and WebSocket endpoints."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.server import create_app


def test_server_status_and_auth(tmp_path: Path):
    cfg = RemoteConfig(
        brain_dir=tmp_path,
        auth_token="secret123",
        enable_auth=True,
    )
    app = create_app(cfg)
    client = TestClient(app)

    # Status without token shows auth_required
    resp = client.get("/api/status")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is False
    assert resp.json()["auth_required"] is True

    # Status with token
    resp = client.get("/api/status?token=secret123")
    assert resp.status_code == 200
    assert resp.json()["authenticated"] is True


def test_conversations_api(tmp_path: Path):
    conv_id = "test-conv-abc"
    conv_dir = tmp_path / conv_id / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True)
    with open(conv_dir / "transcript.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "Test prompt"}) + "\n")

    cfg = RemoteConfig(
        brain_dir=tmp_path,
        auth_token="secret123",
        enable_auth=True,
    )
    app = create_app(cfg)
    client = TestClient(app)

    # Unauthorized access
    resp = client.get("/api/conversations")
    assert resp.status_code == 401

    # Authorized access
    resp = client.get("/api/conversations?token=secret123")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == conv_id

    # Get conversation details
    resp = client.get(f"/api/conversations/{conv_id}?token=secret123")
    assert resp.status_code == 200
    assert resp.json()["id"] == conv_id
    assert len(resp.json()["steps"]) == 1

    # Traversal attack on conversation_id should return 404
    resp = client.get("/api/conversations/../../etc/passwd?token=secret123")
    assert resp.status_code == 404


def test_upload_security(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    app = create_app(cfg)
    client = TestClient(app)

    # Reject unauthenticated upload
    resp = client.post("/api/upload", files={"file": ("test.jpg", b"dummy", "image/jpeg")})
    assert resp.status_code == 401

    # Reject non-image extension
    resp = client.post(
        "/api/upload?token=secret123",
        files={"file": ("malicious.exe", b"malicious binary", "application/octet-stream")},
    )
    assert resp.status_code == 400

    # Accept valid image upload
    resp = client.post(
        "/api/upload?token=secret123",
        files={"file": ("screenshot.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
    )
    assert resp.status_code == 200
    assert "mobile_" in resp.json()["filename"]


# ---------------------------------------------------------------------------
# Key presses: text plus Enter cannot reach Shift+Tab, Esc, or a selection list.
# ---------------------------------------------------------------------------


class _RecordingSupervisor:
    """Stands in for a live PTY session and records what was pressed."""

    def __init__(self):
        self.running = True
        self.pressed = []

    def send_key(self, key: str) -> bool:
        from agy_remote.keys import is_known_key

        if not is_known_key(key):
            return False
        self.pressed.append(key)
        return True


@pytest.fixture
def live_session(monkeypatch):
    """Register a fake supervisor the way `agy-remote run` registers a real one."""
    import agy_remote.pty_runner as pty_runner
    import agy_remote.tmux_runner as tmux_runner

    supervisor = _RecordingSupervisor()
    monkeypatch.setattr(pty_runner, "pty_instance", supervisor)
    monkeypatch.setattr(tmux_runner, "tmux_instance", None)
    return supervisor


@pytest.fixture
def no_session(monkeypatch):
    """Watcher mode: nothing to type into."""
    import agy_remote.pty_runner as pty_runner
    import agy_remote.tmux_runner as tmux_runner

    monkeypatch.setattr(pty_runner, "pty_instance", None)
    monkeypatch.setattr(tmux_runner, "tmux_instance", None)


def _client(tmp_path: Path) -> TestClient:
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    return TestClient(create_app(cfg))


def test_key_press_reaches_the_supervised_session(tmp_path: Path, live_session):
    resp = _client(tmp_path).post("/api/key?token=secret123", json={"key": "shift_tab"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert live_session.pressed == ["shift_tab"]


def test_key_press_rejects_names_outside_the_allowlist(tmp_path: Path, live_session):
    resp = _client(tmp_path).post("/api/key?token=secret123", json={"key": "\x1b]0;pwn\x07"})
    assert resp.status_code == 422
    assert live_session.pressed == []


def test_key_press_requires_a_token(tmp_path: Path, live_session):
    resp = _client(tmp_path).post("/api/key", json={"key": "shift_tab"})
    assert resp.status_code in (401, 403)
    assert live_session.pressed == []


def test_key_press_without_a_supervisor_says_so(tmp_path: Path, no_session):
    """Watcher mode has no session to type into; that must not look like success."""
    resp = _client(tmp_path).post("/api/key?token=secret123", json={"key": "escape"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_session"


# ---------------------------------------------------------------------------
# Pairing expiry must hold while the server runs, not only at the next restart.
# ---------------------------------------------------------------------------


def test_an_expired_pairing_is_refused_by_a_running_server(tmp_path: Path):
    """The TTL's contract is that a leaked QR heals in 30 days.

    Checking expiry only at startup let a long-running server honor an expired
    token forever; the deadline must be compared per auth check.
    """
    from datetime import UTC, datetime, timedelta

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    cfg.credentials_expire_at = datetime.now(UTC) - timedelta(seconds=1)
    client = TestClient(create_app(cfg))

    resp = client.get("/api/status?token=secret123")
    assert resp.json()["authenticated"] is False


def test_a_pairing_inside_its_ttl_keeps_working(tmp_path: Path):
    from datetime import UTC, datetime, timedelta

    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    cfg.credentials_expire_at = datetime.now(UTC) + timedelta(days=5)
    client = TestClient(create_app(cfg))

    resp = client.get("/api/status?token=secret123")
    assert resp.json()["authenticated"] is True


def test_a_pairing_without_a_deadline_never_expires(tmp_path: Path):
    """An explicit --token or a TTL of 0 has no deadline to enforce."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    assert cfg.credentials_expire_at is None
    client = TestClient(create_app(cfg))

    resp = client.get("/api/status?token=secret123")
    assert resp.json()["authenticated"] is True


def test_status_names_the_agent_it_fronts(tmp_path: Path):
    """The PWA has to know which engine is behind it, and only `init` said so.

    The header, the tab title and the tool vocabulary are all agy's by default,
    so an opencode session was rendered as an agy one -- and a client that
    reconnects over REST (or loads before the socket opens) had no field to
    correct it from.
    """
    for agent in ("agy", "opencode"):
        cfg = RemoteConfig(
            brain_dir=tmp_path,
            auth_token="secret123",
            enable_auth=True,
            agent=agent,
            opencode_port=4096,
        )
        client = TestClient(create_app(cfg))

        body = client.get("/api/status?token=secret123").json()
        assert body["agent"] == agent

    # Still says nothing to an unauthenticated caller.
    assert "agent" not in client.get("/api/status").json()
