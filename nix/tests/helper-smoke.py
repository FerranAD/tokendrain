"""Runs as root inside the disposable NixOS test VM, using no real credentials."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import socket
import struct
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4


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
                    "mode": "chatgpt",
                    "access_token": synthetic_access_token(),
                    "expires_at": time.time() + 3600,
                    "account_id": "test-account",
                    "plan_type": "plus",
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
    # The helper's trusted artifact location is supplied by the test, never by clients.
    artifacts = Path(os.environ["TOKENDRAIN_TEST_GUEST_ARTIFACTS"])
    image = path / "vm.img"
    subprocess.run(
        ["cp", "--reflink=auto", "--sparse=always", str(artifacts / "base.img"), str(image)],
        check=True,
    )
    image.chmod(0o600)
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


def use_control_artifacts(artifacts: Path) -> None:
    """Simulate a host upgrade with root-owned helper configuration, never client paths."""
    unit = Path("/etc/systemd/system/tokendrain-helper.service").read_text()
    match = re.search(r"^ExecStart=(.*)$", unit, re.M)
    assert match
    command = shlex.split(match[1])
    config_index = command.index("--config") + 1
    config = json.loads(Path(command[config_index]).read_text())
    config["guest_artifacts"] = str(artifacts)
    config_path = Path("/run/test-helper.json")
    config_path.write_text(json.dumps(config))
    command[config_index] = str(config_path)
    dropin = Path("/run/systemd/system/tokendrain-helper.service.d")
    dropin.mkdir(parents=True, exist_ok=True)
    (dropin / "test.conf").write_text(
        "[Service]\nExecStart=\nExecStart=" + shlex.join(command) + "\n"
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "restart", "tokendrain-helper"], check=True)


def main() -> None:
    diagnostics = {check["name"]: check for check in helper("diagnostics", {})}
    for name in (
        "kvm",
        "tun",
        "firecracker",
        "ip",
        "nft",
        "guest_artifacts",
        "cgroup_v2",
        "ipv4_forwarding",
    ):
        assert diagnostics[name]["ok"], diagnostics[name]
        assert diagnostics[name]["scope"] == "helper", diagnostics[name]
    handles: list[dict[str, Any]] = []
    first_project = project()
    try:
        first, guest = boot(first_project)
        handles.append(first)
        first_control = guest.rpc("guest_info")
        assert first_control["control_version"] == "0.1.0", first_control
        guest.command(
            "set -e; printf workspace > /workspace/sentinel; "
            "mkdir -p /root/.config /opt; printf home > /root/.config/sentinel; "
            "printf opt > /opt/sentinel; printf config > /etc/td-sentinel; "
            "mkdir -p /root/test-tool/bin; "
            "printf '#!/bin/sh\\nprintf persistent-tool\\n' "
            "> /root/test-tool/bin/td-test-tool; "
            "chmod +x /root/test-tool/bin/td-test-tool; "
            'nix-env -i "$(nix-store --add /root/test-tool)"; '
            "mkdir -p /usr/local/bin; printf ephemeral > /run/tokendrain/ephemeral; "
            "test -s /run/tokendrain/secrets.json; "
            "findmnt -n -o FSTYPE /run | grep tmpfs"
        )
        for operation in ("tree", "file", "archive"):
            payload = {"project_id": first_project, "operation": operation}
            if operation == "file":
                payload["path"] = "sentinel"
            try:
                helper("workspace_export", payload)
                raise AssertionError("Active VM workspace was exported")
            except AssertionError as error:
                assert "Stop the project" in str(error), error
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
        exported = helper("workspace_export", {"project_id": first_project})
        archive_path = Path("/var/lib/tokendrain/exports") / (exported["export_id"] + ".zip")
        with zipfile.ZipFile(archive_path) as archive:
            assert archive.read("sentinel") == b"workspace"
        archive_path.unlink()
        for operation, suffix in [("tree", ".json"), ("file", ".bin")]:
            payload = {"project_id": first_project, "operation": operation}
            if operation == "file":
                payload["path"] = "sentinel"
            exported = helper("workspace_export", payload)
            export_path = Path("/var/lib/tokendrain/exports") / (exported["export_id"] + suffix)
            if operation == "tree":
                assert "sentinel" in [
                    entry["name"] for entry in json.loads(export_path.read_text())["entries"]
                ]
            else:
                assert export_path.read_bytes() == b"workspace"
            export_path.unlink()
        # Grow the same root filesystem offline, then boot it under current control B.
        image = Path("/var/lib/tokendrain/projects") / first_project / "vm.img"
        checked = subprocess.run(["e2fsck", "-pf", str(image)])
        assert checked.returncode in (0, 1)
        with image.open("r+b") as stream:
            stream.truncate(image.stat().st_size + 1024**3)
        subprocess.run(["resize2fs", str(image)], check=True)
        use_control_artifacts(Path(os.environ["TOKENDRAIN_TEST_UPGRADE_ARTIFACTS"]))
        final, guest = boot(first_project)
        handles.append(final)
        updated_control = guest.rpc("guest_info")
        assert updated_control["control_version"] == "control-test-B", updated_control
        assert updated_control["guestd"] != first_control["guestd"]
        assert updated_control["codex_executable"].startswith("/nix/store/")
        assert (
            guest.command(
                "set -e; cat /workspace/sentinel /root/.config/sentinel "
                "/opt/sentinel /etc/td-sentinel; "
                "td-test-tool; test ! -e /run/tokendrain/ephemeral"
            )
            == "workspacehomeoptconfigpersistent-tool"
        )
        guest.command("nix-store --check-validity " + first_control["guestd"])
        # A persistent project-installed Codex must not replace the managed binary.
        guest.command(
            "printf '#!/bin/sh\\nexit 99\\n' > /usr/local/bin/codex; chmod +x /usr/local/bin/codex"
        )
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
