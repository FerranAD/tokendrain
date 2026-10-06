"""Claude work sessions over the existing host/guest transport."""

import asyncio
import json
from typing import Any

import httpx

from tokendrain.auth.claude import ClaudeAuthManager
from tokendrain.credentials import SecretRedactor
from tokendrain.domain import RunReport, UsageWindow
from tokendrain.orchestration.driver import (
    GuestConnector,
    LogCallback,
    ProviderLimited,
    TokenCallback,
    UsageCallback,
    guest_github_secret,
    report_output_schema,
)
from tokendrain.protocol import GuestClient
from tokendrain.vm.models import VmHandle


class ClaudeSessionFactory:
    def __init__(
        self,
        auth: ClaudeAuthManager,
        timeout: int = 7200,
        connector: GuestConnector = GuestClient.connect,
    ) -> None:
        self.auth, self.timeout, self.connector = auth, timeout, connector

    async def connect(
        self,
        handle: VmHandle,
        secrets: dict[str, str],
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
    ) -> "ClaudeSession":
        async def unsupported(method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise ValueError("Interactive actions are unavailable during autonomous runs.")

        guest = await self.connector(
            handle.vsock_path, handle.guest_port, request_handler=unsupported, timeout=120
        )
        return ClaudeSession(self.auth, guest, secrets, github_token, log, observe, self.timeout)


class ClaudeSession:
    def __init__(
        self,
        auth: ClaudeAuthManager,
        guest: Any,
        secrets: dict[str, str],
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
        timeout: int,
    ) -> None:
        self.auth, self.guest, self.secrets = auth, guest, secrets
        self.github_token, self.log, self.observe, self.timeout = (
            github_token,
            log,
            observe,
            timeout,
        )
        self.thread_id = ""
        self.redactor = SecretRedactor()
        self.windows: dict[str, UsageWindow] = {}

    async def initialize(self, thread_id: str | None, model: str) -> str:
        result = await self.guest.request("claude_initialize", {"session_id": thread_id})
        self.thread_id = result["session_id"]
        return self.thread_id

    async def usage(self) -> list[UsageWindow]:
        try:
            self.remember(await self.auth.windows(interval=60))
            return list(self.windows.values())
        except (ValueError, OSError, TimeoutError, httpx.HTTPError):
            return []

    def remember(self, windows: list[UsageWindow]) -> None:
        for window in windows:
            previous = self.windows.get(window.limit_id)
            if previous is None or window.observed_at >= previous.observed_at:
                self.windows[window.limit_id] = window

    async def turn(self, prompt: str, model: str, effort: str, cancel: asyncio.Event) -> RunReport:
        access, _ = await self.auth.access()
        github = await self.github_token()
        github_value = guest_github_secret(github) if github else None
        self.redactor.replace_values(
            [access, *self.secrets.values(), *([github_value] if github_value else [])]
        )
        await self.guest.credentials_set(
            {
                "claude": {"access_token": access},
                "secrets": self.secrets,
                "github_token": github_value,
            }
        )
        completed: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async def start() -> dict[str, Any]:
            await self.guest.request(
                "claude_turn",
                {
                    "prompt": prompt,
                    "model": model,
                    "effort": effort,
                    "schema": report_output_schema(),
                },
            )
            return await completed

        request = asyncio.create_task(start())
        cancelled = asyncio.create_task(cancel.wait())

        async def notifications() -> None:
            tools: dict[str, dict[str, Any]] = {}
            while True:
                event = await self.guest.notifications.get()
                if event.method == "claude/result":
                    if not completed.done():
                        completed.set_result(event.params)
                    continue
                if event.method == "guest/log":
                    await self.log(self.redactor.redact(event.params.get("text", "")))
                if event.method == "claude/event":
                    raw = event.params
                    content = raw.get("message", {}).get("content", [])
                    if raw.get("type") in {"assistant", "user"} and isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") == "text":
                                await self.log(
                                    self.redactor.redact(
                                        json.dumps(
                                            {
                                                "type": "agentMessage",
                                                "phase": "commentary",
                                                "text": item.get("text", ""),
                                            }
                                        )
                                    )
                                )
                            elif item.get("type") == "tool_use":
                                tools[item["id"]] = item
                                if item.get("name") == "Bash":
                                    await self.log(
                                        self.redactor.redact(
                                            json.dumps(
                                                {
                                                    "type": "commandExecution",
                                                    "command": item.get("input", {}).get(
                                                        "command", ""
                                                    ),
                                                    "status": "in_progress",
                                                }
                                            )
                                        )
                                    )
                                else:
                                    await self.log(
                                        self.redactor.redact(
                                            f"{item.get('name', 'Tool')}: "
                                            f"{json.dumps(item.get('input', {}))}"
                                        )
                                    )
                            elif item.get("type") == "tool_result":
                                tool = tools.get(item.get("tool_use_id", ""), {})
                                if tool.get("name") == "Bash":
                                    output = item.get("content", "")
                                    await self.log(
                                        self.redactor.redact(
                                            json.dumps(
                                                {
                                                    "type": "commandExecution",
                                                    "command": tool.get("input", {}).get(
                                                        "command", ""
                                                    ),
                                                    "aggregatedOutput": output
                                                    if isinstance(output, str)
                                                    else json.dumps(output),
                                                    "status": "completed",
                                                    "exitCode": 1 if item.get("is_error") else 0,
                                                }
                                            )
                                        )
                                    )
                    rate = raw.get("rate_limit_info")
                    if isinstance(rate, dict) and rate.get("status") == "rejected":
                        raise ProviderLimited("Claude subscription usage limit reached")
                    if isinstance(rate, dict) and rate.get("utilization") is not None:
                        kind = rate.get("rate_limit_type")
                        minutes = (
                            300
                            if kind == "five_hour"
                            else 10080
                            if kind in {"seven_day", "seven_day_opus", "seven_day_sonnet"}
                            else None
                        )
                        if minutes:
                            from datetime import UTC, datetime

                            windows = [
                                UsageWindow(
                                    limit_id=f"claude:{kind}",
                                    window_minutes=minutes,
                                    used_percent=float(rate["utilization"]) * 100,
                                    resets_at=datetime.fromtimestamp(rate["resets_at"], UTC)
                                    if rate.get("resets_at")
                                    else None,
                                    metadata={
                                        "agent": "claude_code",
                                        "account_id": (await self.auth.record() or {}).get("id"),
                                    },
                                )
                            ]
                            self.remember(windows)
                            await self.observe(list(self.windows.values()))

        events = asyncio.create_task(notifications())
        disconnected = asyncio.create_task(self.guest.closed.wait())
        try:
            async with asyncio.timeout(self.timeout):
                done, _ = await asyncio.wait(
                    [request, cancelled, events, disconnected], return_when=asyncio.FIRST_COMPLETED
                )
                if cancelled in done:
                    await self.guest.request("claude_interrupt")
                    raise asyncio.CancelledError
                if events in done:
                    await events
                if disconnected in done:
                    raise ConnectionError("Claude guest disconnected; workspace preserved.")
                result = await request
                if result.get("is_error"):
                    if result.get("provider_limited"):
                        raise ProviderLimited("Claude subscription usage limit reached")
                    raise ValueError("Claude could not finish this turn. Check live activity.")
                structured = result.get("structured_output")
                if not isinstance(structured, dict):
                    raise ValueError("Claude final answer was not a valid structured checkpoint")
                return RunReport.model_validate_json(self.redactor.redact(json.dumps(structured)))
        finally:
            if not request.done():
                try:
                    await asyncio.wait_for(self.guest.request("claude_interrupt"), 5)
                except Exception:
                    pass
            for task in (request, cancelled, events, disconnected):
                task.cancel()
            await asyncio.gather(request, cancelled, events, disconnected, return_exceptions=True)

    async def close(self) -> None:
        try:
            await self.guest.request("credentials_clear")
        finally:
            await self.guest.close()


class AgentSessionFactory:
    def __init__(self, settings: Any, codex: Any, claude: Any) -> None:
        self.settings, self.codex, self.claude = settings, codex, claude

    async def connect(self, *args: Any, **kwargs: Any) -> Any:
        factory = self.claude if self.settings.active_agent == "claude_code" else self.codex
        return await factory.connect(*args, **kwargs)
