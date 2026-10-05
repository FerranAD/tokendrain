import zipfile
from pathlib import Path

from tokendrain.events import activity
from tokendrain.storage.export import archive_tree


def test_structured_activity_keeps_raw_protocol_and_hides_prompts():
    import json

    raw = {
        "type": "commandExecution",
        "command": "git push",
        "cwd": "/workspace",
        "status": "failed",
        "exitCode": 1,
        "durationMs": 977,
        "aggregatedOutput": "HTTP 403",
    }
    kind, message, data = activity(json.dumps(raw))
    assert kind == "command" and message == "git push"
    assert data["exitCode"] == 1 and data["raw"] == raw
    assert activity(json.dumps({"type": "userMessage", "content": []}))[0] == "execution.debug"


def test_workspace_archive_omits_symlink_escape_and_special_files(tmp_path: Path):
    import os

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "sub").mkdir()
    (root / "sub" / "a file.txt").write_text("Project work")
    (tmp_path / "private").write_text("Host secret")
    (root / "escape").symlink_to(tmp_path)
    (root / "secret").symlink_to(tmp_path / "private")
    os.mkfifo(root / "fifo")
    destination = tmp_path / "work.zip"
    archive_tree(root, destination)
    with zipfile.ZipFile(destination) as archive:
        assert set(archive.namelist()) == {"sub/", "sub/a file.txt"}
        assert archive.read("sub/a file.txt") == b"Project work"


def test_workspace_browse_preview_download_and_bounded_reads(tmp_path: Path):
    import json

    import pytest

    from tokendrain.storage.export import HARD_PREVIEW_LIMIT, PREVIEW_LIMIT, workspace_operation

    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# Project")
    (root / "src" / "app.py").write_text("print('hello')\n")
    output = tmp_path / "output"
    workspace_operation(root, output, "tree", "")
    entries = json.loads(output.read_text())["entries"]
    assert [entry["name"] for entry in entries] == ["src", "README.md"]
    workspace_operation(root, output, "tree", "src")
    assert json.loads(output.read_text())["entries"][0]["path"] == "src/app.py"
    info = workspace_operation(root, output, "file", "src/app.py")
    assert output.read_text() == "print('hello')\n" and info["size"] == 15
    workspace_operation(root, output, "download", "src/app.py")
    assert output.read_bytes() == b"print('hello')\n"
    workspace_operation(root, output, "archive", "src")
    with zipfile.ZipFile(output) as archive:
        assert archive.read("src/app.py") == b"print('hello')\n"
        assert "README.md" not in archive.namelist()
    for unsafe in ["../../private", "/etc/passwd", "src/../README.md", "bad\0path"]:
        with pytest.raises(ValueError):
            workspace_operation(root, output, "file", unsafe)
    (root / "linked").symlink_to(root / "src", target_is_directory=True)
    with pytest.raises(OSError):
        workspace_operation(root, output, "file", "linked/app.py")
    with (root / "large.log").open("wb") as file:
        file.truncate(PREVIEW_LIMIT + 1)
    with pytest.raises(ValueError, match="preview limit"):
        workspace_operation(root, output, "file", "large.log")
    workspace_operation(root, output, "file", "large.log", True)
    assert output.stat().st_size == PREVIEW_LIMIT + 1
    with (root / "large.log").open("wb") as file:
        file.truncate(HARD_PREVIEW_LIMIT + 1)
    with pytest.raises(ValueError, match="preview limit"):
        workspace_operation(root, output, "file", "large.log", True)


async def test_workspace_worker_launch_does_not_require_parent_python_package_path(
    tmp_path: Path, monkeypatch
):
    """Run the real isolated worker with stub mount tools, without root or a real VM."""
    import asyncio
    import json
    import os
    import pwd
    import shutil

    from tokendrain.vm.helper import HelperConfig, InfrastructureService

    tools = tmp_path / "tools"
    tools.mkdir()
    mkdir = shutil.which("mkdir")
    assert mkdir
    for command in ("mount", "umount"):
        tool = tools / command
        tool.write_text(
            f'#!/bin/sh\n{mkdir} -p "$6/workspace"\n'
            if command == "mount"
            else "#!/bin/sh\nexit 0\n"
        )
        tool.chmod(0o700)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    disk = tmp_path / "vm.img"
    disk.touch()
    user = pwd.getpwuid(os.getuid()).pw_name
    service = InfrastructureService(
        HelperConfig(state_dir=state, guest_artifacts=tmp_path, daemon_user=user, vm_user=user)
    )
    monkeypatch.setattr(service, "_open_disk", lambda *_: os.open(disk, os.O_RDONLY))
    launch = asyncio.create_subprocess_exec

    async def private_namespace_stub(*args, **kwargs):
        assert args[:4] == ("unshare", "--mount", "--propagation", "private")
        assert args[5] == "-I"
        assert Path(args[6]).name == "export.py"
        # Only the namespace/mount operations are stubbed: Python and the worker are real.
        return await launch(*args[4:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", private_namespace_stub)
    result = await service.export_workspace("test", "tree")
    output = state / "exports" / (result["export_id"] + ".json")
    assert json.loads(output.read_text()) == {"path": "", "entries": []}
    output.unlink()
