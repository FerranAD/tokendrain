"""Run the single-process Tokendrain HTTP and execution supervisor."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn
from pydantic import ValidationError

from tokendrain.config import Settings
from tokendrain.logging import configure_logging


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
    # Uvicorn owns SIGTERM/SIGINT and invokes the API lifespan shutdown. Never
    # fork workers: one daemon owns SQLite scheduling and project VM leases.
    uvicorn.run(
        "tokendrain.api.app:create_app",
        factory=True,
        host=settings.listen_address,
        port=settings.port,
        workers=1,
        log_config=None,
        access_log=False,  # OAuth callback URLs contain authorization codes.
        timeout_graceful_shutdown=settings.shutdown_timeout_seconds + 10,
    )


if __name__ == "__main__":
    main()
