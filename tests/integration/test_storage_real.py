"""Exercise real ext4 tools on ordinary files; no mounts or root access needed."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from tokendrain.storage import FileProjectStorage
from tokendrain.vm.commands import CommandRunner

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("mkfs.ext4", "debugfs", "e2fsck", "resize2fs")),
    reason="requires e2fsprogs (available in nix develop)",
)


async def test_real_ext4_snapshot_restore_resize_and_reset(tmp_path: Path) -> None:
    storage = FileProjectStorage(tmp_path, disk_gib=1)
    project = str(uuid4())
    info = await storage.create(project)
    runner = CommandRunner()
    seed = tmp_path / "seed.txt"
    seed.write_text("persisted source tree\n")
    await runner.run("debugfs", "-w", "-R", f"write {seed} /sentinel.txt", str(info.workspace))
    await runner.run("debugfs", "-w", "-R", f"write {seed} /tool-cache.txt", str(info.environment))
    snapshot = await storage.snapshot(project)
    await runner.run("debugfs", "-w", "-R", "rm /sentinel.txt", str(info.workspace))
    await storage.restore(project, snapshot.id, "workspace")
    result = await runner.run("debugfs", "-R", "cat /sentinel.txt", str(info.workspace))
    assert result.stdout == b"persisted source tree\n"
    await storage.resize(project, "workspace", 2)
    assert (await storage.usage(project)).workspace_bytes == 2 * 1024**3
    result = await runner.run("e2fsck", "-fn", str(info.workspace), check=False)
    assert result.returncode == 0
    result = await runner.run("debugfs", "-R", "cat /sentinel.txt", str(info.workspace))
    assert result.stdout == b"persisted source tree\n"
    await storage.reset_environment(project)
    result = await runner.run("debugfs", "-R", "stat /tool-cache.txt", str(info.environment))
    assert b"File not found" in result.stderr
    result = await runner.run("debugfs", "-R", "cat /sentinel.txt", str(info.workspace))
    assert result.stdout == b"persisted source tree\n"
