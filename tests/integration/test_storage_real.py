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


async def test_real_ext4_clone_and_grow_preserves_machine(tmp_path: Path) -> None:
    runner = CommandRunner()
    base = tmp_path / "base.img"
    with base.open("wb") as stream:
        stream.truncate(128 * 1024**2)
    await runner.run("mkfs.ext4", "-q", "-F", str(base))
    seed = tmp_path / "seed.txt"
    seed.write_text("persistent machine state\n")
    await runner.run("debugfs", "-w", "-R", "mkdir /workspace", str(base))
    await runner.run("debugfs", "-w", "-R", f"write {seed} /workspace/sentinel", str(base))
    storage = FileProjectStorage(tmp_path, disk_gib=1, base_image=base)
    project = str(uuid4())
    info = await storage.create(project)
    await storage.resize(project, 2)
    assert (await storage.usage(project)).virtual_size_bytes == 2 * 1024**3
    result = await runner.run("e2fsck", "-fn", str(info.vm_path), check=False)
    assert result.returncode == 0
    result = await runner.run("debugfs", "-R", "cat /workspace/sentinel", str(info.vm_path))
    assert result.stdout == b"persistent machine state\n"
    with pytest.raises(ValueError, match="Shrinking"):
        await storage.resize(project, 1)
