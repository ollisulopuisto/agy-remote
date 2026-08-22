"""Configuration and environment detection for agy-remote."""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from .crypto import generate_e2ee_key

logger = logging.getLogger("agy_remote.config")

#: Where a running server publishes the credentials its own helper processes
#: need. The PreToolUse hook is spawned by the agy CLI as a separate process,
#: so it cannot inherit the in-memory config; without this it would mint a
#: fresh random token and fail authentication on every approval request.
RUNTIME_STATE_FILE = Path.home() / ".gemini" / "antigravity-cli" / "agy-remote-session.json"

#: One file per running server, named by port. The state file above can only
#: describe one server, so with two running, a hook that had to fall back to it
#: sent both sessions' approvals to whichever wrote it last. This lets a hook
#: find the server that owns *its* session instead.
SERVER_REGISTRY_DIR = Path.home() / ".gemini" / "antigravity-cli" / "agy-remote-servers"
CREDENTIALS_FILE = Path.home() / ".gemini" / "antigravity-cli" / "agy-remote-credentials.json"

#: Standard locations where Tailscale CLI binary is typically installed across platforms.
TAILSCALE_SEARCH_LOCATIONS: list[Path] = [
    # macOS application bundle locations
    Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    Path("/Applications/Tailscale.app/Contents/Resources/tailscale"),
    Path.home() / "Applications" / "Tailscale.app" / "Contents" / "MacOS" / "Tailscale",
    # Homebrew & standard Unix locations
    Path("/opt/homebrew/bin/tailscale"),
    Path("/usr/local/bin/tailscale"),
    Path("/usr/bin/tailscale"),
    Path("/usr/sbin/tailscale"),
    Path.home() / ".local" / "bin" / "tailscale",
    # Linux packages (Flatpak, Snap, etc.)
    Path("/var/lib/flatpak/exports/bin/tailscale"),
    Path.home() / ".local" / "share" / "flatpak" / "exports" / "bin" / "tailscale",
    Path("/snap/bin/tailscale"),
]


def find_tailscale_binary(custom_path: str | Path | None = None) -> str | None:
    """Find the Tailscale CLI executable from custom path, env var, PATH, or standard locations."""
    if custom_path:
        str_path = str(custom_path)
        if os.sep not in str_path and (os.altsep is None or os.altsep not in str_path):
            found = shutil.which(str_path)
            if found:
                return found
        p = Path(custom_path).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        logger.warning("Specified Tailscale binary does not exist or is not executable: %s", custom_path)
        return None

    for env_var in ("AGY_REMOTE_TAILSCALE_BIN", "AGY_REMOTE_TAILSCALE_PATH", "TAILSCALE_BIN"):
        val = os.environ.get(env_var)
        if val:
            p = Path(val).expanduser()
            if p.is_file() and os.access(p, os.X_OK):
                return str(p)

    found = shutil.which("tailscale")
    if found:
        return found

    for loc in TAILSCALE_SEARCH_LOCATIONS:
        try:
            loc_expanded = loc.expanduser()
            if loc_expanded.is_file() and os.access(loc_expanded, os.X_OK):
                return str(loc_expanded)
        except OSError:
            continue

    return None


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


def get_tailscale_ip(tailscale_bin: str | Path | None = None) -> str | None:
    """Attempt to detect the local machine's Tailscale IPv4 address."""
    binary = find_tailscale_binary(tailscale_bin)
    if not binary:
        return None
    try:
        res = subprocess.run(
            [binary, "ip", "-4"],
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


def parse_tailscale_dns_name(status_json: str) -> str | None:
    """Extract this node's MagicDNS name from `tailscale status --json`."""
    try:
        data = json.loads(status_json)
    except (json.JSONDecodeError, TypeError):
        return None
    name = (data.get("Self") or {}).get("DNSName") or ""
    return name.rstrip(".") or None


def get_tailscale_dns_name(tailscale_bin: str | Path | None = None) -> str | None:
    """This node's MagicDNS name, needed to request a TLS certificate."""
    binary = find_tailscale_binary(tailscale_bin)
    if not binary:
        return None
    try:
        res = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_tailscale_dns_name(res.stdout) if res.returncode == 0 else None


TLS_DIR = Path.home() / ".gemini" / "antigravity-cli" / "tls"


class TailscaleCertError(RuntimeError):
    """Raised when a Tailscale TLS certificate could not be obtained."""


def ensure_tailscale_cert(
    dns_name: str,
    cert_dir: Path | None = None,
    tailscale_bin: str | Path | None = None,
) -> tuple[Path, Path]:
    """Fetch (or refresh) a Let's Encrypt certificate for this tailnet node.

    Browsers only expose Web Crypto in a secure context, so HTTPS is what makes
    payload encryption possible on a phone at all. `tailscale cert` issues a
    genuine certificate for the MagicDNS name, which phones trust with no
    warning and no manual certificate installation.

    Raises:
        TailscaleCertError: if the certificate could not be issued.
    """
    binary = find_tailscale_binary(tailscale_bin)
    if not binary:
        raise TailscaleCertError("Tailscale CLI executable not found.")

    cert_dir = cert_dir or TLS_DIR
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / f"{dns_name}.crt"
    key_path = cert_dir / f"{dns_name}.key"

    # Try streaming cert and key to stdout first. This bypasses sandbox write
    # restrictions on macOS (where Tailscale.app cannot write directly to ~/.gemini/...)
    try:
        res = subprocess.run(
            [
                binary,
                "cert",
                "--cert-file",
                "-",
                "--key-file",
                "-",
                dns_name,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if res.returncode == 0 and "-----BEGIN CERTIFICATE-----" in (res.stdout or ""):
            stdout = res.stdout
            key_markers = (
                "-----BEGIN PRIVATE KEY-----",
                "-----BEGIN EC PRIVATE KEY-----",
                "-----BEGIN RSA PRIVATE KEY-----",
            )
            key_idx = -1
            for marker in key_markers:
                idx = stdout.find(marker)
                if idx != -1:
                    key_idx = idx
                    break

            if key_idx != -1:
                cert_data = stdout[:key_idx].strip() + "\n"
                key_data = stdout[key_idx:].strip() + "\n"
                cert_path.write_text(cert_data, encoding="utf-8")
                # 0600 from creation: write_text-then-chmod leaves the private
                # key world-readable for a moment under the default umask.
                key_fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(key_fd, "w", encoding="utf-8") as key_file:
                    key_file.write(key_data)
                os.chmod(key_path, 0o600)
                return cert_path, key_path
    except (OSError, subprocess.SubprocessError):
        pass

    # Fallback to direct file output
    try:
        res = subprocess.run(
            [
                binary,
                "cert",
                "--cert-file",
                str(cert_path),
                "--key-file",
                str(key_path),
                dns_name,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise TailscaleCertError(f"Could not run `{binary} cert`: {e}") from e

    if res.returncode != 0 or not cert_path.exists() or not key_path.exists():
        detail = (res.stderr or res.stdout or "").strip()
        raise TailscaleCertError(
            f"`{binary} cert` failed for {dns_name}.\n{detail}\n"
            "HTTPS certificates must be enabled for your tailnet in the admin "
            "console (DNS -> HTTPS Certificates)."
        )

    os.chmod(key_path, 0o600)
    return cert_path, key_path


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
    #: When the stored pairing stops being honored, or None for no deadline
    #: (an explicit --token / env token, or TTL 0). Enforced per auth check:
    #: a boot-time verdict alone would let a long-running server honor an
    #: expired pairing until its next restart.
    credentials_expire_at: datetime | None = None

    def pairing_expired(self) -> bool:
        """Whether the stored pairing has outlived its TTL as of right now."""
        return self.credentials_expire_at is not None and datetime.now(UTC) > self.credentials_expire_at

    e2ee_key: str = Field(default_factory=generate_e2ee_key)
    e2ee_enabled: bool = True
    brain_dir: Path = Field(default_factory=get_default_brain_dir)
    enable_auth: bool = True
    #: The tmux session this server drives when it adopted one it did not
    #: start, by name and by the numeric id tmux exports in `$TMUX`. Published
    #: so a hook running inside that session can find this server rather than
    #: whichever one happens to own the shared state file.
    tmux_session: str | None = None
    tmux_session_id: str | None = None
    #: The pane keys are sent to, as `session:window.pane`. A bare session name
    #: aims at whichever pane is active in it, which is the user's own work.
    tmux_target: str | None = None
    #: The agent CLI this server fronts. One value today, kept as a field
    #: because the PWA renders it: the header used to say "agy" whatever was
    #: behind it, which made a session look like something it was not.
    agent: str = "agy"
    tailscale_bin: str | None = Field(default_factory=find_tailscale_binary)
    tailscale_ip: str | None = None
    tailscale_dns_name: str | None = None
    lan_ip: str = Field(default_factory=get_lan_ip)
    hostname: str = Field(default_factory=get_hostname)
    tls_cert: Path | None = None
    tls_key: Path | None = None

    def model_post_init(self, __context: object) -> None:
        if "tailscale_ip" not in self.model_fields_set:
            self.tailscale_ip = get_tailscale_ip(self.tailscale_bin)

    @property
    def tls_enabled(self) -> bool:
        """True when the server can serve HTTPS, making Web Crypto available."""
        return bool(self.tls_cert and self.tls_key)

    @property
    def scheme(self) -> str:
        return "https" if self.tls_enabled else "http"

    @property
    def local_base_url(self) -> str:
        """Base URL for same-machine helpers such as the PreToolUse hook.

        Under TLS this must be the MagicDNS name, not a loopback address: the
        certificate is issued for that name, so https://127.0.0.1 would fail
        verification. MagicDNS resolves it locally, so the request stays on
        this machine.
        """
        if self.tls_enabled and self.tailscale_dns_name:
            return f"https://{self.tailscale_dns_name}:{self.port}"
        return f"http://127.0.0.1:{self.port}"

    def get_connect_urls(self) -> list[tuple[str, str]]:
        """Return list of (label, url) for mobile connection."""
        urls = []
        token_param = f"?token={self.auth_token}" if self.enable_auth else ""
        hash_fragment = f"#key={self.e2ee_key}" if self.e2ee_enabled else ""

        # A TLS certificate is issued for the MagicDNS name, so the URL must
        # use that name rather than the raw IP or the certificate will not match.
        if self.tls_enabled and self.tailscale_dns_name:
            urls.append(
                (
                    "Tailscale HTTPS (Preferred Mobile)",
                    f"https://{self.tailscale_dns_name}:{self.port}/{token_param}{hash_fragment}",
                )
            )
        elif self.tailscale_ip:
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
                    f"{self.scheme}://{self.lan_ip}:{self.port}/{token_param}{hash_fragment}",
                )
            )

        urls.append(
            (
                "Localhost",
                f"{self.scheme}://localhost:{self.port}/{token_param}{hash_fragment}",
            )
        )

        return urls

    def get_primary_mobile_url(self) -> str:
        """Get best URL for mobile QR code."""
        urls = self.get_connect_urls()
        return urls[0][1] if urls else f"http://localhost:{self.port}/"


config_instance: RemoteConfig | None = None


def get_config(tailscale_bin: str | Path | None = None) -> RemoteConfig:
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
        if tailscale_bin:
            kwargs["tailscale_bin"] = str(tailscale_bin)
        stored = load_or_create_credentials()
        kwargs["auth_token"] = token or stored["auth_token"]
        kwargs["e2ee_key"] = e2ee_key or stored["e2ee_key"]
        if not token:
            # An explicit token is the operator's own; only stored pairings age.
            kwargs["credentials_expire_at"] = stored["expires_at"]

        config_instance = RemoteConfig(**kwargs)
    elif tailscale_bin:
        config_instance.tailscale_bin = str(tailscale_bin)
        config_instance.tailscale_ip = get_tailscale_ip(tailscale_bin)
    return config_instance


#: How long a pairing stays valid. The stored token turned every phone bookmark
#: into a credential that never expires; a leaked QR screenshot or a lost phone
#: should not stay a way in forever. 0 disables expiry.
DEFAULT_CREDENTIAL_TTL_DAYS = 30


def credential_ttl_days() -> int:
    """The configured pairing lifetime, in days."""
    try:
        return int(os.environ.get("AGY_REMOTE_CREDENTIAL_TTL_DAYS", DEFAULT_CREDENTIAL_TTL_DAYS))
    except ValueError:
        return DEFAULT_CREDENTIAL_TTL_DAYS


def read_stored_token() -> str | None:
    """This host's stored auth token, or None -- never minting a new one.

    The PreToolUse hook of a second server has no runtime state file to read
    (the first server owns it), and `load_or_create_credentials` would mint a
    token the server has never heard of, 401ing on every approval.
    """
    try:
        with open(CREDENTIALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Could not read stored credentials: %s", e)
        return None
    token = data.get("auth_token") if isinstance(data, dict) else None
    return str(token) if token else None


def load_or_create_credentials() -> dict[str, str]:
    """Return this host's token and E2EE key, minting them once and expiring by age.

    Both were previously regenerated on every launch, so each restart silently
    invalidated the URL saved on the phone: the QR had to be rescanned, and any
    bookmark or installed PWA came back with a dead token. They are properties
    of the host, not of a process, so they are kept on disk and reused -- but a
    pairing URL is a durable secret, so it expires after `credential_ttl_days()`
    and the next launch re-pairs with a fresh QR.

    Owner-only, like the runtime state: the token is equivalent to shell access
    on this machine, and the key decrypts every payload the phone ever sees.
    """
    try:
        with open(CREDENTIALS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("auth_token") and data.get("e2ee_key"):
            if "created_at" not in data:
                # A store from before expiry existed: keep the pairing alive
                # and start its clock now.
                data["created_at"] = datetime.now(UTC).isoformat()
                _write_credentials(data)

            if not _credentials_expired(data.get("created_at")):
                return {
                    "auth_token": str(data["auth_token"]),
                    "e2ee_key": str(data["e2ee_key"]),
                    "expires_at": _deadline_for(data.get("created_at")),
                }
            logger.info("Stored credentials are older than %s days; issuing new ones", credential_ttl_days())
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Could not read stored credentials: %s", e)

    credentials = {
        "auth_token": secrets.token_urlsafe(16),
        "e2ee_key": generate_e2ee_key(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    _write_credentials(credentials)
    return {
        "auth_token": credentials["auth_token"],
        "e2ee_key": credentials["e2ee_key"],
        "expires_at": _deadline_for(credentials["created_at"]),
    }


def _deadline_for(created_at: str | None) -> datetime | None:
    """When a pairing minted at `created_at` expires, or None if it never does."""
    ttl_days = credential_ttl_days()
    if ttl_days <= 0:
        return None

    try:
        born = datetime.fromisoformat(str(created_at))
    except (ValueError, TypeError):
        return datetime.now(UTC)  # unreadable birthdate: already expired

    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    return born + timedelta(days=ttl_days)


def _credentials_expired(created_at: str | None) -> bool:
    """Whether a pairing minted at `created_at` has outlived the TTL."""
    ttl_days = credential_ttl_days()
    if ttl_days <= 0:
        return False

    try:
        born = datetime.fromisoformat(str(created_at))
    except (ValueError, TypeError):
        # An unreadable birthdate on a secret defaults to expired, not eternal.
        return True

    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    return datetime.now(UTC) - born > timedelta(days=ttl_days)


def _write_credentials(credentials: dict[str, str]) -> None:
    """Persist credentials owner-only, tolerating an unwritable home directory."""
    try:
        CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(credentials, indent=2))
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError as e:
        # Not fatal: the server still runs, it just issues fresh credentials
        # next time, which is exactly the old behaviour.
        logger.debug("Could not store credentials: %s", e)


def rotate_credentials() -> None:
    """Discard the stored credentials so the next launch mints new ones.

    Every paired phone is revoked by this, which is the point of it.
    """
    try:
        CREDENTIALS_FILE.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("Could not rotate credentials: %s", e)


def _pid_alive(pid: int) -> bool:
    """Whether a process with this pid still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Someone else's process, but a live one.
        return True
    except OSError:
        return False
    return True


def runtime_state_owner() -> dict | None:
    """The state of another *live* server, or None if nobody else owns it.

    A crashed server leaves its file behind, so the pid is the only honest
    signal that the credentials in it still lead anywhere.
    """
    state = read_runtime_state()
    if not state:
        return None
    pid = state.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
        return state
    return None


def live_runtime_state() -> dict | None:
    """The published state, but only if the server that published it is alive.

    `runtime_state_owner` answers a different question -- "does *another* live
    server own this file?" -- and excludes our own pid, which is what a second
    server starting up needs to know. A hook is never the server, so it wants
    the plain reading: is whoever wrote this still there? A crashed server
    leaves its credentials behind, and posting every tool call to a port nobody
    holds is at best a wasted round trip.

    A file with no pid predates that field and is taken at its word.
    """
    state = read_runtime_state()
    if not state:
        return None
    pid = state.get("pid")
    if pid is None or (isinstance(pid, int) and _pid_alive(pid)):
        return state
    return None


def port_is_free(host: str, port: int) -> bool:
    """Whether a server could bind here, checked the way uvicorn will bind.

    Same SO_REUSEADDR as uvicorn, so a port held in TIME_WAIT reads as free
    exactly when uvicorn would accept it. A wildcard bind is additionally
    probed on loopback: BSD lets 0.0.0.0 coexist with a 127.0.0.1 listener at
    bind time, which would let the real failure through this check.
    """
    candidates = [host]
    if host in ("0.0.0.0", "::", ""):
        candidates.append("127.0.0.1")

    for candidate in candidates:
        try:
            family = socket.AF_INET6 if ":" in candidate else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((candidate, port))
        except OSError:
            return False
    return True


def find_free_port(host: str = "127.0.0.1") -> int:
    """Find an available ephemeral TCP port on host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def publish_server_registration(cfg: RemoteConfig) -> Path | None:
    """Record this server so a hook can find it by the session it drives.

    Unlike the shared state file there is no ownership fight here: every server
    writes its own file, named by the port it holds.
    """
    try:
        SERVER_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.debug("Could not create the server registry: %s", e)
        return None

    path = SERVER_REGISTRY_DIR / f"{cfg.port}.json"
    payload = json.dumps(
        {
            "auth_token": cfg.auth_token,
            "base_url": cfg.local_base_url,
            "port": cfg.port,
            "tmux_session": cfg.tmux_session,
            "tmux_session_id": cfg.tmux_session_id,
            "pid": os.getpid(),
        },
        indent=2,
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except OSError as e:
        logger.debug("Could not publish server registration: %s", e)
        return None
    return path


def withdraw_server_registration(port: int, owner_pid: int | None = None) -> None:
    """Remove our own registration, and only ever our own.

    A server restarting on the same port can finish shutting down after its
    replacement has already registered; deleting unconditionally would strand
    the new one.
    """
    path = SERVER_REGISTRY_DIR / f"{port}.json"
    if owner_pid is not None:
        try:
            with open(path, encoding="utf-8") as f:
                if json.load(f).get("pid") != owner_pid:
                    return
        except (OSError, json.JSONDecodeError):
            return
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def find_server_on_port(port: int) -> dict | None:
    """The live server holding this port, from its own registration.

    The shared state file names one server; a second instance never owns it.
    This answers "who has 8766?" for any of them, and only while its pid is
    alive -- a registration outlives a crash.
    """
    path = SERVER_REGISTRY_DIR / f"{port}.json"
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    pid = entry.get("pid")
    if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
        return entry
    return None


def find_server_for_tmux_session(session_id: str) -> dict | None:
    """The live server driving the tmux session with this id, if any.

    A registration outlives a crash, so the pid is the only honest signal that
    the credentials in it still lead anywhere.
    """
    if not session_id:
        return None
    try:
        entries = sorted(SERVER_REGISTRY_DIR.glob("*.json"))
    except OSError:
        return None

    for path in entries:
        try:
            with open(path, encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if str(entry.get("tmux_session_id") or "") != str(session_id):
            continue
        pid = entry.get("pid")
        if isinstance(pid, int) and _pid_alive(pid):
            return entry
    return None


def write_runtime_state(cfg: RemoteConfig) -> Path | None:
    """Publish the live token and port for helper processes to pick up.

    Written owner-only: the token is equivalent to shell access on this machine.

    Returns None without writing when another live server already owns the
    file. The PreToolUse hook reads exactly one endpoint, so clobbering it
    would silently redirect the first server's approvals to this one's phone.
    """
    owner = runtime_state_owner()
    if owner is not None:
        logger.warning(
            "Runtime state belongs to a live server (pid %s, port %s); "
            "not publishing ours, tool approvals stay with it",
            owner.get("pid"),
            owner.get("port"),
        )
        return None

    RUNTIME_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "auth_token": cfg.auth_token,
            "base_url": cfg.local_base_url,
            "e2ee_key": cfg.e2ee_key,
            "e2ee_enabled": cfg.e2ee_enabled,
            "enable_auth": cfg.enable_auth,
            "port": cfg.port,
            "agent": cfg.agent,
            "tmux_session": cfg.tmux_session,
            # `qr` builds its URLs from a config of its own, and without these
            # it advertised http://<ip> for a server serving https://<magicdns>
            # -- a QR code that pairs a phone to a port that will not answer it.
            "tls_cert": str(cfg.tls_cert) if cfg.tls_cert else None,
            "tls_key": str(cfg.tls_key) if cfg.tls_key else None,
            "tailscale_dns_name": cfg.tailscale_dns_name,
            "pid": os.getpid(),
        },
        indent=2,
    )
    fd = os.open(RUNTIME_STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    os.chmod(RUNTIME_STATE_FILE, 0o600)
    return RUNTIME_STATE_FILE


def clear_runtime_state(owner_pid: int | None = None) -> None:
    """Remove the published credentials, but only if we published them.

    On a quick restart the outgoing server can finish shutting down after the
    incoming one has already written its file. Clearing unconditionally then
    deletes the *new* server's credentials, leaving `qr` and the PreToolUse
    hook with nothing to find.
    """
    if owner_pid is not None:
        state = read_runtime_state()
        if state is not None and state.get("pid") != owner_pid:
            logger.debug("Runtime state belongs to pid %s; leaving it alone", state.get("pid"))
            return
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
    if state.get("agent"):
        cfg.agent = state["agent"]
    if state.get("tmux_session"):
        cfg.tmux_session = state["tmux_session"]
    # Reproduce the scheme and host the running server publishes, not this
    # process's guess at them.
    if state.get("tls_cert") and state.get("tls_key"):
        cfg.tls_cert = Path(state["tls_cert"])
        cfg.tls_key = Path(state["tls_key"])
    if state.get("tailscale_dns_name"):
        cfg.tailscale_dns_name = state["tailscale_dns_name"]
    return cfg
