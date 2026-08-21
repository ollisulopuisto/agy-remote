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


# ---------------------------------------------------------------------------
# The QR code must advertise an address the phone can actually reach.
# ---------------------------------------------------------------------------

MACOS_IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.42 netmask 0xffffff00 broadcast 192.168.1.255
bridge100: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.239.1 netmask 0xffffff00 broadcast 192.168.239.255
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 172.16.30.1 --> 172.16.30.1 netmask 0xffffffff
"""

LINUX_IP_ADDR = """\
1: lo    inet 127.0.0.1/8 scope host lo
2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0
3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0
"""


def test_vpn_tunnel_address_is_not_advertised():
    """A VPN on the default route must not hijack the phone-facing address.

    The UDP-connect probe returns whichever interface carries the default
    route, which is the tunnel when any VPN is up - an address the phone
    cannot reach.
    """
    from agy_remote.config import parse_interface_addresses, pick_lan_address

    addrs = parse_interface_addresses(MACOS_IFCONFIG)
    assert ("en0", "192.168.1.42") in addrs
    assert ("utun4", "172.16.30.1") in addrs

    assert pick_lan_address(addrs) == "192.168.1.42"


def test_bridges_and_docker_are_skipped():
    from agy_remote.config import parse_interface_addresses, pick_lan_address

    assert pick_lan_address(parse_interface_addresses(LINUX_IP_ADDR)) == "10.0.0.5"


def test_pick_lan_address_falls_back_when_nothing_matches():
    from agy_remote.config import pick_lan_address

    assert pick_lan_address([("utun0", "172.16.30.1")]) is None
    assert pick_lan_address([]) is None


# ---------------------------------------------------------------------------
# Credentials must survive a restart, or every launch invalidates the phone's
# saved URL and the QR has to be scanned again.
# ---------------------------------------------------------------------------


def _fresh_config(monkeypatch, tmp_path: Path):
    """Build a config the way the CLI does, against a throwaway credential store."""
    from agy_remote import config as config_mod

    monkeypatch.setattr(config_mod, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config_mod, "config_instance", None)
    for var in ("AGY_REMOTE_TOKEN", "AGY_REMOTE_E2EE_KEY"):
        monkeypatch.delenv(var, raising=False)
    return config_mod.get_config()


def test_token_and_key_are_stable_across_restarts(monkeypatch, tmp_path: Path):
    first = _fresh_config(monkeypatch, tmp_path)
    second = _fresh_config(monkeypatch, tmp_path)

    assert first.auth_token == second.auth_token
    assert first.e2ee_key == second.e2ee_key
    assert first.auth_token  # not empty


def test_credential_store_is_owner_only(monkeypatch, tmp_path: Path):
    """The token is equivalent to shell access; nobody else may read it."""
    import stat

    _fresh_config(monkeypatch, tmp_path)
    store = tmp_path / "credentials.json"
    assert store.exists()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_explicit_env_token_overrides_the_store(monkeypatch, tmp_path: Path):
    from agy_remote import config as config_mod

    _fresh_config(monkeypatch, tmp_path)

    monkeypatch.setattr(config_mod, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config_mod, "config_instance", None)
    monkeypatch.setenv("AGY_REMOTE_TOKEN", "explicit-token")
    assert config_mod.get_config().auth_token == "explicit-token"


def test_rotate_credentials_issues_new_ones(monkeypatch, tmp_path: Path):
    from agy_remote import config as config_mod

    before = _fresh_config(monkeypatch, tmp_path)
    config_mod.rotate_credentials()
    after = _fresh_config(monkeypatch, tmp_path)

    assert after.auth_token != before.auth_token
    assert after.e2ee_key != before.e2ee_key
