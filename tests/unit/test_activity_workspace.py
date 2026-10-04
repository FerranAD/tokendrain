import tarfile
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
    destination = tmp_path / "work.tar.gz"
    archive_tree(root, destination)
    with tarfile.open(destination) as archive:
        assert set(archive.getnames()) == {"sub", "sub/a file.txt"}
        stream = archive.extractfile("sub/a file.txt")
        assert stream and stream.read() == b"Project work"
