"""Unit tests for configuration module."""

from pathlib import Path

from agy_remote.config import RemoteConfig


def test_remote_config_defaults(tmp_path: Path):
    cfg = RemoteConfig(
        brain_dir=tmp_path,
        auth_token="test-secret-token",
        port=8765,
        lan_ip="192.168.1.50",
        tailscale_ip="100.64.0.5",
    )
    assert cfg.port == 8765
    assert cfg.auth_token == "test-secret-token"
    assert cfg.enable_auth is True

    urls = cfg.get_connect_urls()
    assert len(urls) >= 2
    assert any("100.64.0.5:8765" in u[1] for u in urls)
    assert any("192.168.1.50:8765" in u[1] for u in urls)
    assert cfg.get_primary_mobile_url().startswith("http://100.64.0.5:8765/?token=test-secret-token")
