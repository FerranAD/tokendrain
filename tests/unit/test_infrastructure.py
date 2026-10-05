# ruff: noqa: ASYNC109, ASYNC240
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tokendrain.networking.linux import LinuxNetwork, allocation, firewall_rules
from tokendrain.storage import FileProjectStorage
from tokendrain.storage.files import durable_io
from tokendrain.vm.commands import CommandResult
from tokendrain.vm.firecracker import FirecrackerBackend
from tokendrain.vm.helper import Request
from tokendrain.vm.models import VmSpec


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[bytes] = []
        self.fail: str | None = None
        self.routes: list[dict[str, str]] = []

    async def run(
        self, *argv: str, input: bytes | None = None, timeout: float = 120, check: bool = True
    ) -> CommandResult:  # noqa: ASYNC109
        self.calls.append(argv)
        if input:
            self.inputs.append(input)
        if self.fail == argv[0] and check:
            raise RuntimeError("injected failure")
        if argv[0] == "cp":
            Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
        if argv[:3] == ("ip", "-json", "-4"):
            return CommandResult(0, json.dumps(self.routes).encode(), b"")
        return CommandResult(0, b"", b"")


@pytest.fixture
async def storage(tmp_path: Path) -> FileProjectStorage:
    base = tmp_path / "base.img"
    base.write_bytes(b"base development machine")
    return FileProjectStorage(tmp_path, disk_gib=1, runner=RecordingRunner(), base_image=base)


def small_disk(storage: FileProjectStorage, project: str) -> None:
    directory = storage.directory(project)
    directory.mkdir(parents=True)
    (directory / "vm.img").write_bytes(b"precious machine")


async def test_create_clones_one_machine_and_preserves_existing(
    storage: FileProjectStorage,
) -> None:
    project = str(uuid4())
    info = await storage.create(project)
    assert info.vm_path.name == "vm.img"
    assert info.virtual_size_bytes == 1024**3
    assert isinstance(storage.runner, RecordingRunner)
    assert any(call[0] == "cp" for call in storage.runner.calls)
    info.vm_path.write_bytes(b"project changes")
    await storage.create(project)
    assert info.vm_path.read_bytes() == b"project changes"
    assert list(storage.directory(project).iterdir()) == [info.vm_path]


async def test_lease_excludes_other_instance_and_is_reentrant(storage: FileProjectStorage) -> None:
    project = str(uuid4())
    other = FileProjectStorage(storage.root)
    async with storage.lease(project):
        async with storage.lease(project):
            with pytest.raises(RuntimeError, match="another process"):
                async with other.lease(project):
                    pytest.fail("lease allowed duplicate writer")
    async with other.lease(project):
        pass


async def test_lease_serializes_async_tasks(storage: FileProjectStorage) -> None:
    project = str(uuid4())
    order: list[int] = []

    async def worker(number: int) -> None:
        async with storage.lease(project):
            order.append(number)
            await asyncio.sleep(0.01)
            order.append(number)

    await asyncio.gather(worker(1), worker(2))
    assert order == [1, 1, 2, 2]


async def test_failed_clone_does_not_publish_machine(storage: FileProjectStorage) -> None:
    project = str(uuid4())
    assert isinstance(storage.runner, RecordingRunner)
    storage.runner.fail = "cp"
    with pytest.raises(RuntimeError):
        await storage.create(project)
    assert not (storage.directory(project) / "vm.img").exists()
    assert not (storage.directory(project) / "vm.new").exists()


async def test_disk_usage_and_delete(storage: FileProjectStorage) -> None:
    project = str(uuid4())
    small_disk(storage, project)
    info = await storage.usage(project)
    assert info.virtual_size_bytes == len(b"precious machine")
    assert info.allocated_bytes >= info.virtual_size_bytes
    await storage.delete_project(project)
    assert not storage.directory(project).exists()


def test_path_traversal_rejected(storage: FileProjectStorage) -> None:
    with pytest.raises(ValueError):
        storage.directory("../../etc")
    with pytest.raises(ValidationError):
        VmSpec(execution_id=str(uuid4()), project_id="../../etc")
    with pytest.raises(ValidationError):
        Request(version=1, id="1", operation="shell", payload={})


def test_network_slots_and_default_isolation() -> None:
    first, second = allocation(str(uuid4()), 0), allocation(str(uuid4()), 1)
    assert first.guest_ip == "100.127.0.2"
    assert second.guest_ip == "100.127.0.6"
    assert len(first.tap) <= 15
    rules = firewall_rules()
    assert 'iifname "tdt*" drop' in rules
    assert 'iifname "tdt*" oifname "tdt*" drop' in rules
    assert "169.254.0.0/16" in rules
    assert "fib daddr type local drop" in rules
    assert "meta nfproto ipv6 drop" in rules
    assert "169.254.0.0/16" not in firewall_rules(allow_lan=True)


async def test_network_cleanup_after_setup_failure() -> None:
    runner = RecordingRunner()
    runner.fail = "nft"
    backend = LinuxNetwork(runner, vm_uid=234)
    network = allocation(str(uuid4()), 0)
    with pytest.raises(RuntimeError):
        await backend.create(network)
    assert ("ip", "link", "delete", network.tap) in runner.calls
    assert any(
        b'"' + network.tap.encode() + b'" . 100.127.0.2' in message for message in runner.inputs
    )


async def test_firewall_refresh_restores_all_guards_in_one_transaction() -> None:
    runner = RecordingRunner()
    runner.routes = [
        {"dst": "93.184.215.0/24", "dev": "eth0", "scope": "link"},
        {"dst": "93.184.215.0/24", "dev": "vpn0", "scope": "link"},
        {"dst": "100.127.0.0/30", "dev": "tdt123", "scope": "link"},
        {"dst": "default", "dev": "eth0", "scope": "global"},
    ]
    guest = allocation(str(uuid4()), 0)
    await LinuxNetwork(runner, 234).refresh_policy([guest])
    assert runner.calls[0] == ("ip", "-json", "-4", "route", "show", "table", "all")
    assert len(runner.inputs) == 1
    transaction = runner.inputs[0].decode()
    assert transaction.count("93.184.215.0/24") == 1
    assert "100.127.0.0/30" not in transaction
    assert f'"{guest.tap}" . {guest.guest_ip}' in transaction
    assert transaction.endswith("add rule inet tokendrain routing_ready return\n")
    assert "chain routing_ready { drop; }" in firewall_rules()


@pytest.mark.parametrize(
    "message, missing",
    [
        ("Cannot find device tdt123", True),
        ("No such file or directory", True),
        ("Operation not permitted", False),
        ("Resource busy", False),
    ],
)
def test_cleanup_only_ignores_missing_resources(message: str, missing: bool) -> None:
    result = CommandResult(1, b"", message.encode())
    if missing:
        LinuxNetwork._removed(result)
    else:
        with pytest.raises(RuntimeError, match="Network cleanup failed"):
            LinuxNetwork._removed(result)


async def test_helper_client_request_response(tmp_path: Path) -> None:
    path = tmp_path / "helper.sock"
    spec = VmSpec(execution_id=str(uuid4()), project_id=str(uuid4()))
    seen: list[str] = []

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        seen.append(request["operation"])
        result = {
            "execution_id": spec.execution_id,
            "project_id": spec.project_id,
            "vsock_path": "/run/test/vsock.sock",
            "guest_port": 4050,
        }
        if request["operation"] == "list":
            result = [result]
        elif request["operation"] == "stop":
            result = None
        writer.write(json.dumps({"id": request["id"], "result": result}).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(connection, path)
    async with server:
        backend = FirecrackerBackend(path)
        handle = await backend.start(spec)
        assert handle.project_id == spec.project_id
        assert (await backend.reconcile())[0] == handle
        await backend.stop(handle)
    assert seen == ["start", "list", "stop"]


async def test_cancellation_retains_lease_until_worker_has_finished(
    storage: FileProjectStorage,
) -> None:
    project = str(uuid4())
    started, release = threading.Event(), threading.Event()
    changes: list[str] = []

    def blocking_mutation() -> None:
        started.set()
        assert release.wait(3), "test did not release worker"
        changes.append("committed")

    async def mutate() -> None:
        async with storage.lease(project):
            await durable_io(blocking_mutation)

    task = asyncio.create_task(mutate())
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # Repeated shutdown cancellation must not release the disks.
    await asyncio.sleep(0)
    other = FileProjectStorage(storage.root)
    try:
        with pytest.raises(RuntimeError, match="another process"):
            async with other.lease(project):
                pytest.fail("mutation is still running")
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert changes == ["committed"]
    async with other.lease(project):
        pass
