from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from tokendrain.codex import CodexClient, JsonRpcPeer, RpcError
from tokendrain.protocol import FramedPeer, GuestClient, encode_frame, read_frame


async def test_frame_roundtrip_bounds_and_version() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(encode_frame({"id": 1, "method": "ping"}))
    assert (await read_frame(reader))["method"] == "ping"
    reader.feed_data(struct.pack("!I", 9 * 1024 * 1024))
    with pytest.raises(ValueError, match="length"):
        await read_frame(reader)
    bad = b'{"version":2,"method":"ping"}'
    reader.feed_data(struct.pack("!I", len(bad)) + bad)
    with pytest.raises(ValueError):
        await read_frame(reader)


async def test_rpc_out_of_order_server_requests_and_disconnect(tmp_path: Path) -> None:
    peers: list[JsonRpcPeer] = []

    async def handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "fail":
            raise RpcError(42, "expected failure")
        if method == "slow":
            await asyncio.sleep(0.03)
        return {"method": method, **params}

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = JsonRpcPeer(reader, writer, request_handler=handler)
        peers.append(peer)
        await peer.start()

    socket = tmp_path / "rpc.sock"
    server = await asyncio.start_unix_server(accept, socket)
    async with server:
        reader, writer = await asyncio.open_unix_connection(socket)
        client = JsonRpcPeer(reader, writer, request_handler=handler)
        await client.start()
        try:
            responses = await asyncio.gather(client.request("slow"), client.request("fast"))
            assert [result["method"] for result in responses] == ["slow", "fast"]
            await peers[0].notify("turn/completed", {"turn": {"status": "completed"}})
            assert (await client.notifications.get()).method == "turn/completed"
            assert (await peers[0].request("host-refresh"))["method"] == "host-refresh"
            with pytest.raises(RpcError, match="expected failure"):
                await client.request("fail")
            pending = asyncio.create_task(client.request("slow"))
            await asyncio.sleep(0)
            await peers[0].close()
            with pytest.raises((ConnectionError, BrokenPipeError)):
                await pending
        finally:
            await client.close()
            for peer in peers:
                await peer.close()


async def test_rpc_timeout_removes_pending_request(tmp_path: Path) -> None:
    writer_holder: list[asyncio.StreamWriter] = []

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer_holder.append(writer)
        await reader.read()
        writer.close()

    server = await asyncio.start_unix_server(accept, tmp_path / "timeout.sock")
    async with server:
        reader, writer = await asyncio.open_unix_connection(tmp_path / "timeout.sock")
        peer = JsonRpcPeer(reader, writer, timeout=0.01)
        await peer.start()
        with pytest.raises(TimeoutError):
            await peer.request("never-responds")
        assert not peer._pending
        await peer.close()
        for accepted in writer_holder:
            accepted.close()


async def test_guest_handshake_and_codex_proxy(tmp_path: Path) -> None:
    messages: list[tuple[str, dict[str, Any]]] = []
    peer_holder: list[FramedPeer] = []

    async def handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
        messages.append((method, params))
        if method == "codex_rpc_request":
            if params["method"] == "thread/start":
                return {"thread": {"id": "thread-1"}}
            if params["method"] == "turn/start":
                return {"turn": {"id": "turn-1"}}
        return {}

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        assert await reader.readline() == b"CONNECT 4050\n"
        writer.write(b"OK 4050\n")
        await writer.drain()
        peer = FramedPeer(reader, writer, request_handler=handle)
        peer_holder.append(peer)
        await peer.start()

    server = await asyncio.start_unix_server(accept, tmp_path / "vsock")
    async with server:
        client = await GuestClient.connect(tmp_path / "vsock")
        try:
            await client.request("ping")
            codex = CodexClient(client)
            await codex.initialize()
            thread = await codex.start_thread("test-model")
            assert thread == "thread-1"
            assert await codex.start_turn(thread, "implement useful work") == "turn-1"
            start = next(
                params["params"]
                for method, params in messages
                if method == "codex_rpc_request" and params["method"] == "thread/start"
            )
            assert start["approvalPolicy"] == "never"
            assert start["sandbox"] == "danger-full-access"
            assert "refresh_token" not in json.dumps(messages)
        finally:
            await client.close()
            for peer in peer_holder:
                await peer.close()


async def test_notification_queue_bounds_bytes_and_releases_backpressure() -> None:
    from tokendrain.codex.rpc import NotificationQueue, RpcNotification

    queue = NotificationQueue(max_bytes=100)
    event = RpcNotification(method="log", params={"line": "x" * 40})
    await queue.put(event)
    pending = asyncio.create_task(queue.put(event))
    await asyncio.sleep(0)
    assert not pending.done()
    assert await queue.get() == event
    await asyncio.wait_for(pending, 1)
    assert 0 < queue.queued_bytes <= 100
    with pytest.raises(ValueError, match="byte limit"):
        await queue.put(RpcNotification(method="log", params={"line": "x" * 500}))


async def test_output_flood_cannot_block_rpc_response_and_critical_overflow_is_explicit(
    tmp_path: Path,
) -> None:
    peers: list[JsonRpcPeer] = []

    async def handle(method: str, params: dict[str, Any]) -> dict[str, Any]:
        for _ in range(300):
            await peers[0].notify("item/agentMessage/delta", {"delta": "stream fragment"})
        return {"delivered": True}

    async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = JsonRpcPeer(reader, writer, request_handler=handle)
        peers.append(peer)
        await peer.start()

    server = await asyncio.start_unix_server(accept, tmp_path / "flood.sock")
    async with server:
        reader, writer = await asyncio.open_unix_connection(tmp_path / "flood.sock")
        peer = JsonRpcPeer(reader, writer)
        await peer.start()
        try:
            result = await asyncio.wait_for(peer.request("burst"), 1)
            assert result == {"delivered": True}
            assert peer.notifications.qsize() == 256 and peer.dropped_output_events == 44
            await peers[0].notify("turn/completed", {"turn": {"id": "turn", "status": "completed"}})
            await asyncio.wait_for(peer.closed.wait(), 1)
            assert peer.failure and "overloaded" in str(peer.failure)
        finally:
            await peer.close()
            for remote in peers:
                await remote.close()
