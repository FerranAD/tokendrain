"""Runs as root inside the disposable NixOS test VM, using no real credentials."""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


def helper(operation: str, payload: dict[str, Any]) -> Any:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(120)
        connection.connect("/run/tokendrain/helper.sock")
        connection.sendall(
            json.dumps(
                {"version": 1, "id": "test", "operation": operation, "payload": payload}
            ).encode()
            + b"\n"
        )
        result = json.loads(connection.makefile("rb").readline())
        assert "error" not in result, result
        return result["result"]


class Guest:
    def __init__(self, path: str) -> None:
        deadline = time.monotonic() + 90
        while True:
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.socket.settimeout(5)
            try:
                self.socket.connect(path)
                self.socket.sendall(b"CONNECT 4050\n")
                line = b""
                while not line.endswith(b"\n"):
                    part = self.socket.recv(1)
                    if not part:
                        raise ConnectionError("vsock rejected connection")
                    line += part
                if not line.startswith(b"OK "):
                    raise ConnectionError(line)
                self.socket.settimeout(90)
                self.id = 0
                self.rpc("ping")
                return
            except (OSError, ConnectionError):
                self.socket.close()
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    def exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            part = self.socket.recv(count - len(data))
            if not part:
                raise ConnectionError("guest disconnected")
            data += part
        return data

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.id += 1
        data = json.dumps(
            {"version": 1, "id": self.id, "method": method, "params": params or {}}
        ).encode()
        self.socket.sendall(struct.pack("!I", len(data)) + data)
        while True:
            message = json.loads(self.exact(struct.unpack("!I", self.exact(4))[0]))
            if message.get("id") == self.id:
                assert "error" not in message, message
                return message["result"]

    def codex(self) -> None:
        self.rpc(
            "credentials_set",
            {
                "openai": {
                    "mode": "siwc",
                    "access_token": "test-token-no-real-provider",
                    "expires_at": time.time() + 3600,
                }
            },
        )
        self.rpc("codex_start")

    def command(self, script: str) -> str:
        result = self.rpc(
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
        return result["stdout"]

    def shutdown(self) -> None:
        self.rpc("shutdown")
        self.socket.close()


def project() -> str:
    value = str(uuid4())
    path = Path("/var/lib/tokendrain/projects") / value
    path.mkdir(parents=True)
    for domain in ("environment", "workspace"):
        image = path / f"{domain}.img"
        with image.open("wb") as stream:
            stream.truncate(1024**3)
        subprocess.run(["mkfs.ext4", "-q", "-F", str(image)], check=True)
    return value


def boot(project_id: str) -> tuple[dict[str, Any], Guest]:
    handle = helper(
        "start",
        {"execution_id": str(uuid4()), "project_id": project_id, "vcpus": 2, "memory_mib": 1536},
    )
    guest = Guest(handle["vsock_path"])
    guest.codex()
    return handle, guest


def wait_lan_policy(prefix: str, present: bool) -> None:
    deadline = time.monotonic() + 5
    while True:
        rules = subprocess.check_output(["nft", "list", "set", "inet", "tokendrain", "lan4"])
        if (prefix.encode() in rules) == present:
            return
        assert time.monotonic() < deadline, rules
        time.sleep(0.1)


def main() -> None:
    handles: list[dict[str, Any]] = []
    first_project = project()
    try:
        first, guest = boot(first_project)
        handles.append(first)
        guest.command("printf workspace > /workspace/sentinel; printf environment > /root/sentinel")
        guest.command("curl --fail --connect-timeout 5 http://8.8.8.8:8080/ >/dev/null")
        # The existing /32 route remains usable, but its new connected LAN must
        # be denied immediately after route-policy reconciliation.
        subprocess.run(["ip", "address", "add", "8.8.8.1/24", "dev", "eth1"], check=True)
        wait_lan_policy("8.8.8.0/24", True)
        guest.command("! curl --fail --connect-timeout 2 http://8.8.8.8:8080/ >/dev/null 2>&1")
        subprocess.run(["ip", "address", "delete", "8.8.8.1/24", "dev", "eth1"], check=True)
        wait_lan_policy("8.8.8.0/24", False)
        guest.command("curl --fail --connect-timeout 5 http://8.8.8.8:8080/ >/dev/null")
        for address in ("100.127.0.1", "192.168.1.2", "9.9.9.9", "93.184.215.2"):
            guest.command(
                f"! curl --fail --connect-timeout 2 http://{address}:8080/ >/dev/null 2>&1"
            )
        # Reload must leave an empty policy fail-closed until the helper refreshes it.
        subprocess.run(["systemctl", "stop", "tokendrain-helper"], check=True)
        subprocess.run(["systemctl", "reload", "nftables"], check=True)
        guest.command("! curl --fail --connect-timeout 2 http://8.8.8.8:8080/ >/dev/null 2>&1")
        guest.command("! curl --fail --connect-timeout 2 http://93.184.215.2:8080/ >/dev/null 2>&1")
        subprocess.run(["systemctl", "start", "tokendrain-helper"], check=True)
        guest.command("curl --fail --connect-timeout 5 http://8.8.8.8:8080/ >/dev/null")
        guest.command("! curl --fail --connect-timeout 2 http://93.184.215.2:8080/ >/dev/null 2>&1")
        second, other = boot(project())
        handles.append(second)
        other.command(
            "systemd-run --unit test-http /run/current-system/sw/bin/python3 -m http.server 9090"
        )
        other.command(
            "curl --retry 5 --retry-connrefused --retry-delay 1 --retry-max-time 5 "
            "--fail http://127.0.0.1:9090/ >/dev/null"
        )
        guest.command("! curl --fail --connect-timeout 2 http://100.127.0.6:9090/ >/dev/null 2>&1")
        assert len(helper("list", {})) == 2
        other.shutdown()
        helper("stop", {"execution_id": second["execution_id"]})
        handles.remove(second)
        guest.shutdown()
        helper("stop", {"execution_id": first["execution_id"]})
        handles.remove(first)
        final, guest = boot(first_project)
        handles.append(final)
        assert guest.command("cat /workspace/sentinel /root/sentinel") == "workspaceenvironment"
        # Removing the firewall must stop its dependent VMM before policy goes
        # away; restarting the helper then reconciles the dead runtime record.
        subprocess.run(["systemctl", "stop", "nftables"], check=True, timeout=60)
        guest.socket.close()
        unit = "tokendrain-vm-" + final["execution_id"].replace("-", "") + ".service"
        state = subprocess.check_output(["systemctl", "show", "-p", "ActiveState", "--value", unit])
        assert state.strip() in (b"inactive", b"failed"), state
        subprocess.run(["systemctl", "start", "nftables", "tokendrain-helper"], check=True)
        assert helper("list", {}) == []
        handles.remove(final)
        helper("stop", {"execution_id": final["execution_id"]})  # Idempotent cleanup.
        assert helper("list", {}) == []
        print("Real helper, guest control, persistence and network isolation verified.")
    finally:
        for handle in handles:
            helper("stop", {"execution_id": handle["execution_id"]})


if __name__ == "__main__":
    main()
