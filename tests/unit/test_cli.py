from __future__ import annotations

import asyncio
import io
import json
import logging
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tokendrain.cli.admin import execute, local_url, parser, platform_settings, read_token
from tokendrain.cli.daemon import make_server, supervise
from tokendrain.config import Settings
from tokendrain.credentials.store import SecretRedactor
from tokendrain.doctor import inspect_database
from tokendrain.logging import JsonFormatter


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    token = tmp_path / "admin-token"
    token.write_text("administrative-test-token-only-123456789")
    token.chmod(0o600)
    return Settings(state_dir=tmp_path, port=8877, admin_token_file=token)


@pytest.mark.parametrize(
    "command, resource", [("status", "system"), ("projects", "projects"), ("runs", "runs")]
)
async def test_cli_authenticated_requests(settings: Settings, command: str, resource: str) -> None:
    def request(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == f"http://127.0.0.1:8877/api/v1/{resource}"
        assert req.headers["Authorization"] == "Bearer administrative-test-token-only-123456789"
        return httpx.Response(200, json={"status": "ready"})

    output = io.StringIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        assert await execute(parser().parse_args([command]), settings, client, output) == 0
    assert json.loads(output.getvalue()) == {"status": "ready"}
    assert "administrative-test" not in output.getvalue()


async def test_cli_login_token_is_explicit_and_offline(settings: Settings) -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("Printing local login token must not contact a server")

    output = io.StringIO()
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
        assert await execute(parser().parse_args(["login-token"]), settings, client, output) == 0
    assert output.getvalue().strip() == "administrative-test-token-only-123456789"


async def test_cli_auth_none_omits_authorization_header(settings: Settings) -> None:
    settings.auth_mode = "none"
    settings.admin_token_file = None

    def request(req: httpx.Request) -> httpx.Response:
        assert "Authorization" not in req.headers
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(request)) as client:
        result = await execute(parser().parse_args(["projects"]), settings, client, io.StringIO())
        assert result == 0


async def test_cli_auth_failure_does_not_echo_response_credentials(settings: Settings) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"secret": "must-never-be-echoed"})
        )
    ) as client:
        with pytest.raises(ValueError, match="rejected the administrative token") as result:
            await execute(parser().parse_args(["status"]), settings, client, io.StringIO())
    assert "must-never-be-echoed" not in str(result.value)


def test_unprotected_administrative_token_rejected(settings: Settings) -> None:
    path = settings.state_dir / "admin-token"
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        read_token(path)


def test_platform_config_environment_overrides_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "platform.json"
    path.write_text(json.dumps({"port": 9000, "state_dir": str(tmp_path)}))
    monkeypatch.setenv("TOKENDRAIN_PORT", "9001")
    settings = platform_settings(path)
    assert settings.port == 9001
    assert settings.state_dir == tmp_path
    assert local_url(Settings(listen_address="::1", port=9001)) == "http://[::1]:9001"
    assert local_url(Settings(listen_address="0.0.0.0", port=8742)) == "http://127.0.0.1:8742"


def test_doctor_database_is_read_only_and_checks_migration(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    assert not inspect_database(path).ok
    assert not path.exists()
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE alembic_version (version_num TEXT)")
        database.execute("INSERT INTO alembic_version VALUES ('first')")
    assert inspect_database(path).ok
    assert "first" in inspect_database(path).message


def test_structured_logs_redact_values_queries_and_exception_text() -> None:
    formatter = JsonFormatter(SecretRedactor(["secret-value"]))
    try:
        raise ValueError("hidden-exception-value")
    except ValueError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "failed secret-value Bearer hidden-token https://user:pass@example.test/callback?code=hidden-code",
            (),
            sys.exc_info(),
        )
    record.project_id = "project-1"
    record.refresh_token = "hidden-extra-value"
    result = formatter.format(record)
    data = json.loads(result)
    assert data["project_id"] == "project-1"
    assert data["exception"]["type"] == "ValueError"
    assert data["exception"]["frames"]
    for secret in (
        "secret-value",
        "hidden-token",
        "hidden-code",
        "user:pass",
        "hidden-exception-value",
        "hidden-extra-value",
    ):
        assert secret not in result


def test_daemon_uses_single_worker_and_disables_access_logs() -> None:
    server = make_server(FastAPI(), Settings(port=9876))
    assert server.config.port == 9876
    assert server.config.workers == 1
    assert server.config.access_log is False
    assert server.config.log_config is None
    assert server.config.timeout_graceful_shutdown == 5


@pytest.mark.parametrize("background_failure", [True, False])
async def test_background_failure_exits_nonzero_after_lifespan_cleanup(
    background_failure: bool,
) -> None:
    app = FastAPI()
    released = asyncio.Event()

    async def background() -> None:
        if background_failure:
            await asyncio.sleep(0.01)
            raise RuntimeError("worker failed")
        await asyncio.Event().wait()

    worker = asyncio.create_task(background())
    app.state.services = SimpleNamespace(task=worker)

    class Server:
        started = False
        should_exit = False

        async def serve(self) -> None:
            self.started = True
            try:
                if not background_failure:
                    await asyncio.sleep(0.02)
                    self.should_exit = True
                while not self.should_exit:  # noqa: ASYNC110 - emulate Uvicorn's public flag
                    await asyncio.sleep(0.001)
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                released.set()

    assert await supervise(Server(), app, 1) == int(background_failure)
    assert released.is_set()
    assert worker.done()


def test_uvicorn_preformatted_traceback_drops_exception_values() -> None:
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        'Traceback (most recent call last):\n  File "server.py", line 12, in callback\n'
        "ValueError: provider-secret-content",
        (),
        None,
    )
    formatted = JsonFormatter().format(record)
    assert "provider-secret-content" not in formatted
    assert json.loads(formatted)["exception"]["frames"][0]["function"] == "callback"
