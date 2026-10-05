"""Opt-in real Firecracker test: KVM and built guest artifacts, no root required.

TOKENDRAIN_TEST_GUEST_ARTIFACTS=$(nix build .#guest-artifacts --no-link --print-out-paths) \
  nix develop -c pytest tests/integration/test_kvm_guest.py -m kvm -v
"""

from __future__ import annotations

import asyncio
import base64
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


def synthetic_access_token() -> str:
    # Synthetic claims only; no provider/account access is performed by command/exec.
    claims = {
        "sub": "test-user",
        "email": "test@example.invalid",
        "exp": time.time() + 3600,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "test-account",
            "chatgpt_plan_type": "plus",
        },
    }

    def encode(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return encode({"alg": "RS256"}) + "." + encode(claims) + ".dGVzdA"


pytestmark = [
    pytest.mark.kvm,
    pytest.mark.skipif(
        not os.environ.get("TOKENDRAIN_TEST_GUEST_ARTIFACTS") or not Path("/dev/kvm").exists(),
        reason="set TOKENDRAIN_TEST_GUEST_ARTIFACTS on a KVM host to run",
    ),
]


@asynccontextmanager
async def boot(directory: Path, artifacts: Path, vm: Path) -> AsyncIterator[GuestClient]:
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
                "drive_id": "vm",
                "path_on_host": str(vm),
                "is_root_device": True,
                "is_read_only": False,
            },
            {
                "drive_id": "control",
                "path_on_host": str(artifacts / "control.img"),
                "is_root_device": False,
                "is_read_only": True,
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
        "codex_rpc_request",
        {
            "method": "command/exec",
            "params": {
                "command": ["/bin/sh", "-c", script],
                "cwd": "/workspace",
                "sandboxPolicy": {"type": "dangerFullAccess"},
            },
        },
    )
    assert result["exitCode"] == 0, result
    return result


async def start_codex(guest: GuestClient, secrets: dict[str, str] | None = None) -> None:
    await guest.credentials_set(
        {
            "openai": {
                "mode": "chatgpt",
                "access_token": synthetic_access_token(),
                "expires_at": time.time() + 3600,
                "account_id": "test-account",
                "plan_type": "plus",
            },
            "secrets": secrets or {},
        }
    )
    await guest.request("codex_start")


async def test_persistent_machine_and_current_control() -> None:
    artifacts = Path(os.environ["TOKENDRAIN_TEST_GUEST_ARTIFACTS"])
    # Keep UDS paths below Linux's 108-byte sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="tdvm-", dir="/tmp") as temporary:
        directory = Path(temporary)
        storage = FileProjectStorage(directory, disk_gib=12, base_image=artifacts / "base.img")
        project = str(uuid4())
        info = await storage.create(project)
        async with boot(directory / "first", artifacts, info.vm_path) as guest:
            first_control = await guest.request("guest_info")
            assert first_control["workspace"] == "/workspace"
            assert first_control["control_version"] == "0.1.0"
            await start_codex(guest, {"TD_TEMPORARY": "synthetic-secret"})
            await command(
                guest,
                "set -e; printf workspace-persisted > /workspace/sentinel; "
                "mkdir -p /root/.config /opt; printf home > /root/.config/sentinel; "
                "printf opt > /opt/sentinel; printf config > /etc/tokendrain-sentinel; "
                "nix-store --add /root/.config/sentinel > /workspace/store-path; "
                "mkdir -p /root/test-tool/bin; "
                "printf '#!/bin/sh\\nprintf persistent-tool\\n' "
                "> /root/test-tool/bin/td-test-tool; "
                "chmod +x /root/test-tool/bin/td-test-tool; "
                'nix-env -i "$(nix-store --add /root/test-tool)"; '
                "printf '#!/bin/sh\\nexit 99\\n' > /root/test-tool/bin/codex; "
                "chmod +x /root/test-tool/bin/codex; "
                'nix-env -i "$(nix-store --add /root/test-tool)"; '
                "grep synthetic-secret /run/tokendrain/secrets.json >/dev/null; "
                "printf ephemeral > /run/tokendrain/ephemeral; sync",
            )
        updated = Path(os.environ.get("TOKENDRAIN_TEST_UPGRADE_ARTIFACTS", str(artifacts)))
        async with boot(directory / "second", updated, info.vm_path) as guest:
            current_control = await guest.request("guest_info")
            if updated != artifacts:
                assert current_control["control_version"] == "control-test-B"
                assert current_control["guestd"] != first_control["guestd"]
            assert current_control["codex_executable"].startswith("/nix/store/")
            await start_codex(guest)
            result = await command(
                guest,
                'set -e; nix-store --check-validity "$(cat /workspace/store-path)"; '
                "cat /workspace/sentinel /root/.config/sentinel /opt/sentinel "
                "/etc/tokendrain-sentinel; "
                "td-test-tool; test ! -e /run/tokendrain/ephemeral; "
                "! grep synthetic-secret /run/tokendrain/secrets.json; "
                "nix-store --check-validity " + first_control["guestd"],
            )
            assert result["stdout"] == "workspace-persistedhomeoptconfigpersistent-tool"
