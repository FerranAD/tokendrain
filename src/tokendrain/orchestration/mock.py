"""Explicit local simulation. No Codex calls, privileged operations or claimed work."""

import asyncio
from pathlib import Path

from tokendrain.credentials.store import SecretRedactor
from tokendrain.domain import RunReport, UsageWindow
from tokendrain.orchestration.driver import LogCallback, TokenCallback, UsageCallback, WorkSession
from tokendrain.storage.files import FileProjectStorage
from tokendrain.vm.models import VmHandle, VmSpec


class MockStorage(FileProjectStorage):
    async def _disk(self, path: Path, gib: int, label: str) -> None:
        await asyncio.to_thread(path.write_bytes, b"Tokendrain simulated disk\n")

    async def _clone(self, source: Path, destination: Path) -> None:
        import shutil

        await asyncio.to_thread(shutil.copyfile, source, destination)

    async def resize(self, project_id: str, domain: str, gib: int) -> None:
        if domain not in {"environment", "workspace"} or gib < 1:
            raise ValueError("Invalid resize")
        # Simulation intentionally does not allocate giant fake disks.
        async with self.lease(project_id):
            await self.usage(project_id)


class MockVmBackend:
    def __init__(self) -> None:
        self.handles: dict[str, VmHandle] = {}

    async def start(self, spec: VmSpec) -> VmHandle:
        handle = VmHandle(
            execution_id=spec.execution_id, project_id=spec.project_id, vsock_path=Path("/dev/null")
        )
        self.handles[spec.execution_id] = handle
        return handle

    async def stop(self, handle: VmHandle) -> None:
        self.handles.pop(handle.execution_id, None)

    async def reconcile(self) -> list[VmHandle]:
        return list(self.handles.values())


class MockSessionFactory:
    async def connect(
        self,
        handle: VmHandle,
        secrets: dict[str, str],
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
    ) -> WorkSession:
        return MockSession(log, observe)


class MockSession:
    def __init__(self, log: LogCallback, observe: UsageCallback) -> None:
        self.thread_id = "simulation-thread"
        self.redactor = SecretRedactor()
        self.log, self.observe = log, observe

    async def initialize(self, thread_id: str | None, model: str) -> str:
        return self.thread_id

    async def usage(self) -> list[UsageWindow]:
        return []

    async def turn(self, prompt: str, model: str, effort: str, cancel: asyncio.Event) -> RunReport:
        await self.log("Simulation: no VM boot or Codex inference is performed.")
        await asyncio.sleep(0.1)
        if cancel.is_set():
            raise asyncio.CancelledError
        return RunReport(
            status="blocked",
            summary="Simulation completed; no project work performed.",
            blockers=["Select the Firecracker backend for real autonomous execution."],
        )

    async def close(self) -> None:
        pass
