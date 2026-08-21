"""Command Line Interface for agy-remote."""

from __future__ import annotations

import io
import sys
import threading
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
    get_config,
    get_tailscale_dns_name,
    is_loopback_host,
    read_runtime_state,
    rotate_credentials,
)
from .hooks import install_hooks_config, run_pre_tool_hook
from .pty_runner import PtySupervisor, set_pty_supervisor
from .push import get_push_manager
from .server import create_app
from .tmux_runner import TmuxSupervisor, set_tmux_supervisor

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


@click.group()
@click.version_option(version="v26.08.22.3", message="agy-remote %(version)s")
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
    _setup_tls(cfg, tls)
    print_banner(cfg, mode="Watcher Server")

    app = create_app(cfg)
    uvicorn.run(
        app,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        ssl_certfile=str(cfg.tls_cert) if cfg.tls_enabled else None,
        ssl_keyfile=str(cfg.tls_key) if cfg.tls_enabled else None,
    )


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
    rotate_token: bool,
) -> None:
    """Launch agy CLI inside supervisor with simultaneous desktop & mobile control."""
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
    _setup_tls(cfg, tls)

    agy_args = ["agy"] + ctx.args
    mode_label = "tmux Persistence" if tmux else "PTY Supervisor"
    print_banner(cfg, mode=mode_label)

    # Start FastAPI server in a background thread
    app = create_app(cfg)
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

    if tmux:
        supervisor = TmuxSupervisor(session_name="agy-remote", cmd=agy_args)
        set_tmux_supervisor(supervisor)
        console.print("[dim]Starting persistent tmux session 'agy-remote'...[/dim]\n")
        exit_code = attach_tmux_after_pairing(supervisor)
        sys.exit(exit_code)
    else:
        supervisor = PtySupervisor(cmd=agy_args)
        set_pty_supervisor(supervisor)
        console.print(f"[dim]Starting interactive session: {' '.join(agy_args)}...[/dim]\n")
        try:
            exit_code = supervisor.start_sync()
            sys.exit(exit_code)
        except KeyboardInterrupt:
            sys.exit(0)


def attach_tmux_after_pairing(supervisor: TmuxSupervisor, pause=None) -> int:
    """Hold the QR on screen until acknowledged, then attach.

    `tmux attach-session` replaces the entire terminal with tmux's own screen,
    so the banner and QR printed a moment earlier vanish behind agy before a
    phone can scan them. PTY mode is unaffected -- its output scrolls beneath
    the QR rather than replacing it. `agy-remote qr` re-displays the code at
    any time.
    """
    if pause is None:

        def pause() -> None:
            click.pause("Scan the QR code above, then press any key to attach (detach later with Ctrl+B D)...")

    pause()
    return supervisor.start_or_attach()


@cli.command("qr")
def show_qr() -> None:
    """Display connection QR code and active URLs."""
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
