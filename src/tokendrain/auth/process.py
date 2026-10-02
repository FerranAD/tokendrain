"""Isolated host-side Codex processes for authentication and read-only probes.

Never point these processes at project files or the daemon administrator's home.
The caller owns every RPC and must never start a thread/turn in this context.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from tokendrain.codex.rpc import JsonRpcPeer


def clean_process_environment(home: Path) -> dict[str, str]:
    inherited = {"PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "NIX_SSL_CERT_FILE"}
    return {
        **{name: value for name, value in os.environ.items() if name in inherited},
        "HOME": str(home),
        "CODEX_HOME": str(home / "codex"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"),
    }


def auth_runtime_directory() -> Path:
    return Path(os.environ.get("TOKENDRAIN_AUTH_RUNTIME_DIR", "/run/tokendrain-auth"))


@asynccontextmanager
async def isolated_codex(
    runtime_dir: Path,
    *,
    executable: str = "codex",
    imported_auth: bytes | None = None,
) -> AsyncIterator[tuple[JsonRpcPeer, Path]]:
    """Private tmpfs home, no inherited tokens/config, bounded process lifetime."""
    await asyncio.to_thread(runtime_dir.mkdir, parents=True, mode=0o700, exist_ok=True)
    directory = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="codex-", dir=runtime_dir))
    process: asyncio.subprocess.Process | None = None
    peer: JsonRpcPeer | None = None
    try:
        home = directory / "codex"
        await asyncio.to_thread(home.mkdir, mode=0o700)
        if imported_auth is not None:

            def write_auth() -> None:
                fd = os.open(home / "auth.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(imported_auth)

            await asyncio.to_thread(write_auth)
        credential_store = "file" if imported_auth is not None else "ephemeral"
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            f'cli_auth_credentials_store="{credential_store}"',
            env=clean_process_environment(directory),
            cwd=directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8 * 1024 * 1024,
            start_new_session=True,
        )
        assert process.stdout and process.stdin
        peer = JsonRpcPeer(process.stdout, process.stdin, timeout=45)
        await peer.start()
        yield peer, home
    finally:
        if peer:
            await peer.close()
        if process and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        await asyncio.to_thread(shutil.rmtree, directory)
