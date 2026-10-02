"""Bidirectional asynchronous JSON-RPC peer over bounded newline JSON.

Codex omits jsonrpc:'2.0' on the wire. The dispatcher deliberately never retries
requests: a dropped connection cannot prove a side-effecting operation failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

JsonObject = dict[str, Any]
LOSSY_NOTIFICATIONS = frozenset(
    {
        "item/agentMessage/delta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
        "item/reasoning/textDelta",
        "item/reasoning/summaryTextDelta",
        "guest/log",
    }
)


class RpcNotification(BaseModel):
    method: str
    params: JsonObject = Field(default_factory=dict)


class NotificationQueue(asyncio.Queue[RpcNotification]):
    """Bound queued memory as well as event count for untrusted guest traffic."""

    def __init__(self, max_bytes: int = 16 * 1024 * 1024) -> None:
        super().__init__(maxsize=256)
        self.max_bytes = max_bytes
        self.queued_bytes = 0
        self._sizes: deque[int] = deque()
        self._space = asyncio.Event()

    async def put(self, item: RpcNotification) -> None:
        size = len(item.model_dump_json().encode())
        if size > self.max_bytes:
            raise ValueError("notification exceeds queue byte limit")
        while True:
            self._space.clear()
            if not self.full() and self.queued_bytes + size <= self.max_bytes:
                self.put_nowait(item)
                return
            await self._space.wait()

    def put_nowait(self, item: RpcNotification) -> None:
        size = len(item.model_dump_json().encode())
        if self.queued_bytes + size > self.max_bytes:
            raise asyncio.QueueFull
        super().put_nowait(item)

    def _put(self, item: RpcNotification) -> None:
        self._sizes.append(len(item.model_dump_json().encode()))
        self.queued_bytes += self._sizes[-1]
        super()._put(item)

    def _get(self) -> RpcNotification:
        item = super()._get()
        self.queued_bytes -= self._sizes.popleft()
        self._space.set()
        return item


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.data = code, data


class JsonRpcPeer:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        request_handler: Callable[[str, JsonObject], Awaitable[JsonObject]] | None = None,
        timeout: float = 60,
    ) -> None:
        self.reader, self.writer = reader, writer
        self.timeout = timeout
        self.request_handler = request_handler
        self.notifications: asyncio.Queue[RpcNotification] = NotificationQueue()
        self._pending: dict[int, asyncio.Future[JsonObject]] = {}
        self._counter = 0
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self.closed = asyncio.Event()
        self.failure: BaseException | None = None
        self.dropped_output_events = 0

    async def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="codex-rpc-reader")

    async def _write(self, message: JsonObject) -> None:
        encoded = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        async with self._write_lock:
            self.writer.write(encoded)
            await asyncio.wait_for(self.writer.drain(), timeout=self.timeout)

    async def request(self, method: str, params: JsonObject | None = None) -> JsonObject:
        if self.closed.is_set():
            raise ConnectionError("JSON-RPC connection is closed")
        self._counter += 1
        request_id = self._counter
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout=self.timeout)
        finally:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                # A failed write can race disconnect before awaiting this future.
                future.exception()

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        await self._write({"method": method, "params": params or {}})

    async def _serve_request(self, message: JsonObject) -> None:
        try:
            if self.request_handler is None:
                raise RpcError(-32601, "server-initiated method unsupported")
            async with asyncio.timeout(self.timeout):
                result = await self.request_handler(message["method"], message.get("params") or {})
            await self._write({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with contextlib.suppress(ConnectionError, BrokenPipeError, TimeoutError):
                await self._write(
                    {
                        "id": message["id"],
                        "error": {
                            "code": exc.code if isinstance(exc, RpcError) else -32603,
                            "data": exc.data if isinstance(exc, RpcError) else None,
                            "message": str(exc)
                            if isinstance(exc, RpcError)
                            else "request handler failed",
                        },
                    }
                )

    def _fail(self, failure: BaseException) -> None:
        self.failure = failure
        self.closed.set()
        self.writer.close()
        for pending in self._pending.values():
            if not pending.done():
                pending.set_exception(failure)

    def _handler_done(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        if not task.cancelled() and (failure := task.exception()) is not None:
            self._fail(failure)

    async def _read_message(self) -> JsonObject:
        line = await self.reader.readline()
        if not line:
            raise ConnectionError("Codex app-server disconnected")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("JSON-RPC frame must be an object")
        return message

    async def _read_loop(self) -> None:
        failure: Exception = ConnectionError("JSON-RPC connection closed")
        try:
            while True:
                message = await self._read_message()
                if "method" in message:
                    if "id" in message:
                        if len(self._handlers) >= 64:
                            raise ValueError("too many concurrent RPC requests")
                        task = asyncio.create_task(self._serve_request(message))
                        self._handlers.add(task)
                        task.add_done_callback(self._handler_done)
                    else:
                        # The reader also dispatches RPC replies. Waiting for a
                        # full event queue here deadlocks a caller awaiting RPC.
                        notification = RpcNotification.model_validate(message)
                        try:
                            self.notifications.put_nowait(notification)
                        except asyncio.QueueFull:
                            if notification.method in LOSSY_NOTIFICATIONS:
                                self.dropped_output_events += 1
                            else:
                                # Never silently lose completion, errors or quota
                                # observations. Force explicit state recovery.
                                raise ConnectionError(
                                    "Codex event queue overloaded; state recovery required"
                                ) from None
                elif (pending := self._pending.get(message.get("id", -1))) is not None:
                    if pending.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        pending.set_exception(
                            RpcError(error["code"], error["message"], error.get("data"))
                        )
                    else:
                        result = message.get("result") or {}
                        if not isinstance(result, dict):
                            raise ValueError("JSON-RPC result must be an object")
                        pending.set_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = exc
        finally:
            self._fail(failure)

    async def drain_requests(self) -> None:
        """Wait for already accepted requests, including a shutdown reply."""
        await asyncio.gather(*tuple(self._handlers))

    async def close(self) -> None:
        self._fail(ConnectionError("JSON-RPC connection closed"))
        if self._reader_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        for task in tuple(self._handlers):
            task.cancel()
        await asyncio.gather(*tuple(self._handlers), return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), 5)
