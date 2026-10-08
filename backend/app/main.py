"""App factory: ingest API + OAuth server + MCP (mounted last at /)."""

import datetime as dt
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import auth, ingest
from app.config import CLAUDE_CALLBACK, Settings, load_settings
from app.db import create_schema, make_engine
from app.mcp_server import build_mcp

log = logging.getLogger("app")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Library loggers can include request details; keep them quiet.
    for name in ("mcp", "sqlalchemy", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


class SecurityAndAccessLog:
    """Pure ASGI middleware: security headers + a values-free access log."""

    def __init__(self, app, hsts: bool):
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status_holder = {"status": 0}
        path = scope.get("path", "")

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if self.hsts:
                    extra.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if path == "/authorize" or path == "/login":
                    origins = " ".join(sorted({f"{urlparse(u).scheme}://{urlparse(u).netloc}" for u in (CLAUDE_CALLBACK,)}))
                    csp = f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {origins}; frame-ancestors 'none'; base-uri 'none'"
                    extra.append((b"content-security-policy", csp.encode()))
                if not path.startswith("/.well-known/") and b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                headers.extend(h for h in extra if h[0] not in present)
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # Path only: no query string, no headers, no bodies.
            log.info(
                "%s %s %d %dms",
                scope.get("method", ""),
                path,
                status_holder["status"],
                int((time.monotonic() - started) * 1000),
            )


def create_app(
    settings: Settings,
    engine: Engine | None = None,
    clock: Callable[[], dt.datetime] | None = None,
) -> FastAPI:
    engine = engine or make_engine(settings.database_url)
    clock = clock or (lambda: dt.datetime.now(dt.UTC))
    mcp = build_mcp(settings, engine, lambda: clock())

    base = urlparse(settings.public_base_url)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[base.netloc, *settings.extra_allowed_hosts],
        allowed_origins=[settings.public_base_url, "https://claude.ai"],
    )
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host="0.0.0.0",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        create_schema(engine)
        # A mounted sub-app's lifespan never runs, so enter the session manager here.
        async with mcp.session_manager.run():
            yield
        engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.engine = engine
    app.state.clock = clock
    app.state.mcp = mcp

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return ingest.validation_error_response(exc.errors())

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "error"
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Class and location only: exception messages (e.g. DB errors) can echo values.
        tb = exc.__traceback__
        while tb and tb.tb_next:
            tb = tb.tb_next
        where = f"{tb.tb_frame.f_code.co_filename.rsplit('/', 1)[-1]}:{tb.tb_lineno}" if tb else "?"
        log.error("unhandled %s at %s on %s", type(exc).__name__, where, request.url.path)
        return JSONResponse({"error": "internal_error"}, status_code=500)

    @app.get("/health")
    async def health():
        return {"ok": True}

    app.include_router(ingest.router)
    app.include_router(auth.router)
    app.mount("/", mcp_app)
    app.add_middleware(SecurityAndAccessLog, hsts=base.scheme == "https")
    return app


def create_app_from_env() -> FastAPI:
    configure_logging()
    return create_app(load_settings())
