"""Single-owner administrative sessions and runtime-file provisioning."""

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class SessionTokens:
    def __init__(self, admin_token: str | None) -> None:
        if admin_token is not None and len(admin_token) < 32:
            raise ValueError("Administrative token must contain at least 32 characters")
        self._token = admin_token or ""
        self._key = hashlib.sha256((admin_token or secrets.token_urlsafe(32)).encode()).digest()

    def authenticate(self, token: str) -> bool:
        return bool(self._token) and hmac.compare_digest(token.encode(), self._token.encode())

    def issue(self) -> str:
        value = f"{int(time.time()) + 43200}.{secrets.token_urlsafe(24)}"
        signature = hmac.new(self._key, value.encode(), hashlib.sha256).digest()
        return value + "." + base64.urlsafe_b64encode(signature).decode()

    def valid(self, cookie: str) -> bool:
        try:
            value, signature = cookie.rsplit(".", 1)
            expiry = int(value.split(".", 1)[0])
            expected = hmac.new(self._key, value.encode(), hashlib.sha256).digest()
            return expiry > time.time() and hmac.compare_digest(
                base64.b64decode(signature, altchars=b"-_", validate=True), expected
            )
        except (ValueError, TypeError):
            return False


def protected_file(path: Path, size: int = 32, *, raw: bool = False) -> bytes:
    """Create once with O_EXCL; never silently replace a key during startup."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        value = secrets.token_bytes(size) if raw else secrets.token_urlsafe(size).encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(value)
                file.flush()
                os.fsync(file.fileno())
            _sync_directory(path.parent)
        except FileExistsError:
            pass
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Credential file must have mode 0600 or 0400: {path}")
    return path.read_bytes().strip() if not raw else path.read_bytes()
