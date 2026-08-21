"""Unit tests for configuration module."""

import json
from datetime import UTC
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


def test_credentials_expire_after_their_ttl(monkeypatch, tmp_path: Path):
    """A pairing URL is a durable secret; age must eventually invalidate it.

    The store made the token long-lived, which quietly turned every phone
    bookmark into a credential that never expires. A leaked QR screenshot or a
    lost phone should not stay a way in forever.
    """
    from datetime import datetime, timedelta

    before = _fresh_config(monkeypatch, tmp_path)

    # Age the stored credentials past the TTL.
    store = tmp_path / "credentials.json"
    data = json.loads(store.read_text())
    data["created_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    store.write_text(json.dumps(data))

    after = _fresh_config(monkeypatch, tmp_path)
    assert after.auth_token != before.auth_token
    assert after.e2ee_key != before.e2ee_key


def test_credentials_within_ttl_are_kept(monkeypatch, tmp_path: Path):
    from datetime import datetime, timedelta

    before = _fresh_config(monkeypatch, tmp_path)
    store = tmp_path / "credentials.json"
    data = json.loads(store.read_text())
    data["created_at"] = (datetime.now(UTC) - timedelta(days=29)).isoformat()
    store.write_text(json.dumps(data))

    assert _fresh_config(monkeypatch, tmp_path).auth_token == before.auth_token


def test_ttl_zero_means_credentials_never_expire(monkeypatch, tmp_path: Path):
    from datetime import datetime, timedelta

    before = _fresh_config(monkeypatch, tmp_path)
    store = tmp_path / "credentials.json"
    data = json.loads(store.read_text())
    data["created_at"] = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    store.write_text(json.dumps(data))

    monkeypatch.setenv("AGY_REMOTE_CREDENTIAL_TTL_DAYS", "0")
    assert _fresh_config(monkeypatch, tmp_path).auth_token == before.auth_token


def test_a_legacy_store_without_a_birthdate_is_stamped_not_discarded(monkeypatch, tmp_path: Path):
    """Existing pairings must survive the upgrade; the clock starts now."""
    before = _fresh_config(monkeypatch, tmp_path)
    store = tmp_path / "credentials.json"
    data = json.loads(store.read_text())
    del data["created_at"]
    store.write_text(json.dumps(data))

    assert _fresh_config(monkeypatch, tmp_path).auth_token == before.auth_token
    assert "created_at" in json.loads(store.read_text())


def test_rotate_credentials_issues_new_ones(monkeypatch, tmp_path: Path):
    from agy_remote import config as config_mod

    before = _fresh_config(monkeypatch, tmp_path)
    config_mod.rotate_credentials()
    after = _fresh_config(monkeypatch, tmp_path)

    assert after.auth_token != before.auth_token
    assert after.e2ee_key != before.e2ee_key


def test_stored_credentials_carry_their_deadline_into_the_config(monkeypatch, tmp_path: Path):
    """The server needs the deadline at request time, not just a boot-time verdict."""
    from datetime import UTC, datetime, timedelta

    cfg = _fresh_config(monkeypatch, tmp_path)
    assert cfg.credentials_expire_at is not None
    remaining = cfg.credentials_expire_at - datetime.now(UTC)
    assert timedelta(days=29) < remaining <= timedelta(days=30)


def test_an_explicit_env_token_has_no_deadline(monkeypatch, tmp_path: Path):
    from agy_remote import config as config_mod

    monkeypatch.setattr(config_mod, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config_mod, "config_instance", None)
    monkeypatch.setenv("AGY_REMOTE_TOKEN", "explicit-token")
    monkeypatch.delenv("AGY_REMOTE_E2EE_KEY", raising=False)

    assert config_mod.get_config().credentials_expire_at is None


def test_ttl_zero_produces_no_deadline(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("AGY_REMOTE_CREDENTIAL_TTL_DAYS", "0")
    assert _fresh_config(monkeypatch, tmp_path).credentials_expire_at is None


def test_find_tailscale_binary_explicit(tmp_path: Path):
    from agy_remote.config import find_tailscale_binary

    fake_ts = tmp_path / "tailscale"
    fake_ts.write_text("#!/bin/sh\necho ok\n")
    fake_ts.chmod(0o755)

    assert find_tailscale_binary(str(fake_ts)) == str(fake_ts)
    assert find_tailscale_binary(tmp_path / "nonexistent") is None


def test_find_tailscale_binary_env_var(monkeypatch, tmp_path: Path):
    from agy_remote.config import find_tailscale_binary

    fake_ts = tmp_path / "tailscale"
    fake_ts.write_text("#!/bin/sh\necho ok\n")
    fake_ts.chmod(0o755)

    monkeypatch.setenv("AGY_REMOTE_TAILSCALE_BIN", str(fake_ts))
    assert find_tailscale_binary() == str(fake_ts)


def test_find_tailscale_binary_fallback_locations(monkeypatch, tmp_path: Path):
    from agy_remote import config as config_mod

    monkeypatch.delenv("AGY_REMOTE_TAILSCALE_BIN", raising=False)
    monkeypatch.delenv("AGY_REMOTE_TAILSCALE_PATH", raising=False)
    monkeypatch.setattr(config_mod.shutil, "which", lambda cmd: None)

    fake_app_ts = tmp_path / "Applications" / "Tailscale.app" / "Contents" / "MacOS" / "Tailscale"
    fake_app_ts.parent.mkdir(parents=True, exist_ok=True)
    fake_app_ts.write_text("#!/bin/sh\necho ok\n")
    fake_app_ts.chmod(0o755)

    monkeypatch.setattr(config_mod, "TAILSCALE_SEARCH_LOCATIONS", [fake_app_ts])
    assert config_mod.find_tailscale_binary() == str(fake_app_ts)


def test_get_tailscale_ip_and_dns_with_custom_bin(monkeypatch, tmp_path: Path):
    import subprocess

    from agy_remote.config import ensure_tailscale_cert, get_tailscale_dns_name, get_tailscale_ip

    fake_ts = tmp_path / "custom_tailscale"
    fake_ts.write_text("#!/bin/sh\necho ok\n")
    fake_ts.chmod(0o755)

    called_cmds = []

    def fake_run(cmd, **kwargs):
        called_cmds.append(cmd)
        if "ip" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="100.80.90.100\n", stderr="")
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"Self":{"DNSName":"my-node.ts.net."}}', stderr="")
        if "cert" in cmd:
            if "-" in cmd:
                stdout_content = (
                    "-----BEGIN CERTIFICATE-----\nMIIFakeCert\n-----END CERTIFICATE-----\n"
                    "-----BEGIN PRIVATE KEY-----\nMIIFakeKey\n-----END PRIVATE KEY-----\n"
                )
                return subprocess.CompletedProcess(cmd, 0, stdout=stdout_content, stderr="")
            cert_file = Path(cmd[cmd.index("--cert-file") + 1])
            key_file = Path(cmd[cmd.index("--key-file") + 1])
            cert_file.write_text("CERT")
            key_file.write_text("KEY")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error")

    monkeypatch.setattr(subprocess, "run", fake_run)

    ip = get_tailscale_ip(tailscale_bin=str(fake_ts))
    assert ip == "100.80.90.100"
    assert called_cmds[-1][0] == str(fake_ts)

    dns = get_tailscale_dns_name(tailscale_bin=str(fake_ts))
    assert dns == "my-node.ts.net"
    assert called_cmds[-1][0] == str(fake_ts)

    cert, key = ensure_tailscale_cert("my-node.ts.net", cert_dir=tmp_path, tailscale_bin=str(fake_ts))
    assert cert.exists() and key.exists()
    assert "MIIFakeCert" in cert.read_text()
    assert "MIIFakeKey" in key.read_text()
    assert called_cmds[-1][0] == str(fake_ts)


def test_cli_options_accept_tailscale_path(monkeypatch, tmp_path: Path):
    from click.testing import CliRunner

    from agy_remote import config as config_mod
    from agy_remote.cli import cli

    fake_ts = tmp_path / "custom_tailscale"
    fake_ts.write_text("#!/bin/sh\necho ok\n")
    fake_ts.chmod(0o755)

    monkeypatch.setattr(config_mod, "CREDENTIALS_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(config_mod, "config_instance", None)

    runner = CliRunner()
    # Test --help contains --tailscale-path
    res_serve = runner.invoke(cli, ["serve", "--help"])
    assert "--tailscale-path" in res_serve.output
    assert "--tailscale-bin" in res_serve.output

    res_run = runner.invoke(cli, ["run", "--help"])
    assert "--tailscale-path" in res_run.output
    assert "--tailscale-bin" in res_run.output

