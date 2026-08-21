"""Configuration and environment detection for agy-remote."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field


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


def get_lan_ip() -> str:
    """Get the primary local area network IPv4 address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        # Connect to an arbitrary public IP to find outgoing interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_hostname() -> str:
    """Get local hostname."""
    return socket.gethostname()


class RemoteConfig(BaseModel):
    """Runtime configuration for agy-remote server."""

    host: str = "0.0.0.0"
    port: int = 8765
    auth_token: str = Field(default_factory=lambda: secrets.token_urlsafe(16))
    brain_dir: Path = Field(default_factory=get_default_brain_dir)
    enable_auth: bool = True
    tailscale_ip: str | None = Field(default_factory=get_tailscale_ip)
    lan_ip: str = Field(default_factory=get_lan_ip)
    hostname: str = Field(default_factory=get_hostname)

    def get_connect_urls(self) -> list[tuple[str, str]]:
        """Return list of (label, url) for mobile connection."""
        urls = []
        token_param = f"?token={self.auth_token}" if self.enable_auth else ""

        if self.tailscale_ip:
            urls.append(
                (
                    "Tailscale (Preferred Mobile)",
                    f"http://{self.tailscale_ip}:{self.port}/{token_param}",
                )
            )

        if self.lan_ip and self.lan_ip != "127.0.0.1":
            urls.append(
                (
                    "Local Wi-Fi / LAN",
                    f"http://{self.lan_ip}:{self.port}/{token_param}",
                )
            )

        urls.append(
            (
                "Localhost",
                f"http://localhost:{self.port}/{token_param}",
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
        brain_path_env = os.environ.get("AGY_BRAIN_DIR")
        brain_dir = Path(brain_path_env) if brain_path_env else get_default_brain_dir()
        port = int(os.environ.get("AGY_REMOTE_PORT", "8765"))
        host = os.environ.get("AGY_REMOTE_HOST", "0.0.0.0")
        no_auth = os.environ.get("AGY_REMOTE_NO_AUTH", "0").lower() in (
            "1",
            "true",
            "yes",
        )

        kwargs = {
            "host": host,
            "port": port,
            "brain_dir": brain_dir,
            "enable_auth": not no_auth,
        }
        if token:
            kwargs["auth_token"] = token

        config_instance = RemoteConfig(**kwargs)
    return config_instance
