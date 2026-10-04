"""Opt-in real Firecracker test: KVM and built guest artifacts, no root required.

TOKENDRAIN_TEST_GUEST_ARTIFACTS=$(nix build .#guest-artifacts --no-link --print-out-paths) \
  nix develop -c pytest tests/integration/test_kvm_guest.py -m kvm -v
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tokendrain.protocol import GuestClient
from tokendrain.storage import FileProjectStorage

pytestmark = [
    pytest.mark.kvm,
    pytest.mark.skipif(
        not os.environ.get("TOKENDRAIN_TEST_GUEST_ARTIFACTS") or not Path("/dev/kvm").exists(),
        reason="set TOKENDRAIN_TEST_GUEST_ARTIFACTS on a KVM host to run",
    ),
]


@asynccontextmanager
async def boot(
    directory: Path, artifacts: Path, environment: Path, workspace: Path
) -> AsyncIterator[GuestClient]:
    binary = shutil.which("firecracker")
    if not binary:
        pytest.fail("Firecracker is missing; use nix develop")
    await asyncio.to_thread(directory.mkdir)
    manifest = json.loads((artifacts / "manifest.json").read_text())
    config = {
        "boot-source": {
            "kernel_image_path": str(artifacts / "kernel"),
            "initrd_path": str(artifacts / "initrd"),
            "boot_args": manifest["boot_args"],
        },
        "machine-config": {"vcpu_count": 2, "mem_size_mib": 1536, "smt": False},
        "drives": [
            {
                "drive_id": "store",
                "path_on_host": str(artifacts / "store.img"),
                "is_root_device": False,
                "is_read_only": True,
            },
            {
                "drive_id": "environment",
                "path_on_host": str(environment),
                "is_root_device": False,
                "is_read_only": False,
            },
            {
                "drive_id": "workspace",
                "path_on_host": str(workspace),
                "is_root_device": False,
                "is_read_only": False,
            },
        ],
        "vsock": {"guest_cid": 3, "uds_path": str(directory / "vsock.sock")},
    }
    config_path = directory / "firecracker.json"
    config_path.write_text(json.dumps(config))
    with (directory / "console.log").open("wb") as console:
        process = await asyncio.create_subprocess_exec(
            binary,
            "--config-file",
            str(config_path),
            "--api-sock",
            str(directory / "api.sock"),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=console,
            stderr=console,
        )
        guest = None
        try:
            async with asyncio.timeout(120):
                while guest is None:
                    if process.returncode is not None:
                        raise RuntimeError("Firecracker exited during boot")
                    try:
                        guest = await GuestClient.connect(directory / "vsock.sock", timeout=2)
                        await guest.request("ping")
                    except (ConnectionError, OSError, TimeoutError):
                        guest = None
                        await asyncio.sleep(0.2)
            yield guest
            await guest.request("shutdown")
            await asyncio.wait_for(process.wait(), 45)
            assert process.returncode == 0
        except BaseException:
            print((directory / "console.log").read_text(errors="replace"))
            raise
        finally:
            if guest:
                with contextlib.suppress(Exception):
                    await guest.close()
            if process.returncode is None:
                process.kill()
                await process.wait()


async def command(guest: GuestClient, script: str) -> dict[str, Any]:
    result = await guest.request(
        "command/exec",
        {
            "command": ["/bin/sh", "-c", script],
            "cwd": "/workspace",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        },
    )
    assert result["exitCode"] == 0, result
    return result


async def start_codex(guest: GuestClient) -> None:
    await guest.credentials_set(
        {
            "openai": {
                "mode": "chatgpt",
                "access_token": "test-only-no-network",
                "expires_at": time.time() + 3600,
            }
        }
    )
    await guest.request("codex_start")


async def test_real_guest_vsock_and_persistent_disks() -> None:
    artifacts = Path(os.environ["TOKENDRAIN_TEST_GUEST_ARTIFACTS"])
    # Keep UDS paths below Linux's 108-byte sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="tdvm-", dir="/tmp") as temporary:
        directory = Path(temporary)
        storage = FileProjectStorage(directory, disk_gib=1)
        project = str(uuid4())
        info = await storage.create(project)
        async with boot(directory / "first", artifacts, info.environment, info.workspace) as guest:
            assert (await guest.request("guest_info"))["workspace"] == "/workspace"
            await start_codex(guest)
            await command(
                guest,
                "set -e; printf workspace-persisted > /workspace/sentinel; "
                "printf environment-persisted > /root/sentinel; "
                "nix-store --add /root/sentinel > /workspace/store-path; "
                "printf ephemeral > /run/tokendrain/ephemeral; sync",
            )
        async with boot(directory / "second", artifacts, info.environment, info.workspace) as guest:
            await start_codex(guest)
            result = await command(
                guest,
                'set -e; nix-store --check-validity "$(cat /workspace/store-path)"; '
                "cat /workspace/sentinel /root/sentinel; test ! -e /run/tokendrain/ephemeral",
            )
            assert result["stdout"] == "workspace-persistedenvironment-persisted"
