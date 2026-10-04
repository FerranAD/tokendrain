from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel

from tokendrain.vm.commands import CommandRunner, Runner

Domain = Literal["environment", "workspace", "all"]


async def durable_io[T, **P](function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Never release a lease while an offloaded mutation is still executing."""
    # Keep a Future rather than a Task: asyncio.run cancels every remaining Task
    # at shutdown, including an inner to_thread Task whose worker is still busy.
    task = asyncio.get_running_loop().run_in_executor(None, partial(function, *args, **kwargs))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def identifier(value: str) -> str:
    """Only UUID path components are accepted at the infrastructure boundary."""
    return str(UUID(value))


class StorageInfo(BaseModel):
    project_id: str
    environment: Path
    workspace: Path
    environment_bytes: int
    workspace_bytes: int
    environment_allocated_bytes: int = 0
    workspace_allocated_bytes: int = 0


class SnapshotInfo(BaseModel):
    id: str
    project_id: str
    created_at: datetime
    environment_bytes: int
    workspace_bytes: int


class ProjectStorage(Protocol):
    async def create(self, project_id: str) -> StorageInfo: ...
    async def snapshot(self, project_id: str) -> SnapshotInfo: ...
    async def restore(self, project_id: str, snapshot_id: str, domain: Domain = "all") -> None: ...
    async def usage(self, project_id: str) -> StorageInfo: ...
    def lease(self, project_id: str) -> AbstractAsyncContextManager[None]: ...
    async def list_snapshots(self, project_id: str) -> list[SnapshotInfo]: ...
    async def delete_snapshot(self, project_id: str, snapshot_id: str) -> None: ...
    async def reset_environment(self, project_id: str) -> None: ...
    async def resize(
        self, project_id: str, domain: Literal["environment", "workspace"], gib: int
    ) -> None: ...
    async def delete_project(self, project_id: str) -> None: ...


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FileProjectStorage:
    def __init__(self, root: Path, disk_gib: int = 40, runner: Runner | None = None) -> None:
        self.root = root
        self.disk_gib = disk_gib
        self.runner = runner or CommandRunner()
        self._locks: dict[str, asyncio.Lock] = {}
        self._owners: dict[str, asyncio.Task[object] | None] = {}

    def directory(self, project_id: str) -> Path:
        return self.root / "projects" / identifier(project_id)

    @asynccontextmanager
    async def lease(self, project_id: str) -> AsyncIterator[None]:
        key = identifier(project_id)
        task = asyncio.current_task()
        if key in self._owners and self._owners[key] is task:
            yield
            return
        async with self._locks.setdefault(key, asyncio.Lock()):
            locks = self.root / "locks"
            locks.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (locks / f"{key}.lock").open("a") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("Project storage is leased by another process") from exc
                self._owners[key] = task
                try:
                    await durable_io(self._recover, self.directory(key))
                    yield
                finally:
                    del self._owners[key]
                    fcntl.flock(stream, fcntl.LOCK_UN)

    @staticmethod
    def _recover(directory: Path) -> None:
        journal = directory / "restore.json"
        if journal.exists():
            domains = json.loads(journal.read_text())
            for domain in domains:
                if domain not in ("environment", "workspace"):
                    raise ValueError("Invalid restore journal")
                stage = directory / f"{domain}.restore"
                if stage.exists():
                    stage.replace(directory / f"{domain}.img")
            FileProjectStorage._sync_directory(directory)
            journal.unlink()
            FileProjectStorage._sync_directory(directory)

    async def _disk(self, path: Path, gib: int, label: str) -> None:
        if not 1 <= gib <= 16384:
            raise ValueError("Disk size must be between 1 and 16384 GiB")
        with path.open("xb") as stream:
            stream.truncate(gib * 1024**3)
        try:
            await self.runner.run("mkfs.ext4", "-q", "-F", "-L", label, str(path))
            await durable_io(self._sync_file, path)
        except BaseException:
            await durable_io(path.unlink, missing_ok=True)
            raise

    async def create(self, project_id: str) -> StorageInfo:
        async with self.lease(project_id):
            directory = self.directory(project_id)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for domain in ("environment", "workspace"):
                path = directory / f"{domain}.img"
                if not path.exists():
                    stage = directory / f"{domain}.new"
                    stage.unlink(missing_ok=True)
                    await self._disk(stage, self.disk_gib, f"td-{domain[:8]}")
                    stage.replace(path)
            await durable_io(self._sync_directory, directory)
            await durable_io(self._sync_directory, directory.parent)
            await durable_io(self._sync_directory, self.root)
            return await self.usage(project_id)

    async def usage(self, project_id: str) -> StorageInfo:
        def read() -> StorageInfo:
            directory = self.directory(project_id)
            env, workspace = directory / "environment.img", directory / "workspace.img"
            e, w = env.stat(), workspace.stat()
            return StorageInfo(
                project_id=identifier(project_id),
                environment=env,
                workspace=workspace,
                environment_bytes=e.st_size,
                workspace_bytes=w.st_size,
                environment_allocated_bytes=e.st_blocks * 512,
                workspace_allocated_bytes=w.st_blocks * 512,
            )

        return await asyncio.to_thread(read)

    async def _clone(self, source: Path, destination: Path) -> None:
        # GNU cp tries FICLONE and safely falls back to a sparse ordinary copy.
        await self.runner.run(
            "cp",
            "--reflink=auto",
            "--sparse=always",
            "--",
            str(source),
            str(destination),
            timeout=3600,
        )
        await durable_io(self._sync_file, destination)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _sync_file(path: Path) -> None:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    async def snapshot(self, project_id: str) -> SnapshotInfo:
        async with self.lease(project_id):
            info = await self.usage(project_id)
            snapshot = SnapshotInfo(
                id=str(uuid4()),
                project_id=identifier(project_id),
                created_at=datetime.now(UTC),
                environment_bytes=info.environment_bytes,
                workspace_bytes=info.workspace_bytes,
            )
            base = self.directory(project_id) / "snapshots"
            base.mkdir(exist_ok=True)
            stage = base / f".{snapshot.id}.new"
            stage.mkdir()
            try:
                for domain in ("environment", "workspace"):
                    await self._clone(
                        self.directory(project_id) / f"{domain}.img", stage / f"{domain}.img"
                    )
                await durable_io(
                    atomic_json, stage / "metadata.json", snapshot.model_dump(mode="json")
                )
                stage.replace(base / snapshot.id)
                await durable_io(self._sync_directory, base)
            except BaseException:
                await durable_io(shutil.rmtree, stage, True)
                raise
            return snapshot

    async def list_snapshots(self, project_id: str) -> list[SnapshotInfo]:
        def read() -> list[SnapshotInfo]:
            base = self.directory(project_id) / "snapshots"
            return sorted(
                (
                    SnapshotInfo.model_validate_json(path.read_text())
                    for path in base.glob("*/metadata.json")
                    if not path.parent.name.startswith(".")
                ),
                key=lambda item: item.created_at,
                reverse=True,
            )

        return await asyncio.to_thread(read)

    async def restore(self, project_id: str, snapshot_id: str, domain: Domain = "all") -> None:
        if domain not in ("environment", "workspace", "all"):
            raise ValueError("Invalid storage domain")
        async with self.lease(project_id):
            directory = self.directory(project_id)
            source = directory / "snapshots" / identifier(snapshot_id)
            SnapshotInfo.model_validate_json((source / "metadata.json").read_text())
            domains = ["environment", "workspace"] if domain == "all" else [domain]
            try:
                for part in domains:
                    await self._clone(source / f"{part}.img", directory / f"{part}.restore")
                await durable_io(atomic_json, directory / "restore.json", domains)
                await durable_io(self._recover, directory)
            except BaseException:
                if not (directory / "restore.json").exists():
                    for part in domains:
                        (directory / f"{part}.restore").unlink(missing_ok=True)
                raise

    async def delete_snapshot(self, project_id: str, snapshot_id: str) -> None:
        async with self.lease(project_id):
            path = self.directory(project_id) / "snapshots" / identifier(snapshot_id)
            await durable_io(shutil.rmtree, path)

    async def reset_environment(self, project_id: str) -> None:
        async with self.lease(project_id):
            directory = self.directory(project_id)
            stage = directory / "environment.new"
            stage.unlink(missing_ok=True)
            await self._disk(stage, self.disk_gib, "td-environm")
            stage.replace(directory / "environment.img")
            await durable_io(self._sync_directory, directory)

    async def resize(
        self, project_id: str, domain: Literal["environment", "workspace"], gib: int
    ) -> None:
        if domain not in ("environment", "workspace") or not 1 <= gib <= 16384:
            raise ValueError("Invalid disk or size")
        async with self.lease(project_id):
            path = self.directory(project_id) / f"{domain}.img"
            size = gib * 1024**3
            if size < path.stat().st_size:
                raise ValueError("Shrinking disks is not supported")
            result = await self.runner.run("e2fsck", "-pf", str(path), timeout=3600, check=False)
            if result.returncode not in (0, 1):
                raise RuntimeError("Filesystem check failed; refusing resize")
            with path.open("r+b") as stream:
                stream.truncate(size)
            await self.runner.run("resize2fs", str(path), timeout=3600)

    async def delete_project(self, project_id: str) -> None:
        async with self.lease(project_id):
            path = self.directory(project_id)
            if path.exists():
                await durable_io(shutil.rmtree, path)
