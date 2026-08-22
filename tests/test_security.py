"""Regression tests for the v26.08.22.1 security hardening pass.

Each test here reproduces a specific flaw found in the audit of v26.08.21.5.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agy_remote.config import RemoteConfig
from agy_remote.crypto import (
    ReplayError,
    ReplayGuard,
    StaleEnvelopeError,
    decode_key,
    decrypt_payload,
    encrypt_payload,
    generate_e2ee_key,
)
from agy_remote.server import create_app
from agy_remote.session_manager import SessionManager


class FakeWebSocket:
    """Minimal stand-in capturing what the server would put on the wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _cfg(tmp_path: Path, **kw) -> RemoteConfig:
    base = dict(
        brain_dir=tmp_path,
        auth_token="secret123",
        enable_auth=True,
        e2ee_enabled=True,
        tailscale_ip=None,
        lan_ip="127.0.0.1",
    )
    base.update(kw)
    return RemoteConfig(**base)


# ---------------------------------------------------------------------------
# Finding #2: "E2EE" left server->client traffic in cleartext
# ---------------------------------------------------------------------------


async def test_broadcast_is_encrypted_on_the_wire(tmp_path: Path):
    """Transcript content must never hit the socket as plaintext."""
    mgr = SessionManager(_cfg(tmp_path))
    ws = FakeWebSocket()
    mgr._connected_clients.add(ws)

    secret = "SENSITIVE_TRANSCRIPT_CONTENT"
    await mgr.broadcast({"event": "step_added", "data": {"step": {"content": secret}}})

    assert len(ws.sent) == 1
    envelope = ws.sent[0]
    assert envelope.get("encrypted") is True, "broadcast() sent cleartext"
    assert secret not in json.dumps(envelope)

    key = decode_key(mgr.config.e2ee_key)
    assert decrypt_payload(envelope, key)["data"]["step"]["content"] == secret


async def test_init_snapshot_is_encrypted(tmp_path: Path):
    """The initial state dump is the largest payload; it must be encrypted too."""
    conv = tmp_path / "conv-a" / ".system_generated" / "logs"
    conv.mkdir(parents=True)
    (conv / "transcript.jsonl").write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "SECRET_PROMPT"}) + "\n"
    )

    mgr = SessionManager(_cfg(tmp_path))
    await mgr.switch_conversation("conv-a")

    ws = FakeWebSocket()
    await mgr.register_client(ws)

    assert ws.sent, "no init payload sent"
    assert ws.sent[0].get("encrypted") is True, "register_client() sent cleartext"
    assert "SECRET_PROMPT" not in json.dumps(ws.sent[0])


async def test_plaintext_when_e2ee_disabled(tmp_path: Path):
    mgr = SessionManager(_cfg(tmp_path, e2ee_enabled=False))
    ws = FakeWebSocket()
    mgr._connected_clients.add(ws)
    await mgr.broadcast({"event": "pong", "data": {}})
    assert ws.sent[0] == {"event": "pong", "data": {}}


# ---------------------------------------------------------------------------
# Finding: no replay protection on authenticated envelopes
# ---------------------------------------------------------------------------


def test_replayed_envelope_is_rejected():
    """A captured `send_prompt` envelope must not be re-injectable."""
    key = decode_key(generate_e2ee_key())
    guard = ReplayGuard()
    envelope = encrypt_payload({"action": "send_prompt", "data": {"prompt": "rm -rf /"}}, key)

    assert decrypt_payload(envelope, key, guard=guard)["action"] == "send_prompt"
    with pytest.raises(ReplayError):
        decrypt_payload(envelope, key, guard=guard)


def test_stale_envelope_is_rejected():
    key = decode_key(generate_e2ee_key())
    envelope = encrypt_payload({"action": "ping"}, key)
    envelope["ts"] -= 10_000
    with pytest.raises(StaleEnvelopeError):
        decrypt_payload(envelope, key, guard=ReplayGuard())


def test_timestamp_is_authenticated():
    """ts is bound as AAD, so sliding it forward must break the tag."""
    key = decode_key(generate_e2ee_key())
    envelope = encrypt_payload({"action": "ping"}, key)
    envelope["ts"] += 1
    with pytest.raises(InvalidTag):
        decrypt_payload(envelope, key)


def test_decode_key_rejects_wrong_length():
    with pytest.raises(ValueError):
        decode_key("c2hvcnQ=")  # b"short"


# ---------------------------------------------------------------------------
# Finding #3: non-constant-time token comparison on WS + hook endpoints
# ---------------------------------------------------------------------------


def test_websocket_auth_is_constant_time(tmp_path: Path, monkeypatch):
    import agy_remote.server as server_mod

    calls: list[tuple] = []
    real = server_mod.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(server_mod.secrets, "compare_digest", spy)

    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws?token=wrong"):
        pass

    assert calls, "websocket auth did not use secrets.compare_digest"


def test_hook_auth_is_constant_time(tmp_path: Path, monkeypatch):
    import agy_remote.server as server_mod

    calls: list[tuple] = []
    real = server_mod.secrets.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(server_mod.secrets, "compare_digest", spy)

    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    resp = client.post("/api/hook/pre-tool", json={}, headers={"X-Auth-Token": "wrong"})
    assert resp.status_code == 401
    assert calls, "hook auth did not use secrets.compare_digest"


# ---------------------------------------------------------------------------
# Finding #5: wildcard CORS on a LAN-exposed service
# ---------------------------------------------------------------------------


def test_no_wildcard_cors(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    resp = client.get(
        "/api/status?token=secret123",
        headers={"Origin": "https://evil.example"},
    )
    assert resp.headers.get("access-control-allow-origin") != "*"


# ---------------------------------------------------------------------------
# Finding #6: SVG uploads carry active script
# ---------------------------------------------------------------------------


def test_svg_upload_rejected(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    resp = client.post(
        "/api/upload?token=secret123",
        files={"file": ("payload.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
    )
    assert resp.status_code == 400


def test_upload_content_must_match_extension(tmp_path: Path):
    """A .png that is really a shell script must be rejected."""
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    resp = client.post(
        "/api/upload?token=secret123",
        files={"file": ("evil.png", b"#!/bin/sh\nrm -rf ~\n", "image/png")},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Finding #8: pre-auth information disclosure
# ---------------------------------------------------------------------------


def test_status_leaks_nothing_before_auth(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    body = client.get("/api/status").json()
    assert body == {"auth_required": True, "authenticated": False}


# ---------------------------------------------------------------------------
# Finding #1: --no-auth on a LAN-reachable bind is unauthenticated RCE
# ---------------------------------------------------------------------------


def test_no_auth_forbidden_on_non_loopback_bind(tmp_path: Path):
    from agy_remote.config import InsecureConfigError, validate_bind_security

    with pytest.raises(InsecureConfigError):
        validate_bind_security(_cfg(tmp_path, enable_auth=False, host="0.0.0.0"))

    # Loopback + no auth is fine.
    validate_bind_security(_cfg(tmp_path, enable_auth=False, host="127.0.0.1"))
    # Public bind with auth on is fine.
    validate_bind_security(_cfg(tmp_path, enable_auth=True, host="0.0.0.0"))


def test_host_header_checked_when_auth_disabled(tmp_path: Path):
    """No-auth mode is loopback-only, so reject rebound DNS names."""
    app = create_app(_cfg(tmp_path, enable_auth=False, host="127.0.0.1"))
    client = TestClient(app)
    resp = client.get("/api/status", headers={"Host": "attacker.example"})
    assert resp.status_code == 421


# ---------------------------------------------------------------------------
# Finding #7: VAPID private key stored world-readable
# ---------------------------------------------------------------------------


def test_vapid_key_file_is_owner_only(tmp_path: Path):
    from agy_remote.push import PushManager

    key_file = tmp_path / "vapid.json"
    PushManager(key_file=key_file)
    assert key_file.exists()
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600, f"vapid.json mode is {oct(mode)}"


# ---------------------------------------------------------------------------
# Finding: E2EE could be downgraded by sending an unsealed frame
# ---------------------------------------------------------------------------


def _next_frame(ws, key):
    """The next frame that says something, skipping peer-count announcements.

    Every connect and disconnect broadcasts `peers`, which would otherwise
    shift the frame order these tests read positionally.
    """
    while True:
        frame = decrypt_payload(ws.receive_json(), key)
        if frame.get("event") != "peers":
            return frame


def test_unencrypted_frame_rejected_when_e2ee_enabled(tmp_path: Path):
    """Holding the token must not be enough to drive the agent when E2EE is on.

    Ordering makes this discriminating: if the plaintext `send_prompt` were
    accepted, its `prompt_sent` broadcast would arrive *before* the pong.
    """
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    key = decode_key(cfg.e2ee_key)
    client = TestClient(app)

    with client.websocket_connect(f"/ws?token={cfg.auth_token}") as ws:
        ws.receive_json()  # sealed init snapshot

        # Attacker knows the token but not the key: try to drive it in plaintext.
        ws.send_json({"action": "send_prompt", "data": {"prompt": "INJECTED"}})
        # Then a legitimate sealed ping, proving the socket is still alive.
        ws.send_json(encrypt_payload({"action": "ping"}, key))

        # The rejection notice comes first; the pong proves the socket lived.
        seen = [_next_frame(ws, key) for _ in range(2)]

    assert all(f.get("event") != "prompt_sent" for f in seen), f"plaintext frame was acted on: {seen}"
    assert seen[-1] == {"event": "pong"}, f"socket did not survive the rejection: {seen}"


def test_full_websocket_roundtrip_is_sealed(tmp_path: Path):
    """Every frame in both directions carries a v1 envelope."""
    conv = tmp_path / "conv-b" / ".system_generated" / "logs"
    conv.mkdir(parents=True)
    (conv / "transcript.jsonl").write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "TOP_SECRET"}) + "\n"
    )

    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    key = decode_key(cfg.e2ee_key)

    # TestClient as a context manager runs lifespan, loading the conversation.
    with TestClient(app) as client, client.websocket_connect(f"/ws?token={cfg.auth_token}") as ws:
        init = ws.receive_json()
        assert init["encrypted"] is True
        assert init["v"] == 1
        assert "TOP_SECRET" not in json.dumps(init)

        opened = decrypt_payload(init, key)
        assert opened["event"] == "init"
        assert opened["data"]["steps"][0]["content"] == "TOP_SECRET"


# ---------------------------------------------------------------------------
# Finding: the PreToolUse hook minted its own token, so it never authenticated
# ---------------------------------------------------------------------------


def test_runtime_state_is_shared_with_hook_process(tmp_path: Path, monkeypatch):
    """The hook runs in a separate process and must reuse the server's token."""
    from agy_remote.config import read_runtime_state, write_runtime_state

    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr("agy_remote.config.RUNTIME_STATE_FILE", state_file)

    cfg = _cfg(tmp_path, auth_token="server-side-token", port=9999)
    write_runtime_state(cfg)

    state = read_runtime_state()
    assert state is not None
    assert state["auth_token"] == "server-side-token"
    assert state["port"] == 9999
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_hook_posts_with_the_servers_token(tmp_path: Path, monkeypatch):
    """Regression: the hook used to generate a fresh token and always 401."""
    import agy_remote.config as config_mod
    import agy_remote.hooks as hooks_mod

    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)
    config_mod.write_runtime_state(_cfg(tmp_path, auth_token="server-side-token", port=9999))

    captured = {}

    class FakeResponse:
        def read(self):
            return b'{"decision": "allow"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["token"] = req.headers.get("X-auth-token")
        return FakeResponse()

    monkeypatch.setattr(hooks_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO('{"toolCall": {"name": "run_command"}}'))

    hooks_mod.run_pre_tool_hook()

    assert captured["token"] == "server-side-token"
    assert ":9999/" in captured["url"]


def test_vapid_legacy_permissions_are_repaired(tmp_path: Path):
    """A key file left world-readable by an older version must be tightened."""
    from agy_remote.push import PushManager

    key_file = tmp_path / "vapid.json"
    PushManager(key_file=key_file)
    key_file.chmod(0o644)  # simulate a file written by <= v26.08.21.5

    PushManager(key_file=key_file)
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Finding: the PWA loaded scripts from a CDN and rendered model output as HTML
# ---------------------------------------------------------------------------


def test_pwa_loads_no_third_party_code():
    """A CDN compromise would hand an attacker the E2EE key from localStorage."""
    from agy_remote.server import STATIC_DIR

    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net" not in index
    assert "//unpkg.com" not in index
    for scheme in ('src="http', 'href="http'):
        assert scheme not in index, f"index.html still references an external origin ({scheme})"


def test_markdown_renderer_is_escape_first():
    """Model output reaches innerHTML, so the renderer must never emit raw HTML."""
    from agy_remote.server import STATIC_DIR

    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.marked" not in app_js, "still delegating to the unsanitized CDN parser"
    assert "renderMarkdown" in app_js


def test_no_inline_event_handlers_in_generated_html():
    """Inline handlers would force script-src 'unsafe-inline', defeating the CSP."""
    from agy_remote.server import STATIC_DIR

    app_js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for handler in ("onclick=", "onerror=", "onload="):
        assert handler not in app_js, f"app.js still builds markup with {handler}"


def test_security_headers_present(tmp_path: Path):
    app = create_app(_cfg(tmp_path))
    client = TestClient(app)
    resp = client.get("/api/status?token=secret123")

    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "unsafe-inline" not in csp.split("style-src")[0], "script-src must not allow inline"
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("referrer-policy") == "no-referrer"


def test_importing_server_does_not_build_an_app(monkeypatch):
    """Import must stay side-effect free.

    Building the app at import time meant an unsafe config surfaced as a
    traceback from `import agy_remote.server` instead of the CLI's message,
    and every import spun up a PushManager and read the brain directory.
    """
    import importlib
    import sys

    monkeypatch.setenv("AGY_REMOTE_NO_AUTH", "1")
    monkeypatch.setenv("AGY_REMOTE_HOST", "0.0.0.0")
    monkeypatch.setattr("agy_remote.config.config_instance", None)
    sys.modules.pop("agy_remote.server", None)

    module = importlib.import_module("agy_remote.server")  # must not raise
    assert hasattr(module, "create_app")


def test_qr_command_shows_the_running_servers_credentials(tmp_path: Path, monkeypatch):
    """Regression: `agy-remote qr` minted a fresh token, so the QR never worked.

    Same root cause as the PreToolUse hook: a separate process calling
    get_config() gets new random credentials rather than the live server's.
    """
    import agy_remote.config as config_mod

    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)

    running = _cfg(tmp_path, auth_token="live-token", port=9191)
    config_mod.write_runtime_state(running)

    # A fresh process: different random token until it adopts the published one.
    fresh = RemoteConfig(brain_dir=tmp_path)
    assert fresh.auth_token != "live-token"

    adopted = config_mod.adopt_runtime_state(fresh)
    assert adopted.auth_token == "live-token"
    assert adopted.port == 9191
    assert adopted.e2ee_key == running.e2ee_key, "QR would carry the wrong E2EE key"


def test_adopt_runtime_state_is_a_noop_without_a_running_server(tmp_path: Path, monkeypatch):
    import agy_remote.config as config_mod

    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", tmp_path / "absent.json")
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="local")
    assert config_mod.adopt_runtime_state(cfg).auth_token == "local"


def test_shutdown_does_not_clear_another_servers_state(tmp_path: Path, monkeypatch):
    """Regression: restarting the server left no published credentials.

    A stopping server ran clear_runtime_state() unconditionally. When restarted
    quickly, the outgoing process deleted the *incoming* one's file, so `qr`
    and the PreToolUse hook found nothing.
    """
    import os

    import agy_remote.config as config_mod

    state_file = tmp_path / "runtime.json"
    monkeypatch.setattr(config_mod, "RUNTIME_STATE_FILE", state_file)

    # The new server publishes its credentials under its own pid.
    config_mod.write_runtime_state(_cfg(tmp_path, auth_token="new-server"))
    assert config_mod.read_runtime_state()["auth_token"] == "new-server"

    # The old server, shutting down late, must not delete them.
    config_mod.clear_runtime_state(owner_pid=os.getpid() + 12345)
    assert config_mod.read_runtime_state() is not None, "outgoing server clobbered the new state"

    # The owning process still cleans up after itself.
    config_mod.clear_runtime_state(owner_pid=os.getpid())
    assert config_mod.read_runtime_state() is None


# ---------------------------------------------------------------------------
# HTTPS via Tailscale: Web Crypto only exists in a secure context, so plain
# http:// on a LAN address cannot do E2EE at all.
# ---------------------------------------------------------------------------

TAILSCALE_STATUS_JSON = """
{"Self": {"DNSName": "mac-studio.tail1a2b3.ts.net.", "TailscaleIPs": ["100.101.102.103"]}}
"""


def test_tailscale_dns_name_is_parsed():
    from agy_remote.config import parse_tailscale_dns_name

    assert parse_tailscale_dns_name(TAILSCALE_STATUS_JSON) == "mac-studio.tail1a2b3.ts.net"
    assert parse_tailscale_dns_name("not json") is None
    assert parse_tailscale_dns_name("{}") is None


def test_connect_urls_use_https_and_dns_name_when_tls_is_on(tmp_path: Path):
    """Web Crypto needs a secure context, so the pairing URL must be https."""
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("y")

    cfg = _cfg(
        tmp_path,
        tailscale_ip="100.101.102.103",
        tailscale_dns_name="mac-studio.tail1a2b3.ts.net",
        tls_cert=cert,
        tls_key=key,
    )
    label, url = cfg.get_connect_urls()[0]
    assert url.startswith("https://mac-studio.tail1a2b3.ts.net:"), url
    assert "Tailscale" in label
    assert cfg.tls_enabled is True


def test_connect_urls_stay_http_without_tls(tmp_path: Path):
    cfg = _cfg(tmp_path, lan_ip="192.168.1.42")
    assert cfg.tls_enabled is False
    assert cfg.get_connect_urls()[0][1].startswith("http://192.168.1.42:")


# ---------------------------------------------------------------------------
# Finding: a dropped client frame was dropped in silence
# ---------------------------------------------------------------------------


def test_a_rejected_frame_is_reported_back_to_the_client(tmp_path: Path):
    """A prompt the server threw away must not look like one it accepted.

    Every rejection path -- unsealed frame, bad tag, replayed nonce, a clock
    outside the freshness window -- logged a warning on the desktop and
    `continue`d. The phone had sent into a socket that was open, got nothing
    back, and cleared the input: the prompt was simply gone, with the last
    transcript it had received still on screen.

    Ordering makes this fail fast rather than block: a sealed ping follows each
    bad frame, so the rejection has to arrive before that pong or not at all.
    """
    cfg = _cfg(tmp_path)
    app = create_app(cfg)
    key = decode_key(cfg.e2ee_key)
    client = TestClient(app)

    with client.websocket_connect(f"/ws?token={cfg.auth_token}") as ws:
        ws.receive_json()  # sealed init snapshot

        ws.send_json({"action": "send_prompt", "data": {"prompt": "INJECTED"}})
        ws.send_json(encrypt_payload({"action": "ping"}, key))
        unsealed = _next_frame(ws, key)
        assert _next_frame(ws, key)["event"] == "pong"

        # A sealed frame whose nonce the server has already accepted.
        replay = encrypt_payload({"action": "ping"}, key)
        ws.send_json(replay)
        assert _next_frame(ws, key)["event"] == "pong"
        ws.send_json(replay)
        ws.send_json(encrypt_payload({"action": "ping"}, key))
        replayed = _next_frame(ws, key)

    assert unsealed["event"] == "frame_rejected", f"plaintext frame dropped in silence: {unsealed}"
    assert unsealed["data"]["reason"]
    assert replayed["event"] == "frame_rejected", f"replayed frame dropped in silence: {replayed}"
    assert replayed["data"]["reason"]
