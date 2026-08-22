"""FastAPI server providing REST APIs, WebSockets, E2EE, Web Push, and PWA static assets."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .config import (
    RemoteConfig,
    clear_runtime_state,
    get_config,
    is_loopback_host,
    publish_server_registration,
    validate_bind_security,
    withdraw_server_registration,
    write_runtime_state,
)
from .crypto import EnvelopeError, decode_key, decrypt_payload
from .keys import is_known_key
from .models import (
    ApprovalResponseRequest,
    ConversationSummary,
    KeyPressRequest,
    UserPromptRequest,
)
from .pty_runner import get_pty_supervisor
from .push import get_push_manager
from .session_manager import SessionManager
from .tmux_runner import get_tmux_supervisor
from .version import VERSION

logger = logging.getLogger("agy_remote.server")
STATIC_DIR = Path(__file__).parent / "static"

api_key_header = APIKeyHeader(name="X-Auth-Token", auto_error=False)


#: Extensions accepted by /api/upload, mapped to their magic-byte signatures.
#: SVG is deliberately absent: it is an active-content format that can carry
#: script, and these files land in the workspace the agent operates on.
IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


def looks_like_image(ext: str, content: bytes) -> bool:
    """Verify the bytes actually match the claimed image extension.

    An attacker-supplied extension proves nothing; sniffing the magic bytes
    stops a script or binary being dropped into the workspace as `evil.png`.
    """
    signatures = IMAGE_SIGNATURES.get(ext)
    if not signatures:
        return False
    if not any(content.startswith(sig) for sig in signatures):
        return False
    if ext == ".webp":
        return len(content) >= 12 and content[8:12] == b"WEBP"
    return True


def create_app(config: RemoteConfig | None = None) -> FastAPI:
    """Factory creating configured FastAPI app."""
    cfg = config or get_config()
    validate_bind_security(cfg)
    session_mgr = SessionManager(cfg)
    push_mgr = get_push_manager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Start/stop the session manager and publish helper credentials."""
        await session_mgr.start()
        # A supervisor that already exists (a re-attach, or a caller that built
        # one first) is mirrored from here. `agy-remote run` builds its
        # supervisor *after* starting the server, so it hands it over itself --
        # this lookup found None and the screen went unmirrored for the life of
        # the process.
        supervisor = get_pty_supervisor()
        if supervisor is not None:
            session_mgr.attach_terminal(supervisor)
        # Let the PreToolUse hook (a separate process) find our token and port.
        write_runtime_state(cfg)
        # And let a hook inside the tmux session we adopted find *us*, rather
        # than whichever server happens to own the shared state file.
        publish_server_registration(cfg)
        owner_pid = os.getpid()
        try:
            yield
        finally:
            clear_runtime_state(owner_pid=owner_pid)
            withdraw_server_registration(cfg.port, owner_pid=owner_pid)
            await session_mgr.stop()

    app = FastAPI(
        title="Antigravity Remote",
        description="Mobile Remote Web PWA with E2EE & Web Push for Antigravity CLI",
        version=VERSION,
        lifespan=lifespan,
    )
    app.state.session_manager = session_mgr
    app.state.config = cfg

    #: Sent on every response as a second line of defence behind the <meta> CSP
    #: in index.html. No third-party origins are permitted: this page holds the
    #: E2EE key and auth token, so any remote script is a credential thief.
    SECURITY_HEADERS = {
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; "
            "connect-src 'self' ws: wss:; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            "frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        # Keeps the ?token= query string out of any outbound Referer.
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cache-Control": "no-store",
    }

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Attach hardening headers to every response."""
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    @app.middleware("http")
    async def guard_host_header(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Block DNS-rebinding when the token gate is switched off.

        With auth disabled the bind is loopback-only (see validate_bind_security),
        so a request arriving under any other hostname is a rebound attacker
        domain resolving to 127.0.0.1, not a legitimate client.
        """
        if not cfg.enable_auth:
            host = (request.headers.get("host") or "").rsplit(":", 1)[0]
            if not is_loopback_host(host):
                return JSONResponse(
                    status_code=421,
                    content={"detail": "Unrecognized Host header"},
                )
        return await call_next(request)

    def get_mgr(req: Request) -> SessionManager:
        return getattr(req.app.state, "session_manager", session_mgr)

    def token_ok(provided: str | None) -> bool:
        """Constant-time token check, used by every authenticated entry point.

        The pairing deadline is enforced here, per check, not at startup: a
        boot-time verdict alone would let a long-running server honor an
        expired pairing until its next restart, which is exactly the window
        the TTL exists to close. (A WebSocket authenticated before the
        deadline keeps its connection; new connections are refused.)
        """
        if cfg.pairing_expired():
            logger.info("Refusing expired pairing; restart agy-remote to mint a new QR")
            return False

        return secrets.compare_digest(
            (provided or "").encode("utf-8"),
            cfg.auth_token.encode("utf-8"),
        )

    def verify_auth(
        request: Request,
        token_query: str | None = Query(None, alias="token"),
        token_header: str | None = Security(api_key_header),
    ) -> bool:
        if not cfg.enable_auth:
            return True

        if not token_ok(token_header or token_query):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing authentication token",
            )
        return True

    # -------------------------------------------------------------------------
    # REST Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/status")
    async def get_status(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Return server status, encryption info, and connection links."""
        if cfg.enable_auth and not token_ok(token_header or token):
            # Disclose nothing beyond the fact that a token is needed: version
            # and feature flags are useful reconnaissance for an unauthenticated
            # caller and are not needed to render the login state.
            return {"auth_required": True, "authenticated": False}

        mgr = get_mgr(request)
        pty = get_pty_supervisor()
        tmux = get_tmux_supervisor()
        return {
            "auth_required": cfg.enable_auth,
            "authenticated": True,
            "version": f"v{VERSION}",
            "e2ee_enabled": cfg.e2ee_enabled,
            # Which engine is behind this server. The PWA renders whatever a
            # backend normalizes into one step shape, so without this it can
            # only guess at a name -- and a wrong name makes a session look
            # like something it is not.
            "agent": cfg.agent,
            "active_conversation_id": mgr.active_conversation_id,
            "supervisor_running": (pty is not None and pty.running) or (tmux is not None and tmux.has_session()),
            "primary_mobile_url": cfg.get_primary_mobile_url(),
            "connect_urls": cfg.get_connect_urls(),
            "connected_clients": len(mgr._connected_clients),
        }

    @app.get("/api/conversations")
    async def list_conversations(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> list[ConversationSummary]:
        """List all discovered Antigravity conversations."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return mgr.list_conversations()

    @app.get("/api/conversations/{conversation_id}")
    async def get_conversation(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Get details and step history of a specific conversation."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        if mgr.active_conversation_id == conversation_id:
            steps = [s.model_dump() for s in mgr.active_steps]
            result: dict[str, Any] = {
                "id": conversation_id,
                "steps": steps,
                "pending_approvals": mgr.get_active_pending_approvals(),
            }
        else:
            result = await mgr.backend.load_conversation(mgr, conversation_id)
            if result is None:
                raise HTTPException(status_code=404, detail="Conversation not found")

        return result

    @app.post("/api/conversations/{conversation_id}/switch")
    async def switch_conversation_endpoint(
        conversation_id: str,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Switch active conversation."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        success = await mgr.switch_conversation(conversation_id, pin=True)
        if not success:
            raise HTTPException(status_code=404, detail="Could not switch conversation")
        return {"status": "ok", "active_conversation_id": conversation_id}

    @app.get("/api/screen")
    async def get_screen(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """The supervised terminal as plain text, or null in watcher mode."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        return {"terminal": mgr.terminal.snapshot() if mgr.terminal else None}

    def _press_key(key: str) -> str:
        """Deliver a key to whichever supervisor is live, if any."""
        tmux = get_tmux_supervisor()
        if tmux and tmux.has_session():
            return "ok" if tmux.send_key(key) else "refused"

        pty = get_pty_supervisor()
        if pty and pty.running:
            return "ok" if pty.send_key(key) else "refused"

        return "no_session"

    @app.post("/api/key")
    async def send_key(
        req: KeyPressRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Press a named key -- Shift+Tab, Esc, an arrow -- in the live session.

        agy's execution mode, its panels and its selection lists are reachable
        only by keystroke; a prompt line cannot express any of them.
        """
        verify_auth(request, token, token_header)
        return {"status": _press_key(req.key)}

    @app.post("/api/prompt")
    async def send_prompt(
        req: UserPromptRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Send user prompt from mobile UI into active session."""
        verify_auth(request, token, token_header)

        mgr = get_mgr(request)
        delivered_via = await mgr.backend.send_prompt(mgr, req.prompt, req.conversation_id)

        await mgr.broadcast(
            {
                "event": "prompt_sent",
                "data": {
                    "prompt": req.prompt,
                    "conversation_id": req.conversation_id or mgr.active_conversation_id,
                    "delivered_via": delivered_via,
                },
            }
        )
        if delivered_via == "broadcast":
            return {
                "status": "ok",
                "delivered_via": "broadcast",
                "message": "Prompt broadcasted. To enable direct CLI typing, launch with 'agy-remote run'.",
            }
        return {"status": "ok", "delivered_via": delivered_via}

    @app.post("/api/upload")
    async def upload_file(
        request: Request,
        file: UploadFile = File(...),
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Upload image/screenshot from mobile camera or gallery into workspace with strict sanitization."""
        verify_auth(request, token, token_header)
        upload_dir = (Path.cwd() / ".agents" / "uploads").resolve()
        upload_dir.mkdir(parents=True, exist_ok=True)

        raw_filename = Path(file.filename or "image.jpg").name
        ext = Path(raw_filename).suffix.lower()
        if ext not in IMAGE_SIGNATURES:
            raise HTTPException(status_code=400, detail="Invalid file type. Only image uploads are allowed.")

        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 25MB).")

        if not looks_like_image(ext, content):
            raise HTTPException(status_code=400, detail="File content does not match its image extension.")

        filename = f"mobile_{uuid.uuid4().hex[:8]}_{raw_filename}"
        dest = (upload_dir / filename).resolve()
        if not dest.is_relative_to(upload_dir):
            raise HTTPException(status_code=400, detail="Invalid target filename.")

        with open(dest, "wb") as f:
            f.write(content)
        dest.chmod(0o600)

        return {
            "status": "ok",
            "filename": filename,
            "relative_path": f".agents/uploads/{filename}",
            "absolute_path": str(dest),
        }

    @app.post("/api/approvals/{approval_id}/respond")
    async def respond_approval(
        approval_id: str,
        req: ApprovalResponseRequest,
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, Any]:
        """Approve or deny a pending tool call from mobile UI."""
        verify_auth(request, token, token_header)
        mgr = get_mgr(request)
        resolved = await mgr.resolve_approval(approval_id, req)
        if not resolved:
            raise HTTPException(status_code=404, detail="Pending approval not found or expired")
        return {"status": "ok", "decision": req.decision}

    @app.post("/api/hook/pre-tool")
    async def hook_pre_tool(
        request: Request,
        token_header: str | None = Security(api_key_header),
    ) -> JSONResponse:
        """Endpoint called by agy CLI PreToolUse hook."""
        if cfg.enable_auth and not token_ok(token_header):
            raise HTTPException(status_code=401, detail="Unauthorized hook call")

        mgr = get_mgr(request)
        if mgr.backend.name != "agy":
            # Only agy speaks this hook protocol. A stale hook firing at a
            # server fronting something else must not mint phantom approvals.
            raise HTTPException(status_code=400, detail="This server is not fronting an agy session")

        payload = await request.json()
        tool_call = payload.get("toolCall", {})
        tool_name = tool_call.get("name", "unknown_tool")
        args = tool_call.get("args", {})
        conversation_id = payload.get("conversationId", "default")
        approval_id = str(uuid.uuid4())

        # Only buzz a phone about a decision the phone is actually going to be
        # asked for. Otherwise agy answers it in its own terminal, and a push
        # would be an alert about something already settled.
        if mgr.can_hold_approval(conversation_id):
            push_mgr.send_notification(
                title=f"Permission Required: {tool_name}",
                body=f"{tool_name}: {args.get('CommandLine') or args.get('TargetFile') or 'Action requested'}",
                data={"approval_id": approval_id, "type": "approval_request"},
            )

        decision_payload = await mgr.request_approval(
            approval_id=approval_id,
            conversation_id=conversation_id,
            tool_name=tool_name,
            args=args,
        )
        return JSONResponse(content=decision_payload)

    # -------------------------------------------------------------------------
    # Web Push Notification Endpoints
    # -------------------------------------------------------------------------

    @app.get("/api/push/vapid-public-key")
    async def get_vapid_key() -> dict[str, str]:
        """Get public VAPID key for browser push subscription."""
        return {"public_key": push_mgr.public_key}

    @app.post("/api/push/subscribe")
    async def subscribe_push(
        request: Request,
        token: str | None = Query(None),
        token_header: str | None = Security(api_key_header),
    ) -> dict[str, str]:
        """Register a browser push subscription."""
        verify_auth(request, token, token_header)
        sub_data = await request.json()
        push_mgr.add_subscription(sub_data)
        return {"status": "subscribed"}

    # -------------------------------------------------------------------------
    # WebSocket Endpoint with E2EE
    # -------------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(
        websocket: WebSocket,
        token: str | None = Query(None),
    ) -> None:
        """Bidirectional WebSocket for live updates with E2EE envelope support."""
        if cfg.enable_auth and not token_ok(token):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()
        mgr = session_mgr
        await mgr.register_client(websocket)

        raw_key_bytes = decode_key(cfg.e2ee_key) if cfg.e2ee_enabled else None

        try:
            while True:
                raw_msg = await websocket.receive_json()

                async def reject(reason: str) -> None:
                    """Say so, rather than dropping the frame in silence.

                    A prompt the server threw away used to look exactly like
                    one it accepted: the phone sent into an open socket, got
                    nothing back and cleared the input. The reply is sealed
                    like any other, so it tells an attacker nothing they could
                    not already see from the connection staying open.
                    """
                    logger.warning("Rejected WS frame: %s", reason)
                    await mgr.send_to(websocket, {"event": "frame_rejected", "data": {"reason": reason}})

                if raw_key_bytes is not None:
                    # E2EE is on, so an unsealed frame is never legitimate:
                    # accepting one would let anyone holding only the token
                    # downgrade out of encryption and drive the agent.
                    if not raw_msg.get("encrypted"):
                        await reject("encrypted frame required while E2EE is enabled")
                        continue
                    try:
                        msg = decrypt_payload(raw_msg, raw_key_bytes, guard=mgr.replay_guard)
                    except EnvelopeError as e:
                        await reject(str(e))
                        continue
                    except Exception as e:
                        await reject(f"could not decrypt: {e}")
                        continue
                else:
                    msg = raw_msg

                if not isinstance(msg, dict):
                    continue

                action = msg.get("action")
                data = msg.get("data", {})

                if action == "ping":
                    await mgr.send_to(websocket, {"event": "pong"})
                elif action == "send_prompt":
                    prompt_text = data.get("prompt", "")
                    if prompt_text:
                        # Say how it went out. "broadcast" means no supervisor
                        # took it -- the prompt was typed nowhere, and a client
                        # that hears only `prompt_sent` cannot tell that apart
                        # from one that landed.
                        delivered_via = await mgr.backend.send_prompt(mgr, prompt_text, data.get("conversation_id"))
                        await mgr.broadcast(
                            {
                                "event": "prompt_sent",
                                "data": {"prompt": prompt_text, "delivered_via": delivered_via},
                            }
                        )
                elif action == "request_screen":
                    # A client revealing the panel wants the screen now, not at
                    # the next redraw -- a still terminal never sends one.
                    if mgr.terminal is not None:
                        await mgr.send_to(websocket, {"event": "terminal_screen", "data": mgr.terminal.snapshot()})
                elif action == "send_key":
                    key = data.get("key")
                    if isinstance(key, str) and is_known_key(key):
                        _press_key(key)
                    else:
                        logger.warning("Refused unknown key press: %r", key)
                elif action == "approve_tool":
                    approval_id = data.get("approval_id")
                    decision = data.get("decision", "deny")
                    reason = data.get("reason")
                    if approval_id:
                        try:
                            response = ApprovalResponseRequest(decision=decision, reason=reason)
                        except ValidationError as e:
                            logger.warning("Rejected malformed approval response: %s", e)
                            continue
                        await mgr.resolve_approval(approval_id, response)
                elif action == "switch_conversation":
                    target_id = data.get("conversation_id")
                    # Only switch to an id that resolves to a real
                    # conversation, so a crafted id cannot leave the manager
                    # pointing at nothing.
                    if isinstance(target_id, str) and mgr.backend.is_known_conversation(target_id):
                        await mgr.switch_conversation(target_id, pin=True)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.debug("WebSocket loop terminated: %s", e)
        finally:
            mgr.unregister_client(websocket)
            # The count is the alarm, so a device leaving has to clear it.
            with contextlib.suppress(Exception):
                await mgr.announce_peers()

    # -------------------------------------------------------------------------
    # PWA Static Files & Fallback
    # -------------------------------------------------------------------------

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/manifest.json")
        async def manifest(token: str | None = Query(None)) -> Response:
            """The PWA manifest, carrying credentials only to a paired caller.

            An installed iOS web app gets its own storage container -- nothing
            the Safari tab saved comes with it -- and launches at `start_url`.
            Fixed at "/", the home-screen icon opened to "no encryption key in
            this link", so the install that was meant to end the QR code
            required one. Putting the credentials in `start_url` fixes that,
            and means this response hands out secrets: it is authenticated like
            every other one, and an anonymous fetch still gets a usable
            manifest with a bare start_url.
            """
            with open(STATIC_DIR / "manifest.json", encoding="utf-8") as f:
                data = json.load(f)

            if not cfg.enable_auth or token_ok(token):
                start = f"/?token={quote(cfg.auth_token)}"
                if cfg.e2ee_enabled:
                    start += f"#key={cfg.e2ee_key}"
                data["start_url"] = start

            return Response(content=json.dumps(data), media_type="application/manifest+json")

        @app.get("/sw.js")
        async def service_worker() -> FileResponse:
            return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    return app


def __getattr__(name: str) -> Any:
    """Build the ASGI app lazily on first access.

    `uvicorn agy_remote.server:app` still works, but merely importing this
    module no longer constructs a server. Eager construction meant an unsafe
    configuration blew up as an import traceback before the CLI could print a
    readable message, and every import touched the brain dir and VAPID keys.
    """
    if name == "app":
        global _app_singleton
        try:
            return _app_singleton
        except NameError:
            _app_singleton = create_app()
            return _app_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
