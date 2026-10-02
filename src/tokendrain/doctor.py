"""Read-only host diagnostics; call through to_thread from asynchronous code."""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tokendrain.config import Settings
from tokendrain.credentials.store import load_master_key


class DoctorCheck(BaseModel):
    name: str
    ok: bool
    message: str


def inspect_database(path: Path) -> DoctorCheck:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            deadline = time.monotonic() + 3
            connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            result = connection.execute("PRAGMA quick_check").fetchone()
            revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
            return DoctorCheck(
                name="database",
                ok=bool(result and result[0] == "ok" and revision),
                message=f"SQLite integrity {result}; migration {revision}",
            )
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as error:
        return DoctorCheck(name="database", ok=False, message=str(error))


def inspect_checks(settings: Settings) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    try:
        fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
        try:
            version = fcntl.ioctl(fd, 0xAE00)
            checks.append(DoctorCheck(name="kvm", ok=version == 12, message=f"KVM API {version}"))
        finally:
            os.close(fd)
    except OSError as error:
        checks.append(
            DoctorCheck(
                name="kvm", ok=False, message=f"{error.strerror}; helper has separate device access"
            )
        )
    tun = Path("/dev/net/tun")
    checks.append(
        DoctorCheck(
            name="tun", ok=tun.exists() and stat.S_ISCHR(tun.stat().st_mode), message=str(tun)
        )
    )
    for name in ("firecracker", "ip", "nft", "mkfs.ext4", "e2fsck", "resize2fs", "nix", "codex"):
        executable = shutil.which(name)
        checks.append(
            DoctorCheck(
                name=name,
                ok=executable is not None,
                message=executable or "Not in this process PATH",
            )
        )
    helper = settings.helper_socket
    checks.append(DoctorCheck(name="helper_socket", ok=helper.is_socket(), message=str(helper)))
    artifacts = settings.guest_artifacts
    missing = [
        name
        for name in ("kernel", "initrd", "store.img", "manifest.json")
        if artifacts is None or not os.access(artifacts / name, os.R_OK)
    ]
    checks.append(
        DoctorCheck(
            name="guest_artifacts",
            ok=not missing,
            message=f"Missing/unreadable: {', '.join(missing)}" if missing else str(artifacts),
        )
    )
    key = settings.master_key_file
    if key is None:
        credentials_directory = os.environ.get("CREDENTIALS_DIRECTORY")
        key = (
            Path(credentials_directory) / "master-key"
            if credentials_directory
            else Path("/var/lib/tokendrain-keys/master.key")
        )
    try:
        load_master_key(key)
        checks.append(
            DoctorCheck(name="master_key", ok=True, message=f"Protected valid key: {key}")
        )
    except (OSError, ValueError) as error:
        checks.append(DoctorCheck(name="master_key", ok=False, message=f"{key}: {error}"))
    writable = os.access(settings.state_dir, os.W_OK)
    checks.append(DoctorCheck(name="state_directory", ok=writable, message=str(settings.state_dir)))
    if settings.state_dir.is_dir():
        free = shutil.disk_usage(settings.state_dir).free
        checks.append(
            DoctorCheck(
                name="disk_space",
                ok=free >= 512 * 1024**2,
                message=f"{free // 1024**2} MiB available; images are sparse",
            )
        )
    forwarding = Path("/proc/sys/net/ipv4/ip_forward")
    enabled = forwarding.exists() and forwarding.read_text().strip() == "1"
    checks.append(
        DoctorCheck(
            name="ipv4_forwarding",
            ok=enabled,
            message="enabled" if enabled else "disabled; module enables this",
        )
    )
    cgroups = Path("/sys/fs/cgroup/cgroup.controllers")
    controllers = cgroups.read_text().split() if cgroups.exists() else []
    checks.append(
        DoctorCheck(
            name="cgroup_v2",
            ok={"cpu", "memory", "pids"}.issubset(controllers),
            message="Available controllers: " + ", ".join(controllers),
        )
    )
    checks.append(inspect_database(settings.database_path))
    return checks


def inspect_system(settings: Settings) -> list[dict[str, Any]]:
    return [check.model_dump() for check in inspect_checks(settings)]
