"""Host-owned Claude subscription login, refresh and usage metadata."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import pty
import re
import select
import shutil
import signal
import tempfile
import termios
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from tokendrain.auth.process import clean_process_environment
from tokendrain.credentials import CredentialStore
from tokendrain.domain import UsageWindow

OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
RECORD = "claude-account"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def decode_credentials(value: Any) -> dict[str, Any]:
    oauth = value.get("claudeAiOauth") if isinstance(value, dict) else None
    if (
        not isinstance(oauth, dict)
        or not isinstance(oauth.get("accessToken"), str)
        or not oauth["accessToken"]
    ):
        raise ValueError(
            "Claude subscription login did not produce credentials. Try connecting again."
        )
    if (
        not isinstance(oauth.get("scopes"), list)
        or "user:inference" not in oauth["scopes"]
        or "user:profile" not in oauth["scopes"]
    ):
        raise ValueError(
            "Connect a Claude subscription account, rather than a Console API account."
        )
    if (
        not isinstance(oauth.get("expiresAt"), (int, float))
        or not math.isfinite(oauth["expiresAt"])
        or oauth["expiresAt"] <= 0
    ):
        raise ValueError("Claude login returned an invalid expiry. Try connecting again.")
    return dict(oauth)


def normalize_usage(raw: dict[str, Any], account: str) -> list[UsageWindow]:
    windows = []
    for key, minutes, name in (
        ("five_hour", 300, "5-hour usage"),
        ("seven_day", 10080, "Weekly usage"),
        ("seven_day_opus", 10080, "Weekly Opus usage"),
        ("seven_day_sonnet", 10080, "Weekly Sonnet usage"),
    ):
        value = raw.get(key)
        if not isinstance(value, dict) or value.get("utilization") is None:
            continue
        reset = value.get("resets_at")
        resets_at = (
            datetime.fromisoformat(reset.replace("Z", "+00:00")) if isinstance(reset, str) else None
        )
        if resets_at is not None and resets_at.tzinfo is None:
            raise ValueError("Claude usage reset has no timezone")
        windows.append(
            UsageWindow(
                limit_id=f"claude:{key}",
                name=name,
                window_minutes=minutes,
                used_percent=value["utilization"],
                resets_at=resets_at,
                metadata={"agent": "claude_code", "account_id": account},
            )
        )
    return windows


class ClaudeAuthManager:
    def __init__(
        self,
        store: CredentialStore,
        http: httpx.AsyncClient,
        runtime_dir: Path,
        save_login: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        executable: str = "claude",
        login_timeout: float = 600,
    ) -> None:
        self.store, self.http, self.runtime_dir = store, http, runtime_dir
        self.save_login, self.executable = save_login, executable
        self.login_timeout = login_timeout
        self.login_lock = asyncio.Lock()
        self.lock = asyncio.Lock()
        self.usage_lock = asyncio.Lock()
        self.next_refresh_attempt = 0.0
        self.login: dict[str, Any] | None = None
        self.login_task: asyncio.Task[None] | None = None
        self.master: int | None = None
        self.next_usage_attempt = 0.0
        self.usage_cache: tuple[float, dict[str, Any]] | None = None
        self.last_error: str | None = None

    async def record(self) -> dict[str, Any] | None:
        raw = await self.store.get(RECORD)
        return json.loads(raw) if raw else None

    async def status(self) -> dict[str, Any]:
        record = await self.record()
        return {
            "connected": record is not None,
            "account_label": (record or {}).get("label"),
            "expires_at": (record or {}).get("oauth", {}).get("expiresAt"),
            "credential_error": self.last_error,
        }

    async def save(self, record: dict[str, Any]) -> None:
        await self.store.put(RECORD, json.dumps(record).encode())
        self.usage_cache = None
        self.next_usage_attempt = 0
        self.last_error = None
        self.next_refresh_attempt = 0

    async def disconnect(self) -> None:
        await self.cancel_login()
        async with self.lock:
            await self.store.delete(RECORD)
            self.usage_cache = None
            self.next_usage_attempt = 0
            self.last_error = None

    async def access(self, force: bool = False) -> tuple[str, str]:
        async with self.lock:
            record = await self.record()
            if not record:
                raise ValueError("Connect Claude Code in Settings before starting a run.")
            oauth = record["oauth"]
            if force or oauth["expiresAt"] / 1000 <= time.time() + 180:
                if time.time() < self.next_refresh_attempt:
                    raise ValueError(self.last_error or "Claude credential refresh is backing off.")
                self.next_refresh_attempt = time.time() + 60
                if not oauth.get("refreshToken"):
                    self.last_error = "Claude login expired. Connect Claude Code again in Settings."
                    raise ValueError(self.last_error)
                response = await self.http.post(
                    "https://platform.claude.com/v1/oauth/token",
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": oauth["refreshToken"],
                        "client_id": OAUTH_CLIENT_ID,
                    },
                )
                if response.status_code != 200:
                    self.last_error = (
                        "Claude credentials could not be renewed. Reconnect or retry later."
                    )
                    raise ValueError(self.last_error)
                result = response.json()
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("access_token"), str)
                    or not result["access_token"]
                    or not isinstance(result.get("expires_in"), (int, float))
                    or not math.isfinite(result["expires_in"])
                    or result["expires_in"] <= 0
                ):
                    raise ValueError("Claude returned an invalid credential refresh response.")
                oauth = {
                    **oauth,
                    "accessToken": result["access_token"],
                    "refreshToken": result.get("refresh_token", oauth["refreshToken"]),
                    "expiresAt": (time.time() + result["expires_in"]) * 1000,
                }
                record = {**record, "oauth": oauth}
                await self.save(record)
            self.last_error = None
            return str(oauth["accessToken"]), str(record["id"])

    async def usage(self, *, force: bool = False, interval: int = 900) -> dict[str, Any]:
        async with self.usage_lock:
            return await self._usage(force=force, interval=interval)

    async def _usage(self, *, force: bool, interval: int) -> dict[str, Any]:
        now = time.time()
        if now < self.next_usage_attempt:
            raise ValueError("Claude usage is temporarily unavailable; retrying after backoff.")
        if not force and self.usage_cache and now - self.usage_cache[0] < interval:
            return self.usage_cache[1]
        token, _ = await self.access()
        for attempt in range(2):
            response = await self.http.get(
                "https://api.anthropic.com/api/oauth/usage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            )
            if response.status_code == 401 and attempt == 0:
                token, _ = await self.access(force=True)
                continue
            if response.status_code == 429:
                try:
                    delay = float(response.headers.get("retry-after", "900"))
                    delay = max(900, delay) if math.isfinite(delay) else 900
                except ValueError:
                    delay = 900
                self.next_usage_attempt = now + delay
            if response.status_code != 200:
                raise ValueError("Claude usage unavailable. Check Settings; it will be retried.")
            raw = response.json()
            if not isinstance(raw, dict):
                raise ValueError("Claude usage response is invalid.")
            self.usage_cache = (now, raw)
            return raw
        raise ValueError("Claude login expired. Connect Claude Code again.")

    async def windows(self, *, force: bool = False, interval: int = 900) -> list[UsageWindow]:
        raw = await self.usage(force=force, interval=interval)
        record = await self.record()
        windows = normalize_usage(raw, str((record or {}).get("id", "")))
        observed = (
            datetime.fromtimestamp(self.usage_cache[0], UTC)
            if self.usage_cache
            else datetime.now(UTC)
        )
        return [window.model_copy(update={"observed_at": observed}) for window in windows]

    def login_status(self, login_id: str) -> dict[str, Any]:
        if not self.login or self.login["id"] != login_id:
            raise LookupError("Claude login attempt not found")
        return dict(self.login)

    async def start_login(self) -> dict[str, Any]:
        async with self.login_lock:
            return await self._start_login()

    async def _start_login(self) -> dict[str, Any]:
        await self._cancel_login()
        self.login = {
            "id": uuid4().hex,
            "status": "starting",
            "authorization_url": None,
            "expires_at": time.time() + self.login_timeout,
            "error": None,
        }
        self.login_task = asyncio.create_task(self._login(), name="claude-login")
        return dict(self.login)

    async def code(self, login_id: str, value: str) -> None:
        status = self.login_status(login_id)
        if status["status"] != "waiting" or self.master is None:
            raise ValueError("Claude login is not waiting for a code.")
        if not value or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
            raise ValueError("Enter the login code shown by Claude.")
        os.write(self.master, (value + "\n").encode())

    async def cancel_login(self) -> None:
        async with self.login_lock:
            await self._cancel_login()

    async def _cancel_login(self) -> None:
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.login_task
            if self.login:
                self.login["status"] = "cancelled"
        self.login_task = None

    async def _login(self) -> None:
        assert self.login
        process = None
        directory = None
        slave = None
        try:
            self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory = Path(tempfile.mkdtemp(prefix="claude-login-", dir=self.runtime_dir))
            config = directory / "claude"
            config.mkdir(mode=0o700)
            master, slave = pty.openpty()
            self.master = master
            termios.tcsetwinsize(slave, (24, 4096))
            os.set_blocking(master, False)
            env = {
                **clean_process_environment(directory),
                "CLAUDE_CONFIG_DIR": str(config),
                "BROWSER": "true",
                "TERM": "dumb",
                "NO_COLOR": "1",
            }
            process = await asyncio.create_subprocess_exec(
                self.executable,
                "auth",
                "login",
                "--claudeai",
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=directory,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            output = ""
            async with asyncio.timeout(self.login_timeout):
                while process.returncode is None:

                    def read_output() -> bytes:
                        if select.select([master], [], [], 0.2)[0]:
                            try:
                                return os.read(master, 8192)
                            except (OSError, BlockingIOError):
                                pass
                        return b""

                    output = (
                        output + (await asyncio.to_thread(read_output)).decode(errors="replace")
                    )[-131072:]
                    text = ANSI.sub("", output)
                    match = re.search(
                        r"https://(?:claude\.com|claude\.ai|platform\.claude\.com)/(?:cai/)?oauth/authorize[^\s\x1b]*",
                        text,
                    )
                    if match:
                        self.login.update(status="waiting", authorization_url=match.group(0))
                    await asyncio.sleep(0.1)
                await process.wait()
                path = config / ".credentials.json"
                if process.returncode != 0 or not path.exists():
                    raise ValueError("Claude login did not complete. Try connecting again.")
                oauth = decode_credentials(json.loads(path.read_text()))
                profile_path = config / ".claude.json"
                profile = json.loads(profile_path.read_text()) if profile_path.exists() else {}
                account = profile.get("oauthAccount", {})
                record = {
                    "id": account.get("accountUuid") or uuid4().hex,
                    "label": account.get("emailAddress") or "Claude subscription",
                    "oauth": oauth,
                }
                if self.save_login:
                    await self.save_login(record)
                else:
                    await self.save(record)
                self.login.update(status="connected", authorization_url=None)
        except TimeoutError:
            self.login.update(
                status="expired", authorization_url=None, error="Login expired. Connect again."
            )
        except (ValueError, OSError, httpx.HTTPError):
            self.login.update(
                status="failed",
                authorization_url=None,
                error="Claude login failed. Finish active runs and try connecting again.",
            )
        finally:
            if process and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            if self.master is not None:
                os.close(self.master)
                self.master = None
            if slave is not None:
                os.close(slave)
            if directory:
                await asyncio.to_thread(shutil.rmtree, directory)
