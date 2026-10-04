from __future__ import annotations

import asyncio
from typing import Protocol

from .rpc import JsonObject, RpcNotification


class CodexTransport(Protocol):
    notifications: asyncio.Queue[RpcNotification]

    async def request(self, method: str, params: JsonObject | None = None) -> JsonObject: ...
    async def notify(self, method: str, params: JsonObject | None = None) -> None: ...


class CodexClient:
    """Small stable API surface over app-server; guest transport proxies requests."""

    def __init__(self, transport: CodexTransport) -> None:
        self.transport = transport
        self.notifications = transport.notifications

    async def initialize(self) -> JsonObject:
        result = await self.transport.request(
            "initialize",
            {
                "clientInfo": {"name": "tokendrain", "title": "tokendrain", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.transport.notify("initialized")
        return result

    async def start_thread(
        self,
        model: str | None = None,
        cwd: str = "/workspace",
        thread_id: str | None = None,
    ) -> str:
        params: JsonObject = {
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        if model:
            params["model"] = model
        if thread_id:
            params["threadId"] = thread_id
        result = await self.transport.request(
            "thread/resume" if thread_id else "thread/start", params
        )
        return str(result["thread"]["id"])

    async def start_turn(
        self,
        thread_id: str,
        prompt: str,
        model: str | None = None,
        effort: str = "medium",
        output_schema: JsonObject | None = None,
    ) -> str:
        params: JsonObject = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "effort": effort,
        }
        if model:
            params["model"] = model
        if output_schema:
            params["outputSchema"] = output_schema
        result = await self.transport.request("turn/start", params)
        return str(result["turn"]["id"])

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        await self.transport.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def read_rate_limits(self) -> JsonObject:
        return await self.transport.request("account/rateLimits/read")

    async def models(self) -> list[JsonObject]:
        result: list[JsonObject] = []
        cursor = None
        while True:
            page = await self.transport.request("model/list", {"cursor": cursor, "limit": 100})
            result.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return result

    async def read_thread(self, thread_id: str) -> JsonObject:
        return await self.transport.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
