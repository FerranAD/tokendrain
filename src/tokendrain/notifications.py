"""Host-owned ntfy usage reminders; delivery never creates or authorizes a run."""

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.credentials.store import CredentialStore
from tokendrain.db.models import Setting
from tokendrain.domain import Boundary, UsageWindow, utcnow

log = logging.getLogger(__name__)
TOKEN = "ntfy-access-token"


class UsageTrigger(Boundary):
    window_minutes: int = Field(default=10080, ge=1, le=525600)
    limit_id: str | None = Field(default=None, max_length=200)
    hours_before_reset: float = Field(default=12, gt=0, le=168, allow_inf_nan=False)
    min_remaining_percent: float = Field(default=80, ge=0, le=100, allow_inf_nan=False)

    def matches(self, window: UsageWindow, now: datetime) -> bool:
        if window.window_minutes != self.window_minutes:
            return False
        if self.limit_id and window.limit_id != self.limit_id:
            return False
        if window.resets_at is None or window.resets_at.tzinfo is None:
            return False
        if window.observed_at.tzinfo is None:
            return False
        age = (now - window.observed_at).total_seconds()
        remaining = (window.resets_at - now).total_seconds()
        return (
            -30 <= age <= 300
            and 0 < remaining <= self.hours_before_reset * 3600
            and max(0, 100 - window.used_percent) >= self.min_remaining_percent
        )


class UsageAlert(UsageTrigger):
    id: str = Field(default_factory=lambda: str(uuid4()), pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    enabled: bool = True

    def matches(self, window: UsageWindow, now: datetime) -> bool:
        return self.enabled and super().matches(window, now)


class NtfyConfig(Boundary):
    enabled: bool = False
    server_url: str = Field(default="https://ntfy.sh", max_length=2000)
    topic: str = Field(default="", max_length=200, pattern=r"^[a-zA-Z0-9_-]*$")
    rules: list[UsageAlert] = Field(default_factory=list, max_length=20)

    @field_validator("server_url")
    @classmethod
    def server(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or any(char.isspace() or ord(char) < 32 for char in value)
            or "\\" in value
        ):
            raise ValueError("Use an HTTP(S) server URL without credentials, query or fragment")
        # Validate the port too; prefix paths support reverse-proxy installations.
        _ = url.port
        return value.rstrip("/")

    @model_validator(mode="after")
    def valid_rules(self) -> "NtfyConfig":
        if self.enabled and not self.topic:
            raise ValueError("A topic is required when notifications are enabled")
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Alert rule IDs must be unique")
        return self


class NtfyInput(NtfyConfig):
    access_token: SecretStr | None = None
    clear_token: bool = False

    @field_validator("access_token")
    @classmethod
    def valid_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(r"[\x21-\x7e]{1,4096}", value.get_secret_value()):
            raise ValueError("Use a nonempty ASCII access token without whitespace")
        return value

    @model_validator(mode="after")
    def token_operation(self) -> "NtfyInput":
        if self.clear_token and self.access_token is not None:
            raise ValueError("Replace or clear the access token, not both")
        return self


class NtfyService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        credentials: CredentialStore,
        http: httpx.AsyncClient,
        public_url: str,
        read_usage: Callable[[], Awaitable[list[UsageWindow]]],
    ) -> None:
        self.sessions, self.credentials, self.http = sessions, credentials, http
        self.public_url, self.read_usage = public_url, read_usage
        self.lock = asyncio.Lock()

    async def _load(self, key: str) -> dict[str, Any]:
        async with self.sessions() as db:
            row = await db.get(Setting, key)
            return dict(row.value) if row else {}

    async def _save(self, key: str, value: dict[str, object]) -> None:
        async with self.sessions.begin() as db:
            await db.merge(Setting(key=key, value=value))

    async def config(self) -> NtfyConfig:
        return NtfyConfig.model_validate(await self._load("ntfy"))

    async def status(self) -> dict[str, object]:
        async with self.lock:
            return {
                **(await self.config()).model_dump(mode="json"),
                "has_token": await self.credentials.get(TOKEN) is not None,
                "delivery": {
                    key: value
                    for key, value in (await self._load("ntfy_delivery")).items()
                    if key in {"last_sent_at", "last_error", "last_checked_at"}
                },
            }

    async def configure(self, value: NtfyInput) -> None:
        async with self.lock:
            if value.access_token is not None:
                await self.credentials.put(TOKEN, value.access_token.get_secret_value().encode())
            elif value.clear_token:
                await self.credentials.delete(TOKEN)
            await self._save(
                "ntfy", value.model_dump(mode="json", exclude={"access_token", "clear_token"})
            )

    async def _publish(
        self,
        config: NtfyConfig,
        message: str,
        *,
        test: bool = False,
        title: str | None = None,
        click: str | None = None,
    ) -> None:
        if not config.topic:
            raise ValueError("Save a notification topic before sending a test")
        token = await self.credentials.get(TOKEN)
        headers = {"Authorization": "Bearer " + token.decode()} if token else {}
        try:
            response = await self.http.post(
                config.server_url + "/",
                json={
                    "topic": config.topic,
                    "title": title
                    or ("Tokendrain test" if test else "Codex usage limit resets soon"),
                    "message": message,
                    "click": click or self.public_url,
                    "tags": ["test_tube" if test else "hourglass_flowing_sand"],
                },
                headers=headers,
                timeout=10,
                follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # Never expose response bodies, private topic paths, or auth headers.
            raise ValueError(
                "ntfy delivery failed. Check server, topic and access token."
            ) from None

    async def approval(self, occurrence_id: str, name: str, message: str) -> None:
        async with self.lock:
            await self._publish(
                await self.config(),
                message,
                title=f"Authorize automation: {name}",
                click=self.public_url.rstrip("/") + "/automation-occurrences/" + occurrence_id,
            )

    async def test(self) -> None:
        async with self.lock:
            await self._publish(
                await self.config(), "Your tokendrain notifications are connected.", test=True
            )

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        async with self.lock:
            config = await self.config()
            if not config.enabled or not any(rule.enabled for rule in config.rules):
                return
            state = await self._load("ntfy_delivery")
            state["last_checked_at"] = now.isoformat()
            delivered = {
                str(key): float(expiry)
                for key, expiry in state.get("delivered", {}).items()
                if float(expiry) > now.timestamp()
            }
            try:
                windows = await self.read_usage()
                if not any(
                    window.observed_at.tzinfo is not None
                    and -30 <= (now - window.observed_at).total_seconds() <= 300
                    for window in windows
                ):
                    raise ValueError("No fresh usage observation")
                for rule in config.rules:
                    for window in windows:
                        if not rule.matches(window, now):
                            continue
                        assert window.resets_at is not None
                        identity = json.dumps(
                            [
                                config.server_url,
                                config.topic,
                                rule.model_dump(),
                                window.limit_id,
                                window.window_minutes,
                                window.resets_at.timestamp(),
                            ],
                            sort_keys=True,
                        )
                        key = hashlib.sha256(identity.encode()).hexdigest()
                        if key in delivered:
                            continue
                        hours = (window.resets_at - now).total_seconds() / 3600
                        label = (
                            "weekly"
                            if window.window_minutes == 10080
                            else (f"{window.window_minutes}-minute")
                        )
                        await self._publish(
                            config,
                            f"Your {label} limit ({window.limit_id}) resets in {hours:.1f}h "
                            f"and you still have {max(0, 100 - window.used_percent):.0f}% "
                            "of your usage limit remaining.",
                        )
                        delivered[key] = window.resets_at.timestamp()
                        state["last_sent_at"] = now.isoformat()
                        state["delivered"] = delivered
                        # Persist each successful delivery before considering another rule.
                        await self._save("ntfy_delivery", state)
                state["last_error"] = None
            except (ValueError, OSError, TimeoutError, httpx.HTTPError) as exc:
                state["last_error"] = (
                    "Usage or ntfy unavailable. Check the Codex connection and notification "
                    "settings; delivery will retry on the next check."
                )
                log.warning("notifications.check_failed", extra={"error_type": type(exc).__name__})
            state["delivered"] = delivered
            await self._save("ntfy_delivery", state)

    async def serve(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("notifications.worker_failed")
            await asyncio.sleep(60)
