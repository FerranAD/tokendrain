"""Same-origin authenticated administrative API and compiled React application."""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

from tokendrain.application import Application, Overrides
from tokendrain.config import Settings

log = logging.getLogger(__name__)


def current(request: Request) -> Application:
    return cast(Application, request.app.state.services)


def encoded(value: Any) -> Any:
    return jsonable_encoder(
        value,
        custom_encoder={
            datetime: lambda dt: (
                dt.replace(tzinfo=UTC).isoformat() if dt.tzinfo is None else dt.isoformat()
            )
        },
    )


def create_app(settings: Settings | None = None, overrides: Overrides | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        services = await Application.open(settings, overrides)
        app.state.services = services
        try:
            yield
        finally:
            await services.close()

    app = FastAPI(
        title="tokendrain",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    public = urlsplit(settings.public_url)
    allowed_hosts = {
        public.netloc.lower(),
        f"127.0.0.1:{settings.port}",
        f"localhost:{settings.port}",
    }
    allowed_origins = {
        f"{public.scheme}://{public.netloc}",
        f"http://127.0.0.1:{settings.port}",
        f"http://localhost:{settings.port}",
    }

    @app.middleware("http")
    async def boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.headers.get("host", "").lower() not in allowed_hosts:
            return JSONResponse({"detail": "Unexpected Host header"}, status_code=400)
        path = request.url.path
        if path.startswith("/api/"):
            services = current(request)
            if services.task and services.task.done():
                return JSONResponse(
                    {"detail": "Background service failed; restart and inspect logs"},
                    status_code=503,
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                origin = request.headers.get("origin")
                if request.headers.get("x-tokendrain-request") != "1" or (
                    origin is not None and origin not in allowed_origins
                ):
                    return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
                # Bound uploads before parsing; callers cannot smuggle unbounded chunked bodies.
                chunks: list[bytes] = []
                size = 0
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 2 * 1024 * 1024:
                        return JSONResponse({"detail": "Request exceeds 2 MiB"}, status_code=413)
                    chunks.append(chunk)
                # Starlette CachedRequest forwards this body to the downstream parser.
                request._body = b"".join(chunks)
            public_callback = (
                path == "/api/v1/integrations/github/setup" and request.method == "GET"
            )
            if (
                settings.auth_mode == "token"
                and not public_callback
                and not (path == "/api/v1/session" and request.method == "POST")
            ):
                authorization = request.headers.get("authorization", "")
                bearer = authorization[7:] if authorization.startswith("Bearer ") else ""
                cookie = request.cookies.get("tokendrain_session", "")
                if not (
                    (bearer and services.tokens.authenticate(bearer))
                    or (cookie and services.tokens.valid(cookie))
                ):
                    return JSONResponse(
                        {"detail": "Sign in with the administrative token"}, status_code=401
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
        )
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(LookupError)
    async def missing(_: Request, error: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(_: Request, error: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def bad_input(_: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic's default response echoes inputs, including submitted secret values.
        return JSONResponse(
            {
                "detail": [
                    {"loc": issue["loc"], "msg": issue["msg"], "type": issue["type"]}
                    for issue in error.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(httpx.HTTPError)
    async def upstream(_: Request, error: httpx.HTTPError) -> JSONResponse:
        status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
        return JSONResponse(
            {
                "detail": f"External service request failed ({status or 'connection'}). "
                "Check connectivity, configured credentials and permissions."
            },
            status_code=502,
        )

    @app.get("/healthz")
    async def health(request: Request) -> JSONResponse:
        services = current(request)
        healthy = not (services.task and services.task.done())
        return JSONResponse(
            {"status": "ok" if healthy else "failed", "ready": services.supervisor.ready},
            status_code=200 if healthy else 503,
        )

    from tokendrain.api.routes import router

    app.include_router(router)
    web_dir = settings.web_dir
    if web_dir is None:
        candidate = Path(__file__).resolve().parents[3] / "web" / "dist"
        if candidate.is_dir():
            web_dir = candidate
    if web_dir and (web_dir / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="assets")
        index = web_dir / "index.html"

        @app.get("/{path:path}")
        async def frontend(path: str) -> Response:
            if path.startswith(("api/", "auth/")):
                return JSONResponse({"detail": "Not found"}, status_code=404)
            assert web_dir is not None
            file = (web_dir / path).resolve()
            if file.is_relative_to(web_dir.resolve()) and file.is_file():
                return FileResponse(file)
            return FileResponse(index)

    return app
