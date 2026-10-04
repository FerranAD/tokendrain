"""Small privileged service. It accepts resource IDs, never caller-supplied paths.

The web daemon cannot request arbitrary commands, mounts, or files. Firecracker
runs as a separate unprivileged user inside a systemd filesystem/cgroup sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import pwd
import shutil
import signal
import socket
import stat
import struct
import sys
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tokendrain.doctor import inspect_vm_checks
from tokendrain.logging import configure_logging
from tokendrain.networking.linux import LinuxNetwork, NetworkAllocation, allocation
from tokendrain.storage.export import workspace_path
from tokendrain.storage.files import atomic_json, durable_io

from .commands import CommandRunner, Runner
from .models import VmHandle, VmSpec

logger = logging.getLogger(__name__)


class HelperConfig(BaseModel):
    state_dir: Path = Path("/var/lib/tokendrain")
    runtime_dir: Path = Path("/run/tokendrain-vms")
    socket_path: Path = Path("/run/tokendrain/helper.sock")
    guest_artifacts: Path
    firecracker: str = "firecracker"
    daemon_user: str = "tokendrain"
    vm_user: str = "tokendrain-vm"
    max_concurrency: int = Field(default=2, ge=1, le=128)
    max_memory_mib: int = Field(default=16384, ge=512)
    max_vcpus: int = Field(default=16, ge=1, le=32)
    total_memory_mib: int | None = Field(default=None, ge=512)


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    id: str = Field(max_length=64)
    operation: Literal["start", "stop", "list", "diagnostics", "workspace_export"]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def diagnostics_has_no_arguments(self) -> Request:
        if self.operation == "diagnostics" and self.payload:
            raise ValueError("diagnostics does not accept arguments")
        return self


class Record(BaseModel):
    spec: VmSpec
    handle: VmHandle
    network: NetworkAllocation


def unit_name(execution_id: str) -> str:
    return f"tokendrain-vm-{UUID(execution_id).hex}.service"


async def notify_ready() -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.setblocking(False)
        await asyncio.wait_for(
            asyncio.get_running_loop().sock_sendto(channel, b"READY=1", address), 2
        )


class InfrastructureService:
    def __init__(self, config: HelperConfig, runner: Runner | None = None) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.daemon_uid = pwd.getpwnam(config.daemon_user).pw_uid
        self.vm_uid = pwd.getpwnam(config.vm_user).pw_uid
        self.vm_gid = pwd.getpwnam(config.vm_user).pw_gid
        self.network = LinuxNetwork(self.runner, self.vm_uid)
        self._lock = asyncio.Lock()

    def records(self) -> list[Record]:
        records = []
        for path in self.config.runtime_dir.glob("*/record.json"):
            try:
                records.append(Record.model_validate_json(path.read_text()))
            except FileNotFoundError:
                # Cleanup may finish while the route watcher takes its snapshot.
                continue
        return records

    async def active(self, execution_id: str) -> bool:
        result = await self.runner.run(
            "systemctl",
            "show",
            "--property=ActiveState",
            "--value",
            unit_name(execution_id),
            check=False,
            timeout=10,
        )
        return result.stdout.strip() in (b"active", b"activating", b"deactivating", b"reloading")

    def _open_disk(self, project_id: str, domain: str) -> int:
        descriptor = os.open(self.config.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in ("projects", str(UUID(project_id))):
                child = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            disk = os.open(f"{domain}.img", os.O_RDWR | os.O_NOFOLLOW, dir_fd=descriptor)
            metadata = os.fstat(disk)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                os.close(disk)
                raise ValueError("Project disk must be a regular, non-hardlinked file")
            os.fchown(disk, self.daemon_uid, self.vm_gid)
            os.fchmod(disk, 0o660)
            return disk
        finally:
            os.close(descriptor)

    async def start(self, spec: VmSpec) -> VmHandle:
        if spec.memory_mib > self.config.max_memory_mib or spec.vcpus > self.config.max_vcpus:
            raise ValueError("VM resource request exceeds platform limits")
        records = self.records()
        for old in records:
            if old.spec.execution_id == spec.execution_id:
                if old.spec != spec:
                    raise ValueError(
                        "Execution ID is already associated with different VM settings"
                    )
                if await self.active(spec.execution_id):
                    return old.handle
                await self.stop(spec.execution_id)
            elif old.spec.project_id == spec.project_id:
                raise ValueError("Project is already attached to a VM; reconcile it first")
        records = self.records()
        if len(records) >= self.config.max_concurrency:
            raise ValueError("Platform VM concurrency limit reached")
        memory_budget = self.config.total_memory_mib
        if memory_budget is None:
            meminfo = await asyncio.to_thread(Path("/proc/meminfo").read_text)
            total_kib = next(
                int(line.split()[1])
                for line in meminfo.splitlines()
                if line.startswith("MemTotal:")
            )
            # Keep at least a quarter of physical RAM for the host and page cache.
            memory_budget = total_kib * 3 // (4 * 1024)
        reserved = sum(record.spec.memory_mib + 512 for record in records)
        if reserved + spec.memory_mib + 512 > memory_budget:
            raise ValueError("Insufficient platform VM memory budget (host RAM reserve protected)")
        used = {record.network.host_ip for record in records}
        network = next(
            candidate
            for slot in range(16384)
            if (candidate := allocation(spec.execution_id, slot)).host_ip not in used
        )
        base = self.config.runtime_dir / spec.execution_id
        jail = base / "root"
        sockets = base / "sockets"
        for path in (jail / "nix/store", jail / "disks", jail / "artifacts", sockets):
            path.mkdir(parents=True, exist_ok=True)
        for path in (
            base,
            jail,
            jail / "nix",
            jail / "nix/store",
            jail / "disks",
            jail / "artifacts",
        ):
            os.chmod(path, 0o755)
        os.chown(sockets, self.vm_uid, self.vm_gid)
        os.chmod(sockets, 0o770)
        # Daemon gets access to the vsock UDS through a bind-visible directory;
        # it belongs to the VM group in the NixOS module.
        handle = VmHandle(
            execution_id=spec.execution_id,
            project_id=spec.project_id,
            vsock_path=sockets / "vsock.sock",
        )
        record = Record(spec=spec, handle=handle, network=network)
        atomic_json(base / "record.json", record.model_dump(mode="json"))
        descriptors: list[int] = []
        try:
            await self.network.refresh_policy([item.network for item in self.records()])
            await self.network.create(network)
            manifest = json.loads((self.config.guest_artifacts / "manifest.json").read_text())
            config = {
                "boot-source": {
                    "kernel_image_path": "/artifacts/kernel",
                    "initrd_path": "/artifacts/initrd",
                    "boot_args": manifest["boot_args"]
                    + f" ip={network.guest_ip}::{network.host_ip}:255.255.255.252::eth0:off",
                },
                "machine-config": {
                    "vcpu_count": spec.vcpus,
                    "mem_size_mib": spec.memory_mib,
                    "smt": False,
                },
                "drives": [
                    {
                        "drive_id": "store",
                        "path_on_host": "/artifacts/store.img",
                        "is_root_device": False,
                        "is_read_only": True,
                    },
                    {
                        "drive_id": "environment",
                        "path_on_host": "/disks/environment.img",
                        "is_root_device": False,
                        "is_read_only": False,
                    },
                    {
                        "drive_id": "workspace",
                        "path_on_host": "/disks/workspace.img",
                        "is_root_device": False,
                        "is_read_only": False,
                    },
                ],
                "network-interfaces": [
                    {"iface_id": "eth0", "host_dev_name": network.tap, "guest_mac": network.mac}
                ],
                "vsock": {"guest_cid": 3, "uds_path": "/sockets/vsock.sock"},
            }
            atomic_json(base / "firecracker.json", config)
            os.chmod(base / "firecracker.json", 0o644)
            properties = [
                f"User={self.config.vm_user}",
                f"Group={self.config.vm_user}",
                f"RootDirectory={jail}",
                "MountAPIVFS=yes",
                "PrivateTmp=yes",
                "PrivatePIDs=yes",
                "ProtectSystem=strict",
                "ProtectHome=yes",
                "NoNewPrivileges=yes",
                "CapabilityBoundingSet=",
                "RestrictSUIDSGID=yes",
                "LockPersonality=yes",
                "ProtectKernelTunables=yes",
                "ProtectKernelModules=yes",
                "ProtectControlGroups=yes",
                "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                "DevicePolicy=closed",
                "DeviceAllow=/dev/kvm rw",
                "DeviceAllow=/dev/net/tun rw",
                "TasksMax=128",
                f"MemoryMax={spec.memory_mib + 512}M",
                f"CPUQuota={spec.vcpus * 100}%",
                "KillMode=control-group",
                "BindsTo=nftables.service",
                "After=nftables.service",
                "TimeoutStopSec=30",
                "UMask=0007",
                "StandardOutput=journal",
                "StandardError=journal",
                "BindReadOnlyPaths=/nix/store",
                "BindPaths=/dev/kvm /dev/net/tun",
                f"BindPaths={sockets}:/sockets",
                f"BindReadOnlyPaths={base / 'firecracker.json'}:/firecracker.json",
                f"BindReadOnlyPaths={self.config.guest_artifacts.resolve()}:/artifacts",
            ]
            for domain in ("environment", "workspace"):
                fd = self._open_disk(spec.project_id, domain)
                descriptors.append(fd)
                properties.append(f"BindPaths=/proc/{os.getpid()}/fd/{fd}:/disks/{domain}.img")
            command = [
                "systemd-run",
                f"--unit={unit_name(spec.execution_id)}",
                "--collect",
                "--service-type=exec",
            ]
            for prop in properties:
                command.extend(("--property", prop))
            command.extend(
                (
                    self.config.firecracker,
                    "--config-file",
                    "/firecracker.json",
                    "--api-sock",
                    "/sockets/firecracker.sock",
                )
            )
            await self.runner.run(*command, timeout=45)
            async with asyncio.timeout(15):
                while not handle.vsock_path.exists():
                    if not await self.active(spec.execution_id):
                        raise RuntimeError("Firecracker exited; inspect its systemd journal")
                    await asyncio.sleep(0.1)
            logger.info(
                "vm_started",
                extra={"execution_id": spec.execution_id, "project_id": spec.project_id},
            )
            return handle
        except BaseException:
            await self.stop(spec.execution_id)
            raise
        finally:
            for fd in descriptors:
                os.close(fd)

    async def stop(self, execution_id: str) -> None:
        key = str(UUID(execution_id))
        base = self.config.runtime_dir / key
        api_socket = base / "sockets/firecracker.sock"
        if api_socket.exists() and await self.active(key):
            try:
                async with httpx.AsyncClient(
                    transport=httpx.AsyncHTTPTransport(uds=str(api_socket)), timeout=3
                ) as client:
                    await client.put(
                        "http://localhost/actions", json={"action_type": "SendCtrlAltDel"}
                    )
                async with asyncio.timeout(30):
                    while await self.active(key):  # noqa: ASYNC110 - external systemd state
                        await asyncio.sleep(0.25)
            except (httpx.HTTPError, TimeoutError):
                logger.warning("vm_graceful_stop_timed_out", extra={"execution_id": key})
        forced = await self.active(key)
        if forced:
            # A VMM running as PID 1 in its private namespace can ignore SIGTERM.
            # The graceful deadline has expired; terminate its whole cgroup now.
            await self.runner.run(
                "systemctl", "kill", "--signal=SIGKILL", unit_name(key), timeout=10, check=False
            )
        await self.runner.run("systemctl", "stop", unit_name(key), timeout=45, check=False)
        if await self.active(key):
            raise RuntimeError("VM did not stop; disks remain leased")
        record_path = base / "record.json"
        if record_path.exists():
            record = Record.model_validate_json(record_path.read_text())
            await self.network.remove(record.network)
            await durable_io(shutil.rmtree, base)
        logger.info("vm_stopped", extra={"execution_id": key, "forced": forced})

    async def export_workspace(
        self, project_id: str, operation: str = "archive", path: str = "", allow_large: bool = False
    ) -> dict[str, Any]:
        """Pin both image and output descriptors before entering the worker namespace."""
        disk_fd = self._open_disk(project_id, "workspace")
        state_fd: int | None = None
        directory_fd: int | None = None
        output_fd: int | None = None
        process: asyncio.subprocess.Process | None = None
        export_id = uuid4().hex
        name = (
            export_id
            + {"archive": ".zip", "tree": ".json", "file": ".bin", "download": ".bin"}[operation]
        )
        try:
            state_fd = os.open(self.config.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir("exports", mode=0o700, dir_fd=state_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(
                "exports", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=state_fd
            )
            os.fchown(directory_fd, self.daemon_uid, self.vm_gid)
            os.fchmod(directory_fd, 0o700)
            output_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchown(output_fd, self.daemon_uid, self.vm_gid)
            process = await asyncio.create_subprocess_exec(
                "unshare",
                "--mount",
                "--propagation",
                "private",
                sys.executable,
                "-m",
                "tokendrain.storage.export",
                str(disk_fd),
                str(output_fd),
                operation,
                path,
                "1" if allow_large else "0",
                pass_fds=(disk_fd, output_fd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(120):
                stdout, _ = await process.communicate()
            if process.returncode:
                raise RuntimeError("Read-only workspace export failed; inspect helper setup")
            result = json.loads(stdout)
            if "error" in result:
                os.unlink(name, dir_fd=directory_fd)
                return dict(result)
            os.fsync(output_fd)
            return {"export_id": export_id, **result}
        except BaseException:
            if process and process.returncode is None:
                process.kill()
                await process.wait()
            if directory_fd is not None and output_fd is not None:
                os.unlink(name, dir_fd=directory_fd)
            raise
        finally:
            for descriptor in (output_fd, directory_fd, state_fd, disk_fd):
                if descriptor is not None:
                    os.close(descriptor)

    async def dispatch(self, request: Request) -> object:
        if request.operation == "diagnostics":
            # These probes neither create nor reconcile resources, and need not
            # wait for a slow VM boot or shutdown holding the lifecycle lock.
            if request.payload:
                raise ValueError("diagnostics does not accept arguments")
            checks = await asyncio.to_thread(
                inspect_vm_checks,
                self.config.guest_artifacts,
                self.config.firecracker,
                scope="helper",
            )
            return [check.model_dump(mode="json") for check in checks]
        async with self._lock:
            if request.operation == "workspace_export":
                if (
                    not {"project_id"}
                    <= set(request.payload)
                    <= {"project_id", "operation", "path", "allow_large"}
                ):
                    raise ValueError("Invalid workspace operation arguments")
                operation = request.payload.get("operation", "archive")
                if operation not in {"tree", "file", "download", "archive"}:
                    raise ValueError("Unknown workspace operation")
                path = workspace_path(request.payload.get("path", ""))
                allow_large = request.payload.get("allow_large", False)
                if not isinstance(allow_large, bool):
                    raise ValueError("allow_large must be boolean")
                project_id = str(UUID(str(request.payload["project_id"])))
                if any(
                    str(UUID(record.spec.project_id)) == project_id for record in self.records()
                ):
                    raise ValueError("Stop the project before downloading its workspace")
                return await self.export_workspace(project_id, operation, path, allow_large)
            if request.operation == "start":
                return (await self.start(VmSpec.model_validate(request.payload))).model_dump(
                    mode="json"
                )
            if request.operation == "stop":
                if set(request.payload) != {"execution_id"}:
                    raise ValueError("stop accepts only execution_id")
                await self.stop(str(request.payload["execution_id"]))
                return None
            if request.payload:
                raise ValueError("list does not accept arguments")
            handles = []
            for record in self.records():
                if await self.active(record.spec.execution_id):
                    handles.append(record.handle.model_dump(mode="json"))
                else:
                    await self.stop(record.spec.execution_id)
            return handles

    async def connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id: str | None = None
        try:
            peer = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            if uid not in (0, self.daemon_uid):
                raise PermissionError("Unauthorized infrastructure client")
            async with asyncio.timeout(180):
                request = Request.model_validate_json(await reader.readline())
                request_id = request.id
                result = await self.dispatch(request)
                writer.write(json.dumps({"id": request_id, "result": result}).encode() + b"\n")
                await writer.drain()
        except Exception as exc:
            logger.warning(
                "infrastructure_request_failed",
                extra={"error_type": type(exc).__name__, "request_id": request_id},
            )
            with contextlib.suppress(ConnectionError):
                writer.write(
                    json.dumps({"id": request_id, "error": str(exc)[:2048]}).encode() + b"\n"
                )
                await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    async def refresh_policy(self) -> None:
        await self.network.refresh_policy([item.network for item in self.records()])

    async def watch_routes(self) -> None:
        async for _ in self.network.route_changes():
            await self.refresh_policy()

    async def watch_firewall(self) -> None:
        while True:
            await asyncio.sleep(2)
            # A NixOS nftables reload creates empty source sets and a default-drop
            # readiness chain. Rebuild them atomically before restoring egress.
            await self.refresh_policy()

    async def serve(self) -> None:
        self.config.runtime_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.config.socket_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        self.config.socket_path.unlink(missing_ok=True)
        await self.refresh_policy()
        connections: set[asyncio.Task[None]] = set()

        def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.create_task(self.connection(reader, writer))
            connections.add(task)
            task.add_done_callback(connections.discard)

        server = await asyncio.start_unix_server(accept, str(self.config.socket_path), limit=65536)
        os.chown(self.config.socket_path, self.daemon_uid, -1)
        os.chmod(self.config.socket_path, 0o600)
        await notify_ready()
        owner = asyncio.current_task()
        assert owner is not None
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, owner.cancel)
        async with asyncio.TaskGroup() as group:
            watchers = [
                group.create_task(self.watch_routes()),
                group.create_task(self.watch_firewall()),
            ]
            try:
                await server.serve_forever()
            finally:
                server.close()
                for task in watchers:
                    task.cancel()
                for task in tuple(connections):
                    task.cancel()
                await asyncio.gather(*watchers, *tuple(connections), return_exceptions=True)
                with contextlib.suppress(Exception):
                    await self.network.deny_egress()
                await server.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    configure_logging()
    config = HelperConfig.model_validate_json(args.config.read_text())
    try:
        asyncio.run(InfrastructureService(config).serve())
    except asyncio.CancelledError:
        logger.info("helper_stopped")


if __name__ == "__main__":
    main()
