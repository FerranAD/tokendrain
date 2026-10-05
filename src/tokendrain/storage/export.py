"""Read-only workspace worker, run in the helper's private mount namespace."""

import contextlib
import json
import mimetypes
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

PREVIEW_LIMIT = 2 * 1024**2
HARD_PREVIEW_LIMIT = 32 * 1024**2
TREE_LIMIT = 10000


def workspace_path(value: str) -> str:
    """Validate before normalization so traversal and ambiguous paths cannot disappear."""
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or value.startswith("/")
        or "\0" in value
        or "\\" in value
        or any(part in {"..", ".", ""} for part in value.split("/"))
        and value != ""
    ):
        raise ValueError("Expected a workspace-relative path without traversal")
    # Reject surrogate/unrepresentable names rather than accidentally reading another file.
    value.encode("utf-8", errors="strict")
    return value


@contextlib.contextmanager
def open_workspace(root: Path, path: str, *, directory: bool = False) -> Iterator[int]:
    """Resolve every component through pinned O_NOFOLLOW descriptors, never host paths."""
    workspace_path(path)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/") if path else []
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if directory or index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise ValueError("Only regular files and directories are accessible")
        yield descriptor
    finally:
        os.close(descriptor)


def metadata(path: str, info: os.stat_result) -> dict[str, Any]:
    return {
        "name": path.rsplit("/", 1)[-1],
        "path": path,
        "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
        "size": info.st_size if stat.S_ISREG(info.st_mode) else None,
        "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
        "mime_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
    }


def list_directory(root: Path, path: str) -> dict[str, Any]:
    entries = []
    with open_workspace(root, path, directory=True) as descriptor:
        with os.scandir(descriptor) as children:
            for entry in children:
                info = entry.stat(follow_symlinks=False)
                if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                    continue
                relative = f"{path}/{entry.name}" if path else entry.name
                try:
                    workspace_path(relative)
                    entries.append(metadata(relative, info))
                except (ValueError, UnicodeError, OverflowError):
                    continue
                if len(entries) > TREE_LIMIT:
                    raise ValueError("Directory exceeds the 10,000-entry browsing limit")
    entries.sort(key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))
    return {"path": path, "entries": entries}


def copy_file(root: Path, path: str, output: BinaryIO, limit: int | None) -> dict[str, Any]:
    with open_workspace(root, path) as descriptor:
        info = os.fstat(descriptor)
        if limit is not None and info.st_size > limit:
            raise ValueError(f"File exceeds the {limit // 1024**2} MiB preview limit")
        # Chunked copying bounds memory for downloads of arbitrarily large regular files.
        with os.fdopen(os.dup(descriptor), "rb") as source:
            if limit is None:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            else:
                remaining = limit
                while chunk := source.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError(f"File exceeds the {limit // 1024**2} MiB preview limit")
                    output.write(chunk)
        return metadata(path, info)


def archive_tree(root: Path, destination: Path, path: str = "") -> None:
    """ZIP regular files/directories only, keeping paths relative to /workspace."""
    with open_workspace(root, path, directory=True) as root_fd:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            if path:
                archive.mkdir(path + "/")
            for directory, dirs, files, descriptor in os.fwalk(".", dir_fd=root_fd):
                relative = Path(directory).relative_to(".")
                dirs[:] = [
                    name
                    for name in dirs
                    if stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)
                ]
                for name in dirs + files:
                    member = (Path(path) / relative / name).as_posix()
                    try:
                        workspace_path(member)
                    except (ValueError, UnicodeError):
                        if name in dirs:
                            dirs.remove(name)
                        continue
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        archive.mkdir(member + "/")
                    elif stat.S_ISREG(info.st_mode):
                        fd = os.open(
                            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                        )
                        with os.fdopen(fd, "rb") as source:
                            actual = os.fstat(source.fileno())
                            if not stat.S_ISREG(actual.st_mode):
                                continue
                            stamp = datetime.fromtimestamp(actual.st_mtime, UTC)
                            year = max(1980, min(2107, stamp.year))
                            item = zipfile.ZipInfo(member, (year, *stamp.timetuple()[1:6]))
                            item.compress_type = zipfile.ZIP_DEFLATED
                            item.external_attr = (actual.st_mode & 0o777) << 16
                            with archive.open(item, "w", force_zip64=True) as output:
                                shutil.copyfileobj(source, output, length=1024 * 1024)


def workspace_operation(
    root: Path, destination: Path, operation: str, path: str, allow_large: bool = False
) -> dict[str, Any]:
    """Shared worker primitives also allow cheap tests without mounting an image."""
    workspace_path(path)
    if operation == "archive":
        archive_tree(root, destination, path)
        return {}
    if operation == "tree":
        destination.write_text(json.dumps(list_directory(root, path)), encoding="utf-8")
        return {}
    if operation not in {"file", "download"}:
        raise ValueError("Unknown workspace operation")
    limit = (
        None if operation == "download" else HARD_PREVIEW_LIMIT if allow_large else PREVIEW_LIMIT
    )
    with destination.open("wb") as output:
        return copy_file(root, path, output, limit)


def main() -> None:
    disk_fd, output_fd = map(int, sys.argv[1:3])
    operation, path, allow_large = sys.argv[3:6]
    disk = Path(f"/proc/{os.getpid()}/fd/{disk_fd}")
    destination = Path(f"/proc/self/fd/{output_fd}")
    with tempfile.TemporaryDirectory(prefix="tokendrain-export-") as temporary:
        mount = Path(temporary)
        subprocess.run(
            [
                "mount",
                "-t",
                "ext4",
                "-o",
                "loop,ro,noload,nodev,nosuid,noexec",
                str(disk),
                str(mount),
            ],
            check=True,
            capture_output=True,
        )
        try:
            try:
                result = workspace_operation(
                    mount / "workspace", destination, operation, path, allow_large == "1"
                )
            except FileNotFoundError:
                result = {"error": "Workspace entry not found", "status": 404}
            except (ValueError, OSError, UnicodeError, OverflowError) as error:
                # Never expose host or mount paths from OS exceptions.
                message = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Workspace entry is inaccessible"
                )
                result = {"error": message, "status": 413 if "preview limit" in message else 400}
            print(json.dumps(result))
        finally:
            subprocess.run(["umount", str(mount)], check=True, capture_output=True)


if __name__ == "__main__":
    main()
