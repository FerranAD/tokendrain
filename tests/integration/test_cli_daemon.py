# ruff: noqa: ASYNC240
"""Exercise installed entrypoints, HTTP authentication and SIGTERM ownership."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path

import httpx


async def test_daemon_and_admin_client_stop_cleanly(tmp_path: Path) -> None:
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("TOKENDRAIN_")
    }
    environment.update(
        TOKENDRAIN_BACKEND="mock",
        TOKENDRAIN_STATE_DIR=str(tmp_path),
        TOKENDRAIN_PORT=str(port),
        TOKENDRAIN_PUBLIC_URL=url,
    )
    log_path = tmp_path / "daemon.log"
    with log_path.open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tokendrain.cli.daemon",
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with httpx.AsyncClient(base_url=url, trust_env=False, timeout=1) as client:
                async with asyncio.timeout(15):
                    while True:
                        assert process.returncode is None, log_path.read_text()
                        try:
                            response = await client.get("/healthz?code=unlogged-authorization-code")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(0.05)
                assert (await client.get("/api/v1/projects")).status_code == 401
                token = (tmp_path / "admin-token").read_text().strip()
                response = await client.post(
                    "/api/v1/projects",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Origin": url,
                        "X-Tokendrain-Request": "1",
                    },
                    json={"name": "CLI integration", "description": "Persistent test project"},
                )
                assert response.status_code == 201, response.text
            command = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tokendrain.cli.admin",
                "--state-dir",
                str(tmp_path),
                "--url",
                url,
                "--json",
                "projects",
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(command.communicate(), 10)
            assert command.returncode == 0, stderr.decode()
            projects = json.loads(stdout)
            assert projects[0]["name"] == "CLI integration"
            process.terminate()
            await asyncio.wait_for(process.wait(), 15)
            assert process.returncode in (0, -signal.SIGTERM), log_path.read_text()
            logs = log_path.read_text()
            assert "Application shutdown complete" in logs
            assert "unlogged-authorization-code" not in logs
            assert token not in logs
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
