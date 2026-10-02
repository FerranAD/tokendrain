from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest

from tokendrain.cli.admin import execute, parser
from tokendrain.config import Settings
from tokendrain.doctor import DoctorCheck, inspect_checks, inspect_service_checks
from tokendrain.orchestration.mock import MockVmBackend


def test_daemon_checks_do_not_probe_vm_devices_or_tools(tmp_path: Path, monkeypatch):
    queried: list[str] = []

    def executable(name: str) -> str | None:
        queried.append(name)
        assert name not in {"firecracker", "ip", "nft", "nix"}
        return "/tools/" + name

    def unexpected_vm_probe(*args, **kwargs):
        pytest.fail("The hardened daemon must not perform local VM probes")

    monkeypatch.setattr("tokendrain.doctor.shutil.which", executable)
    monkeypatch.setattr("tokendrain.doctor.inspect_vm_checks", unexpected_vm_probe)
    checks = inspect_checks(Settings(state_dir=tmp_path), scope="daemon")
    assert all(check.scope == "daemon" for check in checks)
    assert set(queried) == {"mkfs.ext4", "e2fsck", "resize2fs", "codex"}
    assert not {"kvm", "tun", "guest_artifacts", "nix"} & {check.name for check in checks}


@pytest.mark.parametrize("available", [True, False])
async def test_status_preserves_actual_helper_results(tmp_path: Path, available: bool):
    class Backend(MockVmBackend):
        async def diagnostics(self) -> list[DoctorCheck]:
            return [DoctorCheck(name="kvm", ok=available, message="KVM probe", scope="helper")]

    checks = {
        check.name: check
        for check in await inspect_service_checks(Settings(state_dir=tmp_path), Backend())
    }
    assert checks["helper"].ok
    assert checks["kvm"].ok is available
    assert checks["kvm"].scope == "helper"
    assert checks["database"].scope == "daemon"
    assert "nix" not in checks


async def test_status_reports_helper_failure_instead_of_daemon_kvm_failure(tmp_path: Path):
    class Backend(MockVmBackend):
        async def diagnostics(self) -> list[DoctorCheck]:
            raise ConnectionError("helper stopped")

    checks = {
        check.name: check
        for check in await inspect_service_checks(Settings(state_dir=tmp_path), Backend())
    }
    assert not checks["helper"].ok
    assert checks["helper"].scope == "helper"
    assert "ConnectionError" in checks["helper"].message
    assert "kvm" not in checks


@pytest.mark.parametrize("scope,name", [("helper", "kvm"), ("daemon", "database")])
async def test_host_doctor_does_not_hide_service_failures(tmp_path: Path, monkeypatch, scope, name):
    token_path = tmp_path / "admin-token"
    token_path.write_text("x" * 40)
    token_path.chmod(0o600)
    monkeypatch.setattr(
        "tokendrain.cli.admin.inspect_checks",
        lambda settings: [DoctorCheck(name="kvm", ok=True, message="Host probe")],
    )
    result = {
        "checks": [{"name": name, "ok": False, "message": "Failed in service", "scope": scope}]
    }
    output = io.StringIO()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=result))
    ) as client:
        status = await execute(
            parser().parse_args(["doctor"]), Settings(state_dir=tmp_path), client, output
        )
    assert status == 1
    assert "OK   kvm:" in output.getvalue()
    assert f"FAIL {scope}.{name}:" in output.getvalue()
