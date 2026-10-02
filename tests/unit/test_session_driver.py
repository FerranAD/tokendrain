from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from tokendrain.auth.openai import AccountInfo, RuntimeCredentials
from tokendrain.codex.rpc import RpcNotification
from tokendrain.domain import RunReport
from tokendrain.orchestration.driver import GuestConnection, RealSessionFactory
from tokendrain.vm.models import VmHandle


class Auth:
    def __init__(self, rotate: bool = False) -> None:
        self.calls = 0
        self.rotate = rotate
        self.selected: list[str | None] = []

    async def accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo(
                id="signed-out", method="siwc", subject="user", expires_at=0, connected=False
            ),
            AccountInfo(
                id="connected", method="siwc", subject="user", expires_at=time.time() + 3600
            ),
        ]

    async def runtime_credentials(
        self, account_id: str | None = None, force_refresh: bool = False
    ) -> RuntimeCredentials:
        self.calls += 1
        self.selected.append(account_id)
        return RuntimeCredentials(
            mode="siwc",
            access_token="replacement-access"
            if self.rotate and self.calls >= 3
            else "initial-access",
            expires_at=time.time() + 3600,
        )


class Guest:
    def __init__(self, mode: str = "completed") -> None:
        self.closed = asyncio.Event()
        self.notifications: asyncio.Queue[RpcNotification] = asyncio.Queue()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.mode = mode
        self.runtime: dict[str, Any] = {}

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        value = params or {}
        self.requests.append((method, value))
        if method in {"thread/start", "thread/resume", "thread/read"}:
            return {"thread": {"id": "thread"}}
        if method == "turn/start":
            if self.mode == "crash":
                await self.notifications.put(
                    RpcNotification(method="guest/codex_exited", params={"exit_code": 1})
                )
            elif self.mode == "completed":
                report = RunReport(status="completed", summary='secret"quoted', task_log="verified")
                await self.notifications.put(
                    RpcNotification(
                        method="item/completed",
                        params={
                            "threadId": "thread",
                            "turnId": "turn",
                            "item": {"type": "agentMessage", "text": report.model_dump_json()},
                        },
                    )
                )
                await self.notifications.put(
                    RpcNotification(
                        method="turn/completed",
                        params={"turn": {"id": "turn", "status": "completed"}},
                    )
                )
            return {"turn": {"id": "turn"}}
        if method == "turn/interrupt":
            await self.notifications.put(
                RpcNotification(
                    method="turn/completed",
                    params={"turn": {"id": "turn", "status": "interrupted"}},
                )
            )
        return {}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        pass

    async def credentials_set(self, credentials: dict[str, Any]) -> None:
        self.runtime = credentials

    async def close(self) -> None:
        self.closed.set()


class Connector:
    def __init__(self, guests: list[Guest]) -> None:
        self.guests = guests

    async def __call__(self, vsock_path: Path | str, port: int, **kwargs: Any) -> GuestConnection:
        return self.guests.pop(0)


async def test_connected_account_strict_schema_and_recursive_report_redaction() -> None:
    guest = Guest()
    auth = Auth()

    async def noop(*args: Any) -> None:
        pass

    factory = RealSessionFactory(auth, connector=Connector([guest]))
    session = await factory.connect(
        VmHandle(execution_id="e", project_id="p", vsock_path=Path("/unused")),
        {"EXAMPLE": 'secret"quoted'},
        noop,
        noop,
        noop,
    )
    await session.initialize(None, "")
    report = await session.turn("Implement", "", "medium", asyncio.Event())
    assert report.summary == "[REDACTED]"
    assert all(account == "connected" for account in auth.selected)
    request = next(params for method, params in guest.requests if method == "turn/start")
    assert "usage" not in request["outputSchema"]["properties"]
    await session.close()


async def test_guest_failure_reconnects_reads_thread_and_never_replays_turn() -> None:
    first, second = Guest("crash"), Guest()
    auth = Auth()

    async def noop(*args: Any) -> None:
        pass

    session = await RealSessionFactory(auth, connector=Connector([first, second])).connect(
        VmHandle(execution_id="e", project_id="p", vsock_path=Path("/unused")), {}, noop, noop, noop
    )
    await session.initialize("thread", "")
    recovered = await session.turn("Potential side effect", "", "medium", asyncio.Event())
    assert recovered.status == "in_progress" and "recovered" in recovered.summary
    assert not any(method == "turn/start" for method, _ in second.requests)
    methods = [method for method, _ in second.requests]
    assert methods.index("thread/read") < methods.index("thread/resume")
    await session.turn(
        "Inspect current state before choosing new work", "", "medium", asyncio.Event()
    )
    turns = [params for method, params in second.requests if method == "turn/start"]
    assert len(turns) == 1 and "Inspect current state" in turns[0]["input"][0]["text"]
    await session.close()


async def test_token_rotation_interrupts_once_and_resumes_existing_thread() -> None:
    guest = Guest("wait")
    auth = Auth(rotate=True)

    async def noop(*args: Any) -> None:
        pass

    session = await RealSessionFactory(auth, connector=Connector([guest])).connect(
        VmHandle(execution_id="e", project_id="p", vsock_path=Path("/unused")), {}, noop, noop, noop
    )
    await session.initialize("thread", "")
    report = await session.turn("Original unit", "", "medium", asyncio.Event())
    assert report.status == "in_progress" and "credential renewal" in report.summary
    assert sum(method == "turn/interrupt" for method, _ in guest.requests) == 1
    rotation = next(params for method, params in guest.requests if method == "openai_token_rotate")
    assert rotation["access_token"] == "replacement-access"
    assert sum(method == "turn/start" for method, _ in guest.requests) == 1
    await session.close()


async def test_failed_shutdown_propagates_and_always_closes_transport() -> None:
    import pytest

    from tokendrain.codex.rpc import RpcError

    class BadShutdown(Guest):
        async def request(
            self, method: str, params: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            if method == "codex_stop":
                raise RpcError(-32000, "guest stop failed")
            return await super().request(method, params)

    guest = BadShutdown()

    async def noop(*args: Any) -> None:
        pass

    session = await RealSessionFactory(Auth(), connector=Connector([guest])).connect(
        VmHandle(execution_id="e", project_id="p", vsock_path=Path("/unused")),
        {"EXAMPLE": "value"},
        noop,
        noop,
        noop,
    )
    with pytest.raises(RpcError, match="stop failed"):
        await session.close()
    assert guest.closed.is_set()
