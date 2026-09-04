"""Stress and concurrency tests for agy-remote hardening, HUD, and push infrastructure."""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path

from agy_remote.config import RemoteConfig
from agy_remote.push import PushManager
from agy_remote.screen import parse_context_and_usage
from agy_remote.session_manager import SessionManager


def test_outbox_ring_buffer_overflow_and_boundaries(tmp_path: Path):
    """Stress test outbox with 2,500 messages, verifying buffer capping and boundary conditions."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret", enable_auth=True, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    # Broadcast 2,500 messages
    async def run_broadcasts():
        for i in range(1, 2501):
            await mgr.broadcast({"event": "step_added", "data": {"num": i}})

    asyncio.run(run_broadcasts())

    # Verify buffer cap
    assert len(mgr._outbox) == 500
    assert mgr._event_seq == 2500
    earliest_seq, _ = mgr._outbox[0]
    latest_seq, _ = mgr._outbox[-1]
    assert earliest_seq == 2001
    assert latest_seq == 2500

    # Mock websocket to test replay_since
    class ReplayCollector:
        def __init__(self):
            self.events = []

        async def send_json(self, data):
            self.events.append(data)

    collector = ReplayCollector()

    # Case A: since_seq far behind window (< earliest - 1) -> must fail (return False)
    assert asyncio.run(mgr.replay_since(collector, 100)) is False
    assert len(collector.events) == 0

    # Case B: since_seq right before earliest (earliest - 1 = 2000) -> must succeed and replay all 500
    collector.events.clear()
    assert asyncio.run(mgr.replay_since(collector, 2000)) is True
    assert len(collector.events) == 500
    assert collector.events[0]["seq"] == 2001
    assert collector.events[-1]["seq"] == 2500

    # Case C: since_seq inside the window (e.g. 2480) -> must replay 20 events
    collector.events.clear()
    assert asyncio.run(mgr.replay_since(collector, 2480)) is True
    assert len(collector.events) == 20
    assert collector.events[0]["seq"] == 2481
    assert collector.events[-1]["seq"] == 2500

    # Case D: since_seq at current tip (2500) -> 0 events, returns True
    collector.events.clear()
    assert asyncio.run(mgr.replay_since(collector, 2500)) is True
    assert len(collector.events) == 0

    # Case E: since_seq in the future (3000) -> 0 events, returns True
    collector.events.clear()
    assert asyncio.run(mgr.replay_since(collector, 3000)) is True
    assert len(collector.events) == 0

    # Case F: negative since_seq -> False
    collector.events.clear()
    assert asyncio.run(mgr.replay_since(collector, -10)) is False
    assert len(collector.events) == 0


def test_concurrent_broadcast_and_replay_storm(tmp_path: Path):
    """Simulate concurrent background broadcasting while multiple clients replay."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret", enable_auth=True, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    class FastCollector:
        def __init__(self):
            self.count = 0

        async def send_json(self, _):
            self.count += 1

    async def worker_broadcaster():
        for i in range(1000):
            await mgr.broadcast({"event": "step_updated", "data": {"i": i}})
            if i % 100 == 0:
                await asyncio.sleep(0.001)

    async def worker_replayer():
        for _ in range(50):
            collector = FastCollector()
            # Random replay point within reasonable range
            curr = mgr._event_seq
            since = max(0, curr - random.randint(10, 200))
            await mgr.replay_since(collector, since)
            await asyncio.sleep(0.002)

    async def run_all():
        await asyncio.gather(
            worker_broadcaster(),
            worker_replayer(),
            worker_replayer(),
            worker_replayer(),
            worker_replayer(),
        )

    t0 = time.perf_counter()
    asyncio.run(run_all())
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"Concurrent broadcast and replay too slow: {elapsed:.2f}s"
    assert mgr._event_seq == 1000
    assert len(mgr._outbox) == 500


def test_screen_parser_stress_and_fuzz():
    """Fuzz and stress test parse_context_and_usage with 2,000 chaotic lines and inputs."""
    fuzz_templates = [
        "? for shortcuts                     {mode} · {model} · medium",
        "Tokens: {tokens} / {limit} ({pct}%) · Cost: {cost} · {steps} steps",
        "Context window: {pct}% utilized, total {tokens} tokens",
        "\x1b[31;1mError:\x1b[0m random ANSI noise \x1b[2J\x1b[H",
        "Random line with no numbers or keywords",
        "{model} cost=${cost} used={tokens}/{limit}",
        "",
        "   \t  \r\n   ",
        "Tokens: 999999999999999k / 999999999999999m (150%) · Cost: $999999.99 · 50000 steps",
        "Invalid token ratio 12.3.4k / 56.7.8m",
        "Claude 3.7 Sonnet Thinking · plan · $0.00 · 0 steps",
        "Gemini 2.5 Flash · accept-edits · $12.45 · 42 steps",
        "o3-mini-high · default · 85.4% context · 128 steps",
        "GPT-4o · 12k / 128k (9.4%)",
    ]

    models = ["Gemini 2.5 Flash", "Claude 3.7 Sonnet", "GPT-4o", "o1-preview", "Codex-1", "UnknownBot"]
    modes = ["plan", "accept-edits", "default", "custom"]

    lines = []
    for _ in range(2000):
        tmpl = random.choice(fuzz_templates)
        line = tmpl.format(
            mode=random.choice(modes),
            model=random.choice(models),
            tokens=f"{random.randint(1, 500)}k",
            limit=f"{random.randint(50, 1000)}k",
            pct=f"{random.uniform(0.1, 99.9):.1f}",
            cost=f"${random.uniform(0.01, 10.0):.2f}",
            steps=f"{random.randint(1, 200)}",
        )
        lines.append(line)

    t0 = time.perf_counter()
    # Run 1,000 multi-line parses across chunks
    for i in range(0, len(lines) - 5, 2):
        chunk = lines[i : i + 5]
        res = parse_context_and_usage(chunk)
        assert isinstance(res, dict)

    duration = time.perf_counter() - t0
    # Must process 1,000 chunks well under 250ms (verifying no catastrophic regex backtracking)
    assert duration < 0.25, f"Parser performance degraded: took {duration:.3f}s for 1,000 parses"


def test_presence_focus_thrash_and_cleanup(tmp_path: Path):
    """Simulate rapid concurrent focus state changes and ensure cleanup on disconnect."""
    cfg = RemoteConfig(brain_dir=tmp_path, auth_token="secret", enable_auth=True, e2ee_enabled=False)
    mgr = SessionManager(cfg)

    class DummyWS:
        def __init__(self, name):
            self.name = name

    clients = [DummyWS(f"client_{i}") for i in range(50)]

    # Register all clients
    for c in clients:
        mgr._connected_clients.add(c)

    # Rapidly toggle focus across different conversations
    convs = ["conv_A", "conv_B", "conv_C", "default"]
    for i in range(1000):
        c = random.choice(clients)
        focused = (i % 2) == 0
        conv = random.choice(convs)
        mgr.set_client_focus(c, focused, conv)

    # Disconnect all clients
    for c in clients:
        mgr.unregister_client(c)

    # Verify zero leaks
    assert len(mgr._connected_clients) == 0
    assert len(mgr._client_focus) == 0
    assert mgr.is_client_focused() is False
    assert mgr.is_client_focused("conv_A") is False


def test_push_preferences_high_volume_concurrency(tmp_path: Path, monkeypatch):
    """Stress test PushManager with 300 subscribers and diverse preference sets."""
    key_file = tmp_path / "vapid.json"
    mgr = PushManager(key_file=key_file)

    # Add 300 subscriptions
    for i in range(300):
        endpoint = f"https://push.example.com/sub/{i}"
        mgr.add_subscription(
            {
                "endpoint": endpoint,
                "keys": {"p256dh": f"p256_{i}", "auth": f"auth_{i}"},
            }
        )
        # Set distinct preferences: only even gets approvals, only % 3 gets loops, etc.
        prefs = {
            "approvals": (i % 2 == 0),
            "completed": True,
            "failed": (i % 5 != 0),
            "attention": (i % 4 == 0),
            "loops": (i % 3 == 0),
        }
        mgr.update_preferences(endpoint, prefs)

    assert len(mgr.subscriptions) == 300

    dispatched = []

    def mock_send(subscription_info, data, vapid_private_key, vapid_claims, timeout=5):
        dispatched.append((subscription_info["endpoint"], data))

    monkeypatch.setattr("agy_remote.push.webpush", mock_send)

    # Test 1: approvals notification
    dispatched.clear()
    mgr.send_notification("Tool Approval Required", "Bash command?", data={"id": "app_1"})
    # Only even i (150 subscribers) should have received it
    assert len(dispatched) == 150

    # Test 2: loops notification
    dispatched.clear()
    mgr.send_notification("Agent Loop Alert", "Loop detected!", data={"loop": True})
    # Only i % 3 == 0 (100 subscribers) should have received it
    assert len(dispatched) == 100

    # Test 3: completed notification
    dispatched.clear()
    mgr.send_notification("Task Finished", "Done", data={"status": "completed"})
    # All 300 have completed=True
    assert len(dispatched) == 300
