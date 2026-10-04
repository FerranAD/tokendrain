"""Read-only workspace archive worker, run only in the helper's private mount namespace."""

import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def archive_tree(root: Path, destination: Path) -> None:
    """Archive regular files/directories only; never dereference guest-controlled links."""
    with tarfile.open(destination, "w:gz", dereference=False) as archive:
        for directory, dirs, files, descriptor in os.fwalk(root, follow_symlinks=False):
            relative = Path(directory).relative_to(root)
            dirs[:] = [
                name
                for name in dirs
                if stat.S_ISDIR(os.stat(name, dir_fd=descriptor, follow_symlinks=False).st_mode)
            ]
            for name in dirs + files:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                path = relative / name
                if stat.S_ISDIR(info.st_mode):
                    member = tarfile.TarInfo(str(path))
                    member.type = tarfile.DIRTYPE
                elif stat.S_ISREG(info.st_mode):
                    # O_NOFOLLOW prevents symlink substitution at open time.
                    fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor
                    )
                    with os.fdopen(fd, "rb") as stream:
                        actual = os.fstat(stream.fileno())
                        if not stat.S_ISREG(actual.st_mode):
                            continue
                        member = tarfile.TarInfo(str(path))
                        member.size, member.mode = actual.st_size, actual.st_mode & 0o777
                        member.mtime = actual.st_mtime
                        archive.addfile(member, stream)
                    continue
                else:
                    # Symlinks, sockets, devices and FIFOs expose no host paths or special files.
                    continue
                member.mode, member.mtime = info.st_mode & 0o777, info.st_mtime
                archive.addfile(member)


def main() -> None:
    disk_fd, output_fd = map(int, sys.argv[1:])
    # The mount subprocess opens our pinned image, rather than a mutable pathname.
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
            archive_tree(mount, destination)
        finally:
            subprocess.run(["umount", str(mount)], check=True, capture_output=True)


if __name__ == "__main__":
    main()
