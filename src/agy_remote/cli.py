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

from .config import RemoteConfig, get_config
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
        table.add_row("E2EE Status:", "[bold green]Active (AES-256-GCM via URL Hash)[/]")
    table.add_row("Brain Dir:", f"[dim]{cfg.brain_dir}[/]")

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


@click.group()
@click.version_option(version="v26.08.21.5", message="agy-remote %(version)s")
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
    "--brain-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to Antigravity brain directory",
)
def serve(
    port: int,
    host: str,
    token: str | None,
    no_auth: bool,
    no_e2ee: bool,
    brain_dir: Path | None,
) -> None:
    """Start the agy-remote server and watch active sessions."""
    cfg = get_config()
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

    print_banner(cfg, mode="Watcher Server")

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


@cli.command("run", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.option("--port", "-p", default=8765, help="Port for web server", show_default=True)
@click.option("--token", "-t", default=None, help="Custom auth token")
@click.option("--tmux", is_flag=True, help="Run inside a persistent tmux session")
@click.option("--no-auth", is_flag=True, help="Disable authentication")
@click.option("--no-e2ee", is_flag=True, help="Disable End-to-End Encryption")
@click.pass_context
def run(ctx: click.Context, port: int, token: str | None, tmux: bool, no_auth: bool, no_e2ee: bool) -> None:
    """Launch agy CLI inside supervisor with simultaneous desktop & mobile control."""
    cfg = get_config()
    cfg.port = port
    if token:
        cfg.auth_token = token
    if no_auth:
        cfg.enable_auth = False
    if no_e2ee:
        cfg.e2ee_enabled = False

    agy_args = ["agy"] + ctx.args
    mode_label = "tmux Persistence" if tmux else "PTY Supervisor"
    print_banner(cfg, mode=mode_label)

    # Start FastAPI server in a background thread
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()

    if tmux:
        supervisor = TmuxSupervisor(session_name="agy-remote", cmd=agy_args)
        set_tmux_supervisor(supervisor)
        console.print("[dim]Starting persistent tmux session 'agy-remote'...[/dim]\n")
        exit_code = supervisor.start_or_attach()
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


@cli.command("qr")
def show_qr() -> None:
    """Display connection QR code and active URLs."""
    cfg = get_config()
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
