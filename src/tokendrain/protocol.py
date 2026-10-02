"""Version 1 guest protocol: uint32 network-byte-order length + UTF-8 JSON."""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from tokendrain.codex.rpc import JsonObject, JsonRpcPeer

VERSION = 1
MAX_FRAME = 8 * 1024 * 1024
GUEST_PORT = 4050


class GuestEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    id: int | str | None = None
    method: str | None = None
    params: JsonObject = Field(default_factory=dict)
    result: JsonObject | None = None
    error: JsonObject | None = None


async def read_frame(reader: asyncio.StreamReader) -> JsonObject:
    header = await reader.readexactly(4)
    length = struct.unpack("!I", header)[0]
    if not 1 <= length <= MAX_FRAME:
        raise ValueError("invalid guest frame length")
    raw = await reader.readexactly(length)
    envelope = GuestEnvelope.model_validate_json(raw)
    return envelope.model_dump(exclude_none=True)


def encode_frame(message: JsonObject) -> bytes:
    message = {"version": VERSION, **message}
    data = json.dumps(message, separators=(",", ":")).encode()
    if len(data) > MAX_FRAME:
        raise ValueError("guest frame exceeds maximum")
    return struct.pack("!I", len(data)) + data


class FramedPeer(JsonRpcPeer):
    async def _write(self, message: JsonObject) -> None:
        async with self._write_lock:
            self.writer.write(encode_frame(message))
            await asyncio.wait_for(self.writer.drain(), self.timeout)

    async def _read_message(self) -> JsonObject:
        return await read_frame(self.reader)


class GuestClient:
    def __init__(self, peer: FramedPeer) -> None:
        self.peer = peer
        self.notifications = peer.notifications
        self.closed = peer.closed

    @classmethod
    async def connect(
        cls,
        vsock_path: Path | str,
        port: int = GUEST_PORT,
        *,
        request_handler: Callable[[str, JsonObject], Awaitable[JsonObject]] | None = None,
        timeout: float = 90,  # noqa: ASYNC109 - bounded connection handshake
        rpc_timeout: float = 90,
    ) -> GuestClient:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(str(vsock_path), limit=MAX_FRAME)
            try:
                writer.write(f"CONNECT {port}\n".encode())
                await writer.drain()
                line = await reader.readline()
                if not line.startswith(b"OK "):
                    raise ConnectionError("Firecracker vsock connection rejected")
            except BaseException:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                raise
            peer = FramedPeer(reader, writer, request_handler=request_handler, timeout=rpc_timeout)
            await peer.start()
            return cls(peer)

    async def request(self, method: str, params: JsonObject | None = None) -> JsonObject:
        # Codex methods use slash names; lifecycle operations use underscores.
        if "/" in method or method == "initialize":
            return await self.peer.request(
                "codex_rpc_request", {"method": method, "params": params or {}}
            )
        return await self.peer.request(method, params)

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        await self.peer.request(
            "codex_rpc_notification", {"method": method, "params": params or {}}
        )

    async def credentials_set(self, credentials: dict[str, Any]) -> None:
        await self.request("credentials_set", credentials)

    async def close(self) -> None:
        await self.peer.close()
