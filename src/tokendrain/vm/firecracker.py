from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter

from tokendrain.doctor import DoctorCheck

from .models import VmHandle, VmSpec


class FirecrackerBackend:
    """Unprivileged client for the narrow local infrastructure helper API."""

    def __init__(self, helper_socket: Path) -> None:
        self.helper_socket = helper_socket

    async def _request(self, operation: str, payload: dict[str, Any]) -> Any:
        async with asyncio.timeout(180):
            reader, writer = await asyncio.open_unix_connection(
                str(self.helper_socket), limit=65536
            )
            request_id = str(uuid4())
            try:
                writer.write(
                    json.dumps(
                        {"version": 1, "id": request_id, "operation": operation, "payload": payload}
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                response = json.loads(await reader.readline())
                if response.get("id") != request_id:
                    raise RuntimeError("Mismatched infrastructure response")
                if "error" in response:
                    raise RuntimeError(str(response["error"]))
                return response["result"]
            finally:
                writer.close()
                await writer.wait_closed()

    async def start(self, spec: VmSpec) -> VmHandle:
        return VmHandle.model_validate(await self._request("start", spec.model_dump()))

    async def diagnostics(self) -> list[DoctorCheck]:
        """Check infrastructure in its real privilege/device namespace."""
        async with asyncio.timeout(5):
            result = await self._request("diagnostics", {})
        checks = TypeAdapter(list[DoctorCheck]).validate_python(result, strict=True)
        if not checks or any(check.scope != "helper" for check in checks):
            raise ValueError("Infrastructure diagnostics must contain helper-scoped checks")
        return checks

    async def stop(self, handle: VmHandle) -> None:
        await self._request("stop", {"execution_id": handle.execution_id})

    async def reconcile(self) -> list[VmHandle]:
        """List surviving services; caller stops or resumes before releasing projects."""
        return [VmHandle.model_validate(value) for value in await self._request("list", {})]
