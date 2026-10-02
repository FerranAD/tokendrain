from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,150}$")
_HEADER = b"TDCR\x01"


class CredentialStore(Protocol):
    async def put(self, name: str, value: bytes) -> None: ...
    async def get(self, name: str) -> bytes | None: ...
    async def delete(self, name: str) -> None: ...
    async def names(self) -> list[str]: ...


def load_master_key(path: Path) -> bytes:
    """Accept a raw 32-byte key or its URL-safe base64 representation."""
    if path.stat().st_mode & 0o077:
        raise ValueError("master-key file must not be group/world accessible")
    raw = path.read_bytes()
    if len(raw) == 32:
        return raw
    try:
        decoded = base64.urlsafe_b64decode(raw.strip())
    except ValueError as exc:
        raise ValueError("master key must be 32 bytes or base64 encoding thereof") from exc
    if len(decoded) != 32:
        raise ValueError("master key must contain exactly 32 bytes")
    return decoded


class EncryptedFileCredentialStore:
    """Versioned AES-256-GCM blobs; names are authenticated associated data."""

    def __init__(self, directory: Path, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("expected a 32-byte master key")
        self.directory = directory
        self._cipher = AESGCM(key)
        self._lock = asyncio.Lock()

    def _path(self, name: str) -> Path:
        if not _NAME.fullmatch(name) or name in {".", ".."}:
            raise ValueError("invalid credential name")
        return self.directory / name

    def _write(self, name: str, value: bytes) -> None:
        path = self._path(name)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        nonce = secrets.token_bytes(12)
        encrypted = _HEADER + nonce + self._cipher.encrypt(nonce, value, name.encode())
        temporary = self.directory / f".pending-{secrets.token_hex(12)}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)

    async def put(self, name: str, value: bytes) -> None:
        async with self._lock:
            await asyncio.to_thread(self._write, name, value)

    def _read(self, name: str) -> bytes | None:
        path = self._path(name)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        if not blob.startswith(_HEADER) or len(blob) < 33:
            raise ValueError("unsupported or truncated credential blob")
        return self._cipher.decrypt(blob[5:17], blob[17:], name.encode())

    async def get(self, name: str) -> bytes | None:
        return await asyncio.to_thread(self._read, name)

    async def delete(self, name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._path(name).unlink, missing_ok=True)

    async def names(self) -> list[str]:
        def scan() -> list[str]:
            if not self.directory.exists():
                return []
            return sorted(p.name for p in self.directory.iterdir() if not p.name.startswith("."))

        return await asyncio.to_thread(scan)


class SecretRedactor:
    def __init__(self, values: list[str] | None = None) -> None:
        self._values: tuple[str, ...] = ()
        self.replace_values(values or [])

    def replace_values(self, values: list[str]) -> None:
        self._values = tuple(sorted({value for value in values if value}, key=len, reverse=True))

    def redact(self, message: str) -> str:
        for value in self._values:
            message = message.replace(value, "[REDACTED]")
        return message
