"""Configuration and environment detection for agy-remote."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from .crypto import generate_e2ee_key

logger = logging.getLogger("agy_remote.config")

#: Where a running server publishes the credentials its own helper processes
#: need. The PreToolUse hook is spawned by the agy CLI as a separate process,
#: so it cannot inherit the in-memory config; without this it would mint a
#: fresh random token and fail authentication on every approval request.
RUNTIME_STATE_FILE = Path.home() / ".gemini" / "antigravity-cli" / "agy-remote-session.json"


def get_default_brain_dir() -> Path:
    """Find the default brain directory used by Antigravity CLI."""
    home = Path.home()
    candidates = [
        home / ".gemini" / "antigravity-cli" / "brain",
        home / ".gemini" / "antigravity" / "brain",
        home / ".gemini" / "antigravity-ide" / "brain",
    ]
    for c in candidates:
        if c.exists():
            return c
    # Default to standard CLI path
    return home / ".gemini" / "antigravity-cli" / "brain"


def get_tailscale_ip() -> str | None:
    """Attempt to detect the local machine's Tailscale IPv4 address."""
    try:
        res = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if res.returncode == 0:
            ip = res.stdout.strip().splitlines()[0]
            if ip:
                return ip
    except Exception:
        pass
    return None


#: Interfaces that never carry an address a phone on your Wi-Fi can reach:
#: VPN tunnels, internet-sharing bridges, container and VM networks, AirDrop.
VIRTUAL_IFACE_PREFIXES = (
    "lo",
    "utun",
    "tun",
    "tap",
    "ppp",
    "ipsec",
    "awdl",
    "llw",
    "anpi",
    "ap",
    "bridge",
    "vmnet",
    "docker",
    "veth",
    "gif",
    "stf",
    "vboxnet",
    "zt",
)
#: Physical LAN interfaces, in the order we prefer them.
PHYSICAL_IFACE_PREFIXES = ("en", "eth", "wl", "wlan")


def parse_interface_addresses(output: str) -> list[tuple[str, str]]:
    """Extract (interface, IPv4) pairs from `ifconfig` or `ip -4 addr` output."""
    pairs: list[tuple[str, str]] = []
    current = ""
    for line in output.splitlines():
        if not line:
            continue
        # `ifconfig` starts an interface block in column 0; `ip addr` prefixes
        # each line with "N: name".
        if not line[0].isspace():
            head = line.split(":", 1)
            if len(head) == 2 and head[0].strip().isdigit():
                current = head[1].strip().split()[0] if head[1].strip() else ""
            else:
                current = head[0].strip()
        match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if match and current:
            pairs.append((current, match.group(1)))
    return pairs


def pick_lan_address(pairs: list[tuple[str, str]]) -> str | None:
    """Choose the address a phone on the same network can actually reach.

    Returns None if no physical interface has a usable private address.
    """
    for iface, ip in pairs:
        name = iface.lower()
        if name.startswith(VIRTUAL_IFACE_PREFIXES):
            continue
        if not name.startswith(PHYSICAL_IFACE_PREFIXES):
            continue
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if addr.is_loopback or addr.is_link_local:
            continue
        return ip
    return None


def get_lan_ip() -> str:
    """Get the LAN IPv4 address to advertise to mobile clients.

    Enumerating interfaces is preferred over the usual UDP-connect probe: that
    probe reports whichever interface holds the default route, which is the
    tunnel whenever a VPN is up - an address no phone on your Wi-Fi can reach.
    """
    for cmd in (["ifconfig", "-a"], ["ip", "-4", "addr"]):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode == 0:
            found = pick_lan_address(parse_interface_addresses(res.stdout))
            if found:
                return found

    # Fall back to asking the routing table which source address it would use.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def get_hostname() -> str:
    """Get local hostname."""
    return socket.gethostname()


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


class InsecureConfigError(RuntimeError):
    """Raised when a requested configuration would expose the host unsafely."""


def is_loopback_host(host: str) -> bool:
    """True if binding to `host` only accepts connections from this machine."""
    return host.strip().strip("[]").lower() in LOOPBACK_HOSTS


def validate_bind_security(cfg: RemoteConfig) -> None:
    """Refuse configurations that expose an unauthenticated agent to the network.

    Sending a prompt to agy-remote injects it straight into the running `agy`
    CLI, which is arbitrary code execution on this machine. Without a token
    that must never be reachable beyond loopback.

    Raises:
        InsecureConfigError: auth disabled on a non-loopback bind.
    """
    if not cfg.enable_auth and not is_loopback_host(cfg.host):
        raise InsecureConfigError(
            f"Refusing to disable authentication while bound to {cfg.host!r}.\n"
            "Prompts sent to agy-remote execute inside your agy session, so an "
            "unauthenticated listener on a LAN or tailnet address is remote code "
            "execution for anyone who can reach it.\n"
            "Either keep authentication enabled, or bind to 127.0.0.1 as well."
        )


class RemoteConfig(BaseModel):
    """Runtime configuration for agy-remote server."""

    host: str = "0.0.0.0"
    port: int = 8765
    auth_token: str = Field(default_factory=lambda: secrets.token_urlsafe(16))
    e2ee_key: str = Field(default_factory=generate_e2ee_key)
    e2ee_enabled: bool = True
    brain_dir: Path = Field(default_factory=get_default_brain_dir)
    enable_auth: bool = True
    tailscale_ip: str | None = Field(default_factory=get_tailscale_ip)
    lan_ip: str = Field(default_factory=get_lan_ip)
    hostname: str = Field(default_factory=get_hostname)

    def get_connect_urls(self) -> list[tuple[str, str]]:
        """Return list of (label, url) for mobile connection."""
        urls = []
        token_param = f"?token={self.auth_token}" if self.enable_auth else ""
        hash_fragment = f"#key={self.e2ee_key}" if self.e2ee_enabled else ""

        if self.tailscale_ip:
            urls.append(
                (
                    "Tailscale (Preferred Mobile)",
                    f"http://{self.tailscale_ip}:{self.port}/{token_param}{hash_fragment}",
                )
            )

        if self.lan_ip and self.lan_ip != "127.0.0.1":
            urls.append(
                (
                    "Local Wi-Fi / LAN",
                    f"http://{self.lan_ip}:{self.port}/{token_param}{hash_fragment}",
                )
            )

        urls.append(
            (
                "Localhost",
                f"http://localhost:{self.port}/{token_param}{hash_fragment}",
            )
        )

        return urls

    def get_primary_mobile_url(self) -> str:
        """Get best URL for mobile QR code."""
        urls = self.get_connect_urls()
        return urls[0][1] if urls else f"http://localhost:{self.port}/"


config_instance: RemoteConfig | None = None


def get_config() -> RemoteConfig:
    """Get global configuration singleton."""
    global config_instance
    if config_instance is None:
        token = os.environ.get("AGY_REMOTE_TOKEN")
        e2ee_key = os.environ.get("AGY_REMOTE_E2EE_KEY")
        brain_path_env = os.environ.get("AGY_BRAIN_DIR")
        brain_dir = Path(brain_path_env) if brain_path_env else get_default_brain_dir()
        port = int(os.environ.get("AGY_REMOTE_PORT", "8765"))
        host = os.environ.get("AGY_REMOTE_HOST", "0.0.0.0")
        no_auth = os.environ.get("AGY_REMOTE_NO_AUTH", "0").lower() in (
            "1",
            "true",
            "yes",
        )
        no_e2ee = os.environ.get("AGY_REMOTE_NO_E2EE", "0").lower() in (
            "1",
            "true",
            "yes",
        )

        kwargs = {
            "host": host,
            "port": port,
            "brain_dir": brain_dir,
            "enable_auth": not no_auth,
            "e2ee_enabled": not no_e2ee,
        }
        if token:
            kwargs["auth_token"] = token
        if e2ee_key:
            kwargs["e2ee_key"] = e2ee_key

        config_instance = RemoteConfig(**kwargs)
    return config_instance


def write_runtime_state(cfg: RemoteConfig) -> Path:
    """Publish the live token and port for helper processes to pick up.

    Written owner-only: the token is equivalent to shell access on this machine.
    """
    RUNTIME_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "auth_token": cfg.auth_token,
            "e2ee_key": cfg.e2ee_key,
            "e2ee_enabled": cfg.e2ee_enabled,
            "enable_auth": cfg.enable_auth,
            "port": cfg.port,
            "pid": os.getpid(),
        },
        indent=2,
    )
    fd = os.open(RUNTIME_STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    os.chmod(RUNTIME_STATE_FILE, 0o600)
    return RUNTIME_STATE_FILE


def clear_runtime_state() -> None:
    """Remove the published credentials when the server shuts down."""
    try:
        RUNTIME_STATE_FILE.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("Could not clear runtime state: %s", e)


def read_runtime_state() -> dict | None:
    """Read the running server's token and port, or None if none is published."""
    try:
        with open(RUNTIME_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "auth_token" in data:
            return data
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Could not read runtime state: %s", e)
    return None


def adopt_runtime_state(cfg: RemoteConfig) -> RemoteConfig:
    """Point `cfg` at the credentials of an already-running server, if any.

    Commands like `qr` run in their own process, where a default-constructed
    RemoteConfig invents a fresh token and key. Pairing against those would
    hand the phone credentials the live server has never heard of.
    """
    state = read_runtime_state()
    if not state:
        return cfg

    cfg.auth_token = state.get("auth_token", cfg.auth_token)
    cfg.port = state.get("port", cfg.port)
    if state.get("e2ee_key"):
        cfg.e2ee_key = state["e2ee_key"]
    if "e2ee_enabled" in state:
        cfg.e2ee_enabled = bool(state["e2ee_enabled"])
    if "enable_auth" in state:
        cfg.enable_auth = bool(state["enable_auth"])
    return cfg
