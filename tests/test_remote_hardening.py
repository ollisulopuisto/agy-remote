"""Tests for remote loop hardening, context and usage HUD, and notification preferences."""

from pathlib import Path

from starlette.testclient import TestClient

from agy_remote.config import RemoteConfig
from agy_remote.crypto import decode_key, encrypt_payload
from agy_remote.push import PushManager, classify_notification
from agy_remote.screen import parse_context_and_usage
from agy_remote.server import create_app
from agy_remote.session_manager import SessionManager


def test_parse_context_and_usage():
    lines = [
        "Some output line 1",
        "Some output line 2",
        "Tokens: 45.2k / 200k (22.6%) · Cost: $0.12 · 18 steps",
        "? for shortcuts                     plan · Gemini 2.5 Flash · medium",
    ]
    usage = parse_context_and_usage(lines)
    assert usage["model"] == "Gemini 2.5 Flash"
    assert usage["mode"] == "plan"
    assert usage["tokens_used"] == 45200
    assert usage["tokens_limit"] == 200000
    assert usage["context_percent"] == 22.6
    assert usage["cost"] == "$0.12"
    assert usage["step_count"] == 18


def test_classify_notification():
    assert classify_notification("Tool Approval Required", {"id": "123"}) == "approvals"
    assert classify_notification("Worker Finished", {"status": "completed"}) == "completed"
    assert classify_notification("Task Failed", {"status": "failed"}) == "failed"
    assert classify_notification("Agent Needs Attention", {"status": "needs_attention"}) == "attention"
    assert classify_notification("Autonomous Loop Detected", {"loop": True}) == "loops"
    assert classify_notification("Random Announcement", {}) == "completed"


def test_push_preferences_filtering(tmp_path: Path, monkeypatch):
    key_file = tmp_path / "vapid.json"
    mgr = PushManager(key_file=key_file)

    sub1 = {
        "endpoint": "https://push.example.com/sub/1",
        "keys": {"p256dh": "key1", "auth": "auth1"},
    }
    sub2 = {
        "endpoint": "https://push.example.com/sub/2",
        "keys": {"p256dh": "key2", "auth": "auth2"},
    }
    mgr.add_subscription(sub1)
    mgr.add_subscription(sub2)

    # Disable 'approvals' for sub1
    mgr.update_preferences(sub1["endpoint"], {"approvals": False, "completed": True})
    assert mgr.get_preferences(sub1["endpoint"])["approvals"] is False
    assert mgr.get_preferences(sub2["endpoint"])["approvals"] is True

    dispatched = []

    def mock_send(subscription_info, data, vapid_private_key, vapid_claims, timeout=5):
        dispatched.append(subscription_info["endpoint"])

    monkeypatch.setattr("agy_remote.push.webpush", mock_send)

    # Approval notification: only sub2 should get it
    mgr.send_notification("Tool Approval Required", "Allow bash?", data={"id": "app_1"})
    assert dispatched == [sub2["endpoint"]]

    dispatched.clear()
    # Completed notification: both should get it
    mgr.send_notification("Task Finished", "All done!", data={"status": "completed"})
    assert set(dispatched) == {sub1["endpoint"], sub2["endpoint"]}


def test_usage_rest_endpoints(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    app = create_app(cfg)
    client = TestClient(app)

    headers = {"X-Auth-Token": "secret123"}
    res = client.get("/api/usage", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "conversation_id" in data
    assert "context_percent" in data

    res2 = client.get("/api/conversations/default/usage", headers=headers)
    assert res2.status_code == 200
    assert res2.json()["conversation_id"] == "default"


def test_push_preferences_rest_endpoints(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    app = create_app(cfg)
    client = TestClient(app)
    headers = {"X-Auth-Token": "secret123"}

    endpoint = "https://push.example.com/sub/user_abc"
    post_res = client.post(
        "/api/push/preferences",
        headers=headers,
        json={
            "endpoint": endpoint,
            "preferences": {"approvals": False, "loops": True},
        },
    )
    assert post_res.status_code == 200
    assert post_res.json()["preferences"]["approvals"] is False

    get_res = client.get(
        f"/api/push/preferences?endpoint={endpoint}",
        headers=headers,
    )
    assert get_res.status_code == 200
    assert get_res.json()["preferences"]["approvals"] is False
    assert get_res.json()["preferences"]["loops"] is True


def test_session_manager_outbox_and_replay(tmp_path: Path):
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret123", enable_auth=True)
    app = create_app(cfg)
    mgr: SessionManager = app.state.session_manager
    key = decode_key(cfg.e2ee_key)
    client = TestClient(app)

    # 1. Initial connection records current seq
    with client.websocket_connect(f"/ws?token={cfg.auth_token}") as ws:
        init_frame = ws.receive_json()
        if init_frame.get("encrypted"):
            from agy_remote.crypto import decrypt_payload

            init_frame = decrypt_payload(init_frame, key)
        assert "active_conversation_id" in init_frame["data"]
        last_seq = init_frame["data"].get("last_seq", 0)

    # 2. Client is disconnected (offline). Broadcast 3 events while offline.
    import asyncio

    asyncio.run(mgr.broadcast({"event": "step_added", "data": {"id": "1"}}))
    asyncio.run(mgr.broadcast({"event": "step_added", "data": {"id": "2"}}))
    asyncio.run(mgr.broadcast({"event": "step_added", "data": {"id": "3"}}))

    # 3. Client reconnects and requests replay of missed events since last_seq
    with client.websocket_connect(f"/ws?token={cfg.auth_token}") as ws2:
        # Drain connection handshake frames (init, peers)
        ws2.receive_json()
        ws2.receive_json()

        ws2.send_json(encrypt_payload({"action": "replay", "data": {"since_seq": last_seq}}, key))
        replayed = []
        while len([f for f in replayed if f["event"] == "step_added"]) < 3:
            f = ws2.receive_json()
            if f.get("encrypted"):
                from agy_remote.crypto import decrypt_payload

                f = decrypt_payload(f, key)
            replayed.append(f)

        steps = [f for f in replayed if f["event"] == "step_added"]
        assert len(steps) == 3
        assert steps[0]["data"]["id"] == "1"
        assert steps[1]["data"]["id"] == "2"
        assert steps[2]["data"]["id"] == "3"
        assert steps[0]["seq"] > last_seq
        assert steps[1]["seq"] > steps[0]["seq"]
        assert steps[2]["seq"] > steps[1]["seq"]
