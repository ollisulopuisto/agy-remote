"""Command Line Interface for agy-remote."""

from __future__ import annotations

import contextlib
import io
import logging
import math
import select
import sys
import threading
import time
from pathlib import Path

import click
import qrcode
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import (
    InsecureConfigError,
    RemoteConfig,
    TailscaleCertError,
    adopt_runtime_state,
    ensure_tailscale_cert,
    find_free_port,
    get_config,
    get_tailscale_dns_name,
    is_loopback_host,
    port_is_free,
    publish_server_registration,
    read_runtime_state,
    rotate_credentials,
    runtime_state_owner,
)
from .hooks import hook_health, install_hooks_config, run_pre_tool_hook
from .pty_runner import PtySupervisor, set_pty_supervisor
from .push import get_push_manager
from .screen import TmuxScreen
from .server import create_app
from .tmux_runner import (
    TmuxSupervisor,
    is_tmux_available,
    session_id_of,
    session_name_for_port,
    sessions_running,
    set_tmux_supervisor,
)
from .version import __version__

logger = logging.getLogger("agy_remote.cli")
console = Console()


def print_qr_code(url: str) -> None:
    """Print ASCII QR code in terminal for mobile phone scanning."""
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    f = io.StringIO()
    qr.print_ascii(out=f, invert=True)
    console.print(f.getvalue(), style="bold white")


def print_banner(cfg: RemoteConfig, mode: str = "Standalone") -> None:
    """Print a rich terminal startup banner with connection links and QR code."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("Key", style="bold cyan")
    table.add_column("Value", style="white")

    urls = cfg.get_connect_urls()
    for label, url in urls:
        table.add_row(f"{label}:", f"[underline green]{url}[/]")

    table.add_row("Mode:", f"[bold magenta]{mode}[/]")
    if cfg.agent != "agy":
        table.add_row("Agent:", f"[bold cyan]{cfg.agent}[/]")
    if cfg.enable_auth:
        table.add_row("Auth Token:", f"[yellow]{cfg.auth_token}[/]")
    if cfg.e2ee_enabled:
        if cfg.tls_enabled:
            table.add_row("E2EE Status:", "[bold green]Active (AES-256-GCM over HTTPS)[/]")
        else:
            table.add_row(
                "E2EE Status:",
                "[yellow]Key issued, but browsers need HTTPS for Web Crypto[/]",
            )
    if cfg.tls_enabled:
        table.add_row("TLS:", "[bold green]Tailscale certificate[/]")
    table.add_row("Brain Dir:", f"[dim]{cfg.brain_dir}[/]")
    if not cfg.enable_auth:
        table.add_row("Auth:", "[bold red]DISABLED (loopback only)[/]")
    if not cfg.e2ee_enabled:
        table.add_row("E2EE Status:", "[bold red]DISABLED - payloads are cleartext[/]")
    if not is_loopback_host(cfg.host):
        table.add_row(
            "Exposure:",
            f"[yellow]Reachable from the network on {cfg.host}:{cfg.port}. Prompts execute inside your agy session.[/]",
        )

    panel = Panel(
        table,
        title="[bold green]🚀 Antigravity Mobile Remote (agy-remote)[/]",
        subtitle="[dim]Scan QR code below with your mobile camera or browser[/]",
        border_style="green",
    )
    console.print(panel)
    console.print()

    primary_url = cfg.get_primary_mobile_url()
    console.print(f"[bold]Scan to Connect Mobile Device:[/bold] [dim]({primary_url})[/dim]")
    print_qr_code(primary_url)
    console.print()


def _setup_tls(cfg: RemoteConfig, tls: bool | None) -> None:
    """Obtain a Tailscale HTTPS certificate and point `cfg` at it.

    Browsers expose Web Crypto only in a secure context, so without HTTPS the
    phone has no crypto API and payload encryption cannot work at all. A
    Tailscale certificate is a real, publicly-trusted one, so phones accept it
    with no warning and nothing to install.

    `tls=None` means "use it if available"; `tls=True` makes it mandatory.
    """
    if tls is False:
        return

    dns_name = get_tailscale_dns_name(cfg.tailscale_bin)
    if not dns_name:
        message = (
            "Tailscale is not running or CLI not found, so no HTTPS certificate can be issued.\n"
            "  Start it or supply --tailscale-path / --tailscale-bin if installed in a custom path."
        )
        if tls:
            console.print(f"[bold red]--tls requested but unavailable:[/bold red] {message}")
            sys.exit(2)
        console.print(f"[yellow]Serving over plain HTTP.[/yellow] {message}")
        _warn_insecure_context(cfg)
        return

    try:
        cert, key = ensure_tailscale_cert(dns_name, tailscale_bin=cfg.tailscale_bin)
    except TailscaleCertError as e:
        if tls:
            console.print(f"[bold red]Could not obtain a certificate:[/bold red] {e}")
            sys.exit(2)
        console.print(f"[yellow]Serving over plain HTTP.[/yellow] {e}")
        _warn_insecure_context(cfg)
        return

    cfg.tailscale_dns_name = dns_name
    cfg.tls_cert, cfg.tls_key = cert, key
    console.print(f"[green]HTTPS enabled[/green] for [bold]{dns_name}[/bold] (Tailscale certificate)\n")


def _warn_insecure_context(cfg: RemoteConfig) -> None:
    """Explain why E2EE cannot work over plain HTTP on a non-loopback address."""
    if cfg.e2ee_enabled:
        console.print(
            "[yellow]Note:[/yellow] browsers only provide the Web Crypto API over HTTPS or on\n"
            "  localhost, so payload encryption cannot work from a phone over plain HTTP.\n"
            "  Enable HTTPS as above, or set AGY_REMOTE_NO_E2EE=1 to accept cleartext\n"
            "  payloads on a network you trust.\n"
        )


def _guard_or_exit(cfg: RemoteConfig) -> None:
    """Abort with a readable message rather than a traceback on unsafe config."""
    from .config import validate_bind_security

    try:
        validate_bind_security(cfg)
    except InsecureConfigError as e:
        console.print(f"[bold red]Refusing to start:[/bold red] {e}")
        sys.exit(2)


def agy_child_env(cfg: RemoteConfig) -> dict[str, str]:
    """What the supervised agy needs to know about the server supervising it.

    Its PreToolUse hook otherwise resolves the endpoint from a host-wide state
    file, so with two servers running both sessions' approvals would go to
    whichever one published that file. Carries no token: under tmux this ends
    up in argv, which `ps` shows to every local user, and the token is a
    host-wide credential both servers already share.
    """
    return {
        "AGY_REMOTE_URL": cfg.local_base_url,
        "AGY_REMOTE_PORT": str(cfg.port),
    }


def _serve_forever(cfg: RemoteConfig, app: object) -> None:
    """Run the web server in the foreground until interrupted."""
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        ssl_certfile=str(cfg.tls_cert) if cfg.tls_enabled else None,
        ssl_keyfile=str(cfg.tls_key) if cfg.tls_enabled else None,
    )


def _mirror_supervised_screen(app: object, supervisor: object) -> None:
    """Hand the supervisor to the screen mirror once it exists.

    The lifespan attaches whatever `get_pty_supervisor()` returns, but the
    server starts before the supervisor is built, so that lookup found None and
    `mgr.terminal` stayed None for the life of the process. agy draws its
    pickers, its confirmations and its execution mode on the terminal and never
    writes them to the transcript, so the phone was pressing Shift+Tab at a
    screen nobody was mirroring -- cycling default -> accept-edits -> plan with
    no report of where it landed.
    """
    mgr = getattr(getattr(app, "state", None), "session_manager", None)
    if mgr is not None:
        mgr.attach_terminal(supervisor)


def _preflight_port_or_exit(cfg: RemoteConfig) -> None:
    """Check port availability. If busy, prompt interactive users or exit."""
    if cfg.port == 0:
        cfg.port = find_free_port()
        return

    if port_is_free(cfg.host, cfg.port):
        return

    # Only a live server that published *this* port may be named an agy-remote.
    # Any process can hold the port -- a stray `python -m http.server 8765` was
    # reported as an agy-remote and sent the user to a tmux session that did
    # not exist, while the one useful fact (something else owns it) went unsaid.
    owner = runtime_state_owner()
    if owner and owner.get("port") == cfg.port:
        detail = f" (pid {owner['pid']})" if owner.get("pid") else ""
        console.print(
            f"[bold red]Port conflict:[/bold red] port {cfg.port} is already in use{detail}.\n"
            "  An agy-remote is already running on this host.\n"
            f"  • Reach its session:   [bold]tmux attach -t {session_name_for_port(cfg.port)}[/bold]\n"
            f"  • Re-show its QR:      [bold]agy-remote qr[/bold]\n"
        )
    else:
        console.print(
            f"[bold red]Port conflict:[/bold red] port {cfg.port} is already in use.\n"
            "  Another program holds it -- no agy-remote on this host claims it.\n"
            f"  • See what does:       [bold]lsof -nP -iTCP:{cfg.port} -sTCP:LISTEN[/bold]\n"
        )

    try:
        new_port = cfg.port + 1
        while not port_is_free(cfg.host, new_port):
            new_port += 1
        if click.confirm(f"Would you like to start a new instance on port {new_port} instead?", default=True):
            cfg.port = new_port
            console.print(f"[green]Starting new instance on port {cfg.port}...[/green]\n")
            return
    except (click.Abort, Exception):
        pass

    console.print(f"  Or run an independent instance:  [bold]-p {cfg.port + 1}[/bold]\n")
    sys.exit(2)


def _warn_if_second_instance(cfg: RemoteConfig) -> None:
    """Say which server a hand-started agy will send its approvals to.

    The agy this server launches carries `AGY_REMOTE_URL` and so reaches us,
    but an agy started in another terminal has no such parent: it falls back to
    the host-wide state file, which the first server owns.
    """
    owner = runtime_state_owner()
    if owner is None or owner.get("port") == cfg.port:
        return
    console.print(
        f"[bold yellow]Second instance:[/bold yellow] another agy-remote (pid {owner.get('pid')}) is "
        f"serving on port {owner.get('port')} and owns the shared hook endpoint.\n"
        "  The agy this server launches sends its approvals here. An agy you start by hand "
        "sends its approvals there.\n"
    )


def _serve_in_background_or_exit(cfg: RemoteConfig, app: object) -> uvicorn.Server:
    """Start the web server on a daemon thread and prove it came up.

    The port check above races anyone binding in the same millisecond, and it
    says nothing about other bind failures (a privileged port, a bad TLS key).
    A launch whose server thread is already dead must not continue.
    """
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=cfg.host,
            port=cfg.port,
            log_level="error",
            ssl_certfile=str(cfg.tls_cert) if cfg.tls_enabled else None,
            ssl_keyfile=str(cfg.tls_key) if cfg.tls_enabled else None,
        )
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return server
        if not t.is_alive():
            console.print(
                f"[bold red]Refusing to start:[/bold red] the web server died while binding "
                f"{cfg.host}:{cfg.port} (see the error above).\n"
                "  Without it nothing reaches your phone, so agy is not being started.\n"
            )
            sys.exit(2)
        time.sleep(0.05)

    console.print(f"[yellow]Web server slow to start on {cfg.host}:{cfg.port}; continuing.[/yellow]")
    return server


@click.group()
@click.version_option(version=__version__, message="agy-remote %(version)s")
def cli() -> None:
    """Antigravity CLI (agy) Mobile Remote Controller with E2EE & Web Push."""
    pass


@cli.command("serve")
@click.option("--port", "-p", default=8765, help="Port to listen on", show_default=True)
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind on", show_default=True)
@click.option("--token", "-t", default=None, help="Custom auth token")
@click.option("--no-auth", is_flag=True, help="Disable authentication requirement")
@click.option("--no-e2ee", is_flag=True, help="Disable End-to-End Encryption")
@click.option(
    "--tls/--no-tls",
    "tls",
    default=None,
    help="Serve HTTPS using a Tailscale certificate (default: use it if available)",
)
@click.option(
    "--tailscale-path",
    "--tailscale-bin",
    "tailscale_bin",
    default=None,
    help="Custom path to Tailscale CLI executable",
)
@click.option(
    "--brain-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to Antigravity brain directory",
)
@click.option(
    "--rotate-token",
    is_flag=True,
    help="Issue a new token and encryption key, revoking every paired phone",
)
def serve(
    port: int,
    host: str,
    token: str | None,
    no_auth: bool,
    no_e2ee: bool,
    tls: bool | None,
    tailscale_bin: str | None,
    brain_dir: Path | None,
    rotate_token: bool,
) -> None:
    """Start the agy-remote server and watch active sessions."""
    if rotate_token:
        rotate_credentials()
    cfg = get_config(tailscale_bin=tailscale_bin)
    cfg.port = port
    cfg.host = host
    if token:
        cfg.auth_token = token
    if no_auth:
        cfg.enable_auth = False
    if no_e2ee:
        cfg.e2ee_enabled = False
    if brain_dir:
        cfg.brain_dir = brain_dir

    _guard_or_exit(cfg)
    _preflight_port_or_exit(cfg)
    _setup_tls(cfg, tls)
    print_banner(cfg, mode="Watcher Server")
    _warn_if_hooks_unwired()
    _warn_if_second_instance(cfg)

    _serve_forever(cfg, create_app(cfg))


@cli.command("attach")
@click.option(
    "--session",
    "-s",
    "session",
    default=None,
    help="tmux session to adopt (default: the one running agy, if there is exactly one)",
)
@click.option(
    "--wait",
    is_flag=True,
    help="Serve even with no agy running, and start one when a phone connects (for a boot service)",
)
@click.option("--port", "-p", default=8765, help="Port to listen on", show_default=True)
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind on", show_default=True)
@click.option("--token", "-t", default=None, help="Custom auth token")
@click.option("--no-auth", is_flag=True, help="Disable authentication requirement")
@click.option("--no-e2ee", is_flag=True, help="Disable End-to-End Encryption")
@click.option(
    "--tls/--no-tls",
    "tls",
    default=None,
    help="Serve HTTPS using a Tailscale certificate (default: use it if available)",
)
@click.option(
    "--tailscale-path",
    "--tailscale-bin",
    "tailscale_bin",
    default=None,
    help="Custom path to Tailscale CLI executable",
)
@click.option(
    "--brain-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to Antigravity brain directory",
)
@click.option(
    "--rotate-token",
    is_flag=True,
    help="Issue a new token and encryption key, revoking every paired phone",
)
def attach(
    session: str | None,
    wait: bool,
    port: int,
    host: str,
    token: str | None,
    no_auth: bool,
    no_e2ee: bool,
    tls: bool | None,
    tailscale_bin: str | None,
    brain_dir: Path | None,
    rotate_token: bool,
) -> None:
    """Drive an agy already running in tmux, without restarting it.

    `run` owns the agy it starts, and dies with it. This adopts one that is
    already there: your terminal keeps the session, and the phone gets the same
    transcript, screen, keys and approvals. Nothing is started and nothing is
    taken over -- tmux is what makes it possible, since `send-keys` and
    `capture-pane` address a pane by name from any process at all.
    """
    if not is_tmux_available():
        console.print(
            "[bold red]Refusing to start:[/bold red] tmux is not installed or not on PATH.\n"
            "  Adopting a running session needs it: an agy in a plain terminal owns a pty\n"
            "  no other process can write to.\n"
        )
        sys.exit(2)

    session_name = session if wait else _resolve_tmux_session_or_exit(session)
    if wait and not session_name:
        # Nothing named and nothing running: adopt whatever turns up, and if
        # nothing has by the time a phone arrives, start one for it.
        running = sessions_running("agy")
        session_name = running[0] if len(running) == 1 else None

    if rotate_token:
        rotate_credentials()
    cfg = get_config(tailscale_bin=tailscale_bin)
    cfg.port = port
    cfg.host = host
    if session_name:
        cfg.tmux_session = session_name
        # `$TMUX` carries this id into every process in the pane, so the adopted
        # agy's hook can find this server without a subprocess and without
        # caring which server owns the shared state file.
        cfg.tmux_session_id = session_id_of(session_name)
    if token:
        cfg.auth_token = token
    if no_auth:
        cfg.enable_auth = False
    if no_e2ee:
        cfg.e2ee_enabled = False
    if brain_dir:
        cfg.brain_dir = brain_dir

    _guard_or_exit(cfg)
    _preflight_port_or_exit(cfg)
    _setup_tls(cfg, tls)
    mode_label = (
        f"agy (adopted tmux session '{session_name}')"
        if session_name
        else "agy (waiting — a session starts when a phone connects)"
    )
    print_banner(cfg, mode=mode_label)
    _warn_if_hooks_unwired()
    _warn_if_second_instance(cfg)

    app = create_app(cfg)
    mgr = getattr(getattr(app, "state", None), "session_manager", None)
    if session_name:
        _adopt_tmux_session(cfg, mgr, session_name)
    elif mgr is not None:
        mgr.ensure_session = _session_on_demand(cfg, mgr)

    _serve_forever(cfg, app)


def _adopt_tmux_session(cfg: RemoteConfig, mgr: object, session_name: str) -> None:
    """Point this server at a tmux session: typing, screen, and hook routing."""
    cfg.tmux_session = session_name
    cfg.tmux_session_id = session_id_of(session_name)
    set_tmux_supervisor(TmuxSupervisor(session_name=session_name))
    if mgr is not None and hasattr(mgr, "attach_screen"):
        mgr.attach_screen(TmuxScreen(session_name))
    # Re-publish: the registration is what lets this session's PreToolUse hook
    # find *this* server rather than whichever one owns the shared state file.
    publish_server_registration(cfg)


async def _adopt_when_a_phone_arrives(cfg: RemoteConfig, mgr: object) -> None:
    """Give an arriving client something to drive.

    The Mac listens all day with nothing behind it; a session only has to exist
    when someone actually wants one. An agy already running in tmux is adopted
    as-is -- starting a second one would leave the phone talking to the wrong
    half of your desk.
    """
    if cfg.tmux_session:
        return

    running = sessions_running("agy")
    name = running[0] if running else session_name_for_port(cfg.port)
    if not running:
        supervisor = TmuxSupervisor(session_name=name, cmd=["agy"], env=agy_child_env(cfg))
        if not supervisor.start_detached():
            logger.warning("Could not start a tmux session for the arriving client")
            return

    _adopt_tmux_session(cfg, mgr, name)


def _session_on_demand(cfg: RemoteConfig, mgr: object):
    """The callback the manager runs when the first client shows up."""

    async def ensure() -> None:
        await _adopt_when_a_phone_arrives(cfg, mgr)

    return ensure


def _resolve_tmux_session_or_exit(session: str | None) -> str:
    """Which session to adopt, or a readable exit explaining why none.

    Guessing is the one thing not to do here: adopting the wrong session sends
    the phone's prompts into somebody else's agent.
    """
    if session:
        if not TmuxSupervisor(session_name=session).has_session():
            console.print(
                f"[bold red]Refusing to start:[/bold red] no tmux session named '{session}'.\n"
                "  • See what is running:  [bold]tmux list-sessions[/bold]\n"
            )
            sys.exit(2)
        return session

    candidates = sessions_running("agy")
    if not candidates:
        console.print(
            "[bold red]Refusing to start:[/bold red] no tmux session is running agy.\n"
            "  Start one, then attach to it from another terminal:\n"
            "    [bold]tmux new-session -s agy-work agy[/bold]\n"
            "    [bold]agy-remote attach[/bold]\n"
            "  An agy in a plain terminal cannot be adopted: its pty belongs to that\n"
            "  terminal, and nothing else may write to it. `agy-remote run` instead\n"
            "  starts an agy it owns -- `agy-remote run -- --resume <id>` keeps the\n"
            "  conversation you were in.\n"
        )
        sys.exit(2)

    if len(candidates) > 1:
        listed = "\n".join(f"    [bold]{name}[/bold]" for name in candidates)
        console.print(
            "[bold red]Refusing to start:[/bold red] more than one tmux session is running agy:\n"
            f"{listed}\n"
            "  Name the one you mean, so the phone does not drive the wrong agent:\n"
            f"    [bold]agy-remote attach --session {candidates[0]}[/bold]\n"
        )
        sys.exit(2)

    return candidates[0]


@cli.command("run", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.option("--port", "-p", default=8765, help="Port for web server", show_default=True)
@click.option("--host", "-h", default="0.0.0.0", help="Host to bind on", show_default=True)
@click.option("--token", "-t", default=None, help="Custom auth token")
@click.option("--tmux", is_flag=True, help="Run inside a persistent tmux session")
@click.option("--no-auth", is_flag=True, help="Disable authentication")
@click.option("--no-e2ee", is_flag=True, help="Disable End-to-End Encryption")
@click.option(
    "--tls/--no-tls",
    "tls",
    default=None,
    help="Serve HTTPS using a Tailscale certificate (default: use it if available)",
)
@click.option(
    "--tailscale-path",
    "--tailscale-bin",
    "tailscale_bin",
    default=None,
    help="Custom path to Tailscale CLI executable",
)
@click.option(
    "--qr-timeout",
    "--pairing-timeout",
    "qr_timeout",
    default=30,
    type=float,
    help="Seconds to show QR before auto-attaching (0 to attach immediately, default: 30)",
    show_default=True,
)
@click.option(
    "--rotate-token",
    is_flag=True,
    help="Issue a new token and encryption key, revoking every paired phone",
)
@click.pass_context
def run(
    ctx: click.Context,
    port: int,
    host: str,
    token: str | None,
    tmux: bool,
    no_auth: bool,
    no_e2ee: bool,
    tls: bool | None,
    tailscale_bin: str | None,
    qr_timeout: float,
    rotate_token: bool,
) -> None:
    """Launch agy inside a supervisor with simultaneous desktop & mobile control."""
    if rotate_token:
        rotate_credentials()
    cfg = get_config(tailscale_bin=tailscale_bin)
    cfg.port = port
    cfg.host = host
    if token:
        cfg.auth_token = token
    if no_auth:
        cfg.enable_auth = False
    if no_e2ee:
        cfg.e2ee_enabled = False

    _guard_or_exit(cfg)
    _preflight_port_or_exit(cfg)
    _setup_tls(cfg, tls)

    child_cmd = ["agy"] + ctx.args

    print_banner(cfg, mode="agy (tmux)" if tmux else "agy (PTY)")
    _warn_if_hooks_unwired()
    _warn_if_second_instance(cfg)

    # Start FastAPI server in a background thread
    app = create_app(cfg)
    _serve_in_background_or_exit(cfg, app)

    child_env = agy_child_env(cfg)
    if tmux:
        session_name = session_name_for_port(cfg.port)
        supervisor = TmuxSupervisor(session_name=session_name, cmd=child_cmd, env=child_env)
        set_tmux_supervisor(supervisor)
        console.print(f"[dim]Starting persistent tmux session '{session_name}'...[/dim]\n")
        try:
            exit_code = attach_tmux_after_pairing(supervisor, timeout=qr_timeout)
            sys.exit(exit_code)
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        supervisor = PtySupervisor(cmd=child_cmd, env=child_env)
        set_pty_supervisor(supervisor)
        _mirror_supervised_screen(app, supervisor)
        if qr_timeout > 0:
            wait_for_keypress_or_timeout(
                timeout_seconds=qr_timeout,
                message="Scan QR code above, or press any key to attach (auto-attaching in {remaining}s)...",
            )
        console.print(f"[dim]Starting interactive session: {' '.join(child_cmd)}...[/dim]\n")
        try:
            exit_code = supervisor.start_sync()
            sys.exit(exit_code)
        except KeyboardInterrupt:
            sys.exit(0)


def wait_for_keypress_or_timeout(
    timeout_seconds: float = 30,
    message: str = "Scan QR code above, or press any key to attach (auto-attaching in {remaining}s)...",
) -> bool:
    """Wait for a keypress or until timeout expires.

    Returns True if a key was pressed, False if timed out.
    """
    if timeout_seconds <= 0:
        return False

    if not sys.stdin.isatty():
        time.sleep(min(timeout_seconds, 0.1))
        return False

    fd = sys.stdin.fileno()
    try:
        import termios
        import tty
    except ImportError:
        time.sleep(timeout_seconds)
        return False

    try:
        old_settings = termios.tcgetattr(fd)
    except Exception:
        time.sleep(timeout_seconds)
        return False

    try:
        tty.setcbreak(fd)
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            remaining = max(0, int(math.ceil(timeout_seconds - elapsed)))
            if remaining <= 0:
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
                return False

            prompt = message.format(remaining=remaining) if "{remaining}" in message else message
            sys.stderr.write(f"\r\033[K{prompt}")
            sys.stderr.flush()

            timeout_slice = min(1.0, max(0.01, timeout_seconds - elapsed))
            rlist, _, _ = select.select([sys.stdin], [], [], timeout_slice)
            if rlist:
                with contextlib.suppress(Exception):
                    sys.stdin.read(1)
                sys.stderr.write("\r\033[K")
                sys.stderr.flush()
                return True
    except Exception:
        return False
    finally:
        with contextlib.suppress(Exception):
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _warn_if_hooks_unwired() -> None:
    """Remote approvals fail silently when hooks.json is absent or stale."""
    status, detail = hook_health()
    if status == "ok":
        return
    if status == "missing":
        console.print(
            "[bold yellow]Remote approvals are NOT wired on this machine:[/bold yellow] "
            "no PreToolUse hook installed.\n"
            "  Tool permissions will appear in the terminal only, never on the phone.\n"
            "  Fix:  [bold]agy-remote setup-hooks[/bold]\n"
        )
    else:
        console.print(
            f"[bold yellow]Remote approvals are NOT wired:[/bold yellow] the installed hook points at "
            f"[red]{detail}[/red], which does not exist or is not executable\n"
            "  (moved checkout, recreated venv, or config from another machine).\n"
            "  Fix:  [bold]agy-remote setup-hooks[/bold]\n"
        )


def attach_tmux_after_pairing(supervisor: TmuxSupervisor, pause=None, timeout: float = 30) -> int:
    """Hold the QR on screen until acknowledged or timeout expires, then attach.

    `tmux attach-session` replaces the entire terminal with tmux's own screen,
    so the banner and QR printed a moment earlier vanish behind agy before a
    phone can scan them. PTY mode is unaffected -- its output scrolls beneath
    the QR rather than replacing it. `agy-remote qr` re-displays the code at
    any time.
    """
    if pause is not None:
        pause()
    elif timeout > 0:
        wait_for_keypress_or_timeout(timeout_seconds=timeout)

    return supervisor.start_or_attach()


@cli.command("qr")
@click.option(
    "--port",
    "-p",
    default=None,
    type=int,
    help="Pair the instance on this port instead of the one that published the runtime state",
)
def show_qr(port: int | None) -> None:
    """Display connection QR code and active URLs."""
    if port is not None:
        # A second instance does not publish runtime state -- the first one
        # owns that file -- so adopting it would pair the wrong server.
        cfg = get_config()
        cfg.port = port
        _setup_tls(cfg, None)
        print_banner(cfg)
        return

    cfg = adopt_runtime_state(get_config())
    if read_runtime_state() is None:
        console.print(
            "[yellow]No agy-remote server appears to be running.[/yellow] "
            "Showing a preview; start one with [bold]agy-remote run[/bold] "
            "and re-run this command to get a scannable code.\n"
        )
    print_banner(cfg)


@cli.command("push-test")
@click.argument("message", default="Hello from agy-remote!")
def push_test(message: str) -> None:
    """Send a test push notification to all subscribed mobile devices."""
    push_mgr = get_push_manager()
    push_mgr.send_notification(
        title="Antigravity Test Alert",
        body=message,
        data={"type": "test"},
    )
    console.print(
        f"[bold green]✓ Test push notification sent to {len(push_mgr.subscriptions)} subscriber(s)![/bold green]"
    )


@cli.command("setup-hooks")
@click.option(
    "--project",
    is_flag=True,
    help="Install in current project .agents/hooks.json instead of global config",
)
def setup_hooks(project: bool) -> None:
    """Configure Antigravity lifecycle hooks for remote mobile tool approvals."""
    target_dir = Path.cwd() / ".agents" if project else Path.home() / ".gemini" / "config"
    path = install_hooks_config(target_dir)
    console.print(f"[bold green]✓ Hooks successfully configured in:[/bold green] {path}")
    console.print("[dim]Antigravity CLI tool permissions will now be forwarded to your mobile remote![/dim]")


@cli.command("hook-pre-tool", hidden=True)
def hook_pre_tool() -> None:
    """Internal handler invoked by Antigravity CLI PreToolUse hook."""
    run_pre_tool_hook()


def main() -> None:
    """Main CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
