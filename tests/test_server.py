"""Unit tests for FastAPI REST and WebSocket endpoints."""

import json
from pathlib import Path

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
