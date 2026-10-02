from __future__ import annotations

import asyncio
import json
import os
import pwd
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tokendrain.doctor import DoctorCheck
from tokendrain.orchestration.mock import MockVmBackend
from tokendrain.vm.commands import CommandResult
from tokendrain.vm.firecracker import FirecrackerBackend
from tokendrain.vm.helper import HelperConfig, InfrastructureService, Request


class NoOperationsRunner:
    async def run(
        self,
        *argv: str,
        input: bytes | None = None,
        timeout: float = 120,  # noqa: ASYNC109
        check: bool = True,
    ) -> CommandResult:
        pytest.fail(f"Read-only diagnostics invoked infrastructure operation: {argv}")


@asynccontextmanager
async def unix_server(
    path: Path,
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncIterator[None]:
    connections: set[asyncio.Task[None]] = set()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(handler(reader, writer))
        connections.add(task)

    server = await asyncio.start_unix_server(accept, str(path), limit=65536)
    async with server:
        try:
            yield
        finally:
            for task in connections:
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)


@pytest.mark.parametrize("healthy", [True, False])
async def test_authenticated_diagnostics_use_helper_context_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, healthy: bool
) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    config = HelperConfig(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        socket_path=tmp_path / "helper.sock",
        guest_artifacts=tmp_path / "guest",
        firecracker="/configured/firecracker",
        daemon_user=user,
        vm_user=user,
    )
    checked: list[tuple[Path, str, str]] = []

    def inspect(artifacts: Path, firecracker: str, *, scope: str) -> list[DoctorCheck]:
        checked.append((artifacts, firecracker, scope))
        return [
            DoctorCheck(
                name="kvm",
                ok=healthy,
                message="KVM API 12" if healthy else "Permission denied",
                scope="helper",
            ),
            DoctorCheck(name="firecracker", ok=True, message=firecracker, scope="helper"),
        ]

    monkeypatch.setattr("tokendrain.vm.helper.inspect_vm_checks", inspect)
    service = InfrastructureService(config, runner=NoOperationsRunner())
    # Diagnostics must remain responsive while another connection starts/stops
    # a VM, and must never enumerate or reconcile stale resource records.
    async with service._lock, unix_server(config.socket_path, service.connection):
        checks = await asyncio.wait_for(FirecrackerBackend(config.socket_path).diagnostics(), 1)
    assert checked == [(config.guest_artifacts, config.firecracker, "helper")]
    assert checks[0].ok is healthy
    assert checks[1].message == config.firecracker
    assert all(check.scope == "helper" for check in checks)
    assert not config.state_dir.exists()
    assert not config.runtime_dir.exists()


async def test_unreachable_helper_is_a_failure(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await FirecrackerBackend(tmp_path / "missing.sock").diagnostics()


@pytest.mark.parametrize(
    "response",
    [
        {"result": None},
        {"result": {}},
        {"result": []},
        {"result": [{"name": "kvm", "ok": "true", "message": "bad", "scope": "helper"}]},
        {"result": [{"name": "kvm", "ok": True, "message": "bad", "scope": "daemon"}]},
        {"result": [{"ok": True}]},
        {"error": "diagnostics unavailable"},
    ],
)
async def test_malformed_helper_diagnostics_are_rejected(
    tmp_path: Path, response: dict[str, Any]
) -> None:
    path = tmp_path / "helper.sock"

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        assert request["operation"] == "diagnostics"
        assert request["payload"] == {}
        writer.write(json.dumps({"id": request["id"], **response}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with unix_server(path, respond):
        with pytest.raises((ValidationError, ValueError, RuntimeError)):
            await FirecrackerBackend(path).diagnostics()


async def test_diagnostics_timeout_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / "helper.sock"
    disconnected = asyncio.Event()

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()
            await reader.read()
            disconnected.set()
        finally:
            writer.close()
            await writer.wait_closed()

    async with unix_server(path, stall):
        async with asyncio.timeout(6):
            with pytest.raises(TimeoutError):
                await FirecrackerBackend(path).diagnostics()
        await asyncio.wait_for(disconnected.wait(), 1)


@pytest.mark.parametrize("payload", [{"path": "/dev/kvm"}, {"command": "id"}])
def test_diagnostics_rejects_caller_arguments(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="does not accept arguments"):
        Request(version=1, id="probe", operation="diagnostics", payload=payload)


async def test_dispatch_rejects_unvalidated_diagnostics_payload(tmp_path: Path) -> None:
    user = pwd.getpwuid(os.getuid()).pw_name
    service = InfrastructureService(
        HelperConfig(guest_artifacts=tmp_path, daemon_user=user, vm_user=user),
        runner=NoOperationsRunner(),
    )
    request = Request.model_construct(
        version=1, id="probe", operation="diagnostics", payload={"command": "id"}
    )
    with pytest.raises(ValueError, match="does not accept arguments"):
        await service.dispatch(request)


async def test_mock_backend_does_not_claim_real_infrastructure_health() -> None:
    checks = await MockVmBackend().diagnostics()
    assert len(checks) == 1
    assert checks[0].name == "mock_backend"
    assert checks[0].scope == "mock"
    assert "Simulation" in checks[0].message
