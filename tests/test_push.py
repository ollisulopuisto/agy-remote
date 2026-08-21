"""Unit tests for Web Push notifications module."""

from pathlib import Path

from agy_remote.push import PushManager


def test_push_manager_vapid_generation(tmp_path: Path):
    key_file = tmp_path / "vapid.json"
    mgr = PushManager(key_file=key_file)

    assert key_file.exists()
    assert mgr.public_key
    assert mgr.private_key

    # Subscribe test
    sub = {
        "endpoint": "https://push.example.com/sub/123",
        "keys": {"p256dh": "key123", "auth": "auth123"},
    }
    mgr.add_subscription(sub)
    assert len(mgr.subscriptions) == 1

    # Reload from disk
    mgr2 = PushManager(key_file=key_file)
    assert mgr2.public_key == mgr.public_key
    assert len(mgr2.subscriptions) == 1
