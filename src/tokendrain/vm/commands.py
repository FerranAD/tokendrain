# ruff: noqa: ASYNC109
"""The one subprocess boundary for host infrastructure operations."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    async def run(
        self, *argv: str, input: bytes | None = None, timeout: float = 120, check: bool = True
    ) -> CommandResult: ...


class CommandRunner:
    async def run(
        self, *argv: str, input: bytes | None = None, timeout: float = 120, check: bool = True
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        result = CommandResult(process.returncode or 0, stdout, stderr)
        if check and result.returncode:
            # Infrastructure arguments never contain credentials. Bound captured errors.
            raise RuntimeError(
                f"{argv[0]} failed ({result.returncode}): {stderr[-2048:].decode(errors='replace')}"
            )
        return result
