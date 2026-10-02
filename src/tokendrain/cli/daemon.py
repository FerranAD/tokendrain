"""Run the single-process Tokendrain HTTP and execution supervisor."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol, cast

import uvicorn
from fastapi import FastAPI
from pydantic import ValidationError

from tokendrain.config import Settings
from tokendrain.logging import configure_logging

log = logging.getLogger(__name__)


class ManagedServer(Protocol):
    started: bool
    should_exit: bool

    async def serve(self) -> None: ...


class ServiceOwner(Protocol):
    task: asyncio.Task[None] | None


def make_server(app: FastAPI, settings: Settings) -> uvicorn.Server:
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.listen_address,
            port=settings.port,
            workers=1,
            log_config=None,
            access_log=False,  # OAuth callback URLs contain authorization codes.
            # SSE streams otherwise hold HTTP draining open indefinitely. The
            # separate application lifespan retains its full VM shutdown budget.
            timeout_graceful_shutdown=5,
        )
    )


async def supervise(server: ManagedServer, app: FastAPI, grace_seconds: float) -> int:
    """Uvicorn owns signals; application lifespan owns background task cleanup."""
    server_task = asyncio.create_task(server.serve(), name="http-server")
    failed = False
    try:
        async with asyncio.timeout(90):
            while not server.started:
                if server_task.done():
                    await server_task
                    return 1  # Lifespan/startup failed before accepting requests.
                await asyncio.wait({server_task}, timeout=0.05)
        background = cast(ServiceOwner, app.state.services).task
        if background:
            await asyncio.wait({server_task, background}, return_when=asyncio.FIRST_COMPLETED)
            # Normal signal handling sets should_exit before lifespan cancels its
            # services. Unexpected termination must fail the process for systemd.
            if background.done() and not server.should_exit:
                failed = True
                log.error("daemon.background_stopped_requesting_restart")
                server.should_exit = True
        await asyncio.wait_for(asyncio.shield(server_task), grace_seconds)
    except TimeoutError:
        failed = True
        log.error("daemon.server_shutdown_timeout")
        server_task.cancel()
        await asyncio.gather(server_task, return_exceptions=True)
    except Exception:
        failed = True
        log.exception("daemon.server_failed")
    finally:
        server.should_exit = True
        if not server_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(server_task), grace_seconds)
            except (TimeoutError, asyncio.CancelledError):
                failed = True
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
    return 1 if failed else 0


async def serve(settings: Settings) -> int:
    from tokendrain.api.app import create_app

    app = create_app(settings)
    return await supervise(make_server(app, settings), app, settings.shutdown_timeout_seconds + 15)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-level", choices=("debug", "info", "warning", "error"), default="info"
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValidationError:
        parser.error("Invalid TOKENDRAIN_* configuration; check configured types and bounds")
    configure_logging(args.log_level)
    raise SystemExit(asyncio.run(serve(settings)))


if __name__ == "__main__":
    main()
