from __future__ import annotations

import asyncio
import fcntl
import json
import os
import shutil
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel

from tokendrain.vm.commands import CommandRunner, Runner


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
    vm_path: Path
    virtual_size_bytes: int
    allocated_bytes: int = 0


class ProjectStorage(Protocol):
    async def create(self, project_id: str) -> StorageInfo: ...
    async def usage(self, project_id: str) -> StorageInfo: ...
    def lease(self, project_id: str) -> AbstractAsyncContextManager[None]: ...
    async def resize(self, project_id: str, gib: int) -> None: ...
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
    def __init__(
        self,
        root: Path,
        disk_gib: int = 40,
        runner: Runner | None = None,
        base_image: Path | None = None,
    ) -> None:
        self.base_image = base_image
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
                    yield
                finally:
                    del self._owners[key]
                    fcntl.flock(stream, fcntl.LOCK_UN)

    async def _provision(self, path: Path) -> None:
        if self.base_image is None:
            raise RuntimeError("Project creation requires the Nix-built base VM filesystem")
        if not 1 <= self.disk_gib <= 16384:
            raise ValueError("VM storage must be between 1 and 16384 GiB")
        size = self.disk_gib * 1024**3
        if size < self.base_image.stat().st_size:
            raise ValueError("VM storage size is smaller than the base development machine")
        await self._clone(self.base_image, path)
        await self._grow(path, size)

    async def create(self, project_id: str) -> StorageInfo:
        async with self.lease(project_id):
            directory = self.directory(project_id)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / "vm.img"
            if not path.exists():
                stage = directory / "vm.new"
                stage.unlink(missing_ok=True)
                try:
                    await self._provision(stage)
                    stage.replace(path)
                except BaseException:
                    await durable_io(stage.unlink, missing_ok=True)
                    raise
            await durable_io(self._sync_directory, directory)
            await durable_io(self._sync_directory, directory.parent)
            await durable_io(self._sync_directory, self.root)
            return await self.usage(project_id)

    async def usage(self, project_id: str) -> StorageInfo:
        def read() -> StorageInfo:
            directory = self.directory(project_id)
            path = directory / "vm.img"
            info = path.stat()
            return StorageInfo(
                project_id=identifier(project_id),
                vm_path=path,
                virtual_size_bytes=info.st_size,
                allocated_bytes=info.st_blocks * 512,
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
        await durable_io(destination.chmod, 0o600)
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

    async def _grow(self, path: Path, size: int) -> None:
        if size < (await asyncio.to_thread(path.stat)).st_size:
            raise ValueError("Shrinking VM storage is not supported")
        result = await self.runner.run("e2fsck", "-pf", str(path), timeout=3600, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError("Filesystem check failed; refusing resize")
        with path.open("r+b") as stream:
            stream.truncate(size)
            stream.flush()
            os.fsync(stream.fileno())
        await self.runner.run("resize2fs", str(path), timeout=3600)
        await durable_io(self._sync_file, path)

    async def resize(self, project_id: str, gib: int) -> None:
        if not 1 <= gib <= 16384:
            raise ValueError("Invalid VM storage size")
        async with self.lease(project_id):
            await self._grow(self.directory(project_id) / "vm.img", gib * 1024**3)

    async def delete_project(self, project_id: str) -> None:
        async with self.lease(project_id):
            path = self.directory(project_id)
            if path.exists():
                await durable_io(shutil.rmtree, path)
