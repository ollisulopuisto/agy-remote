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
