"""A work session owns one guest connection and never blindly retries a turn."""

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import ValidationError

from tokendrain.auth.openai import OpenAIAuthManager, RuntimeCredentials
from tokendrain.codex.client import CodexClient
from tokendrain.codex.rpc import RpcError
from tokendrain.credentials.store import SecretRedactor
from tokendrain.domain import RunReport, UsageWindow
from tokendrain.github.provider import InstallationToken
from tokendrain.protocol import GuestClient
from tokendrain.usage import normalize_rate_limits
from tokendrain.vm.models import VmHandle

LogCallback = Callable[[str], Awaitable[None]]
UsageCallback = Callable[[list[UsageWindow]], Awaitable[None]]
TokenCallback = Callable[[], Awaitable[InstallationToken | None]]


class ProviderLimited(Exception):
    pass


class WorkSession(Protocol):
    thread_id: str
    redactor: SecretRedactor

    async def initialize(self, thread_id: str | None, model: str) -> str: ...
    async def usage(self) -> list[UsageWindow]: ...
    async def turn(
        self, prompt: str, model: str, effort: str, cancel: asyncio.Event
    ) -> RunReport: ...
    async def close(self) -> None: ...


class SessionFactory(Protocol):
    async def connect(
        self,
        handle: VmHandle,
        secrets: dict[str, str],
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
    ) -> WorkSession: ...


def is_provider_limit(value: object) -> bool:
    raw = json.dumps(value, default=str).lower()
    return any(
        token in raw
        for token in (
            "usagelimitexceeded",
            "ratelimitexceeded",
            "usage_limit",
            "usage limit reached",
        )
    )


def parse_report(text: str) -> RunReport:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.partition("\n")[2].rsplit("```", 1)[0]
    try:
        return RunReport.model_validate_json(cleaned)
    except (ValueError, ValidationError):
        # Preserve work and report uncertainty; malformed output is never completion.
        return RunReport(
            summary=cleaned[:100_000] or "Turn ended without a structured report.",
            remaining=["Inspect the workspace and update the structured report."],
            suggested_next_action="Inspect current state before continuing.",
        )


class RealSessionFactory:
    def __init__(self, auth: OpenAIAuthManager, timeout: int = 7200) -> None:
        self.auth, self.timeout = auth, timeout

    async def connect(
        self,
        handle: VmHandle,
        secrets: dict[str, str],
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
    ) -> WorkSession:
        runtime = await self.auth.runtime_credentials()
        session = RealSession(
            self.auth, runtime, github_token, log, observe, self.timeout, list(secrets.values())
        )
        last_error: Exception | None = None
        # Boot readiness is read-only and safely retryable, bounded to two minutes.
        for attempt in range(30):
            try:
                session.guest = await GuestClient.connect(
                    handle.vsock_path,
                    handle.guest_port,
                    request_handler=session.handle_request,
                    timeout=5,
                )
                break
            except (OSError, ConnectionError, TimeoutError) as error:
                last_error = error
                await asyncio.sleep(min(0.25 * 1.4**attempt, 5))
        else:
            raise ConnectionError("Guest control channel did not become ready") from last_error
        try:
            gh = await github_token()
            session.github = gh
            session.add_secret(gh.token.get_secret_value() if gh else "")
            await session.guest.credentials_set(
                {
                    "openai": runtime.model_dump(),
                    "secrets": secrets,
                    "github_token": gh.token.get_secret_value() if gh else None,
                }
            )
            await session.guest.request("codex_start")
            session.codex = CodexClient(session.guest)
            return session
        except BaseException:
            await session.close()
            raise


class RealSession:
    def __init__(
        self,
        auth: OpenAIAuthManager,
        runtime: RuntimeCredentials,
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
        timeout: int,
        secrets: list[str],
    ) -> None:
        self.auth, self.runtime, self.github_token = auth, runtime, github_token
        self.log, self.observe, self.timeout = log, observe, timeout
        self._secret_values = secrets + [runtime.access_token]
        self.redactor = SecretRedactor(self._secret_values)
        self.guest: GuestClient
        self.codex: CodexClient
        self.thread_id = ""
        self.github: InstallationToken | None = None
        self.account_id: str | None = None
        self.last_check = 0.0

    def add_secret(self, value: str) -> None:
        if value and value not in self._secret_values:
            self._secret_values.append(value)
            self.redactor.replace_values(self._secret_values)

    async def handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "openai_refresh_required":
            runtime = await self.auth.runtime_credentials(self.account_id, force_refresh=True)
            self.add_secret(runtime.access_token)
            self.runtime = runtime
            return runtime.model_dump()
        raise ValueError(f"Unsupported guest request: {method}")

    async def initialize(self, thread_id: str | None, model: str) -> str:
        accounts = await self.auth.accounts()
        if accounts:
            self.account_id = accounts[0].id
        await self.codex.initialize()
        self.thread_id = await self.codex.start_thread(model or None, thread_id=thread_id)
        return self.thread_id

    async def usage(self) -> list[UsageWindow]:
        try:
            windows = normalize_rate_limits(await self.codex.read_rate_limits())
            if windows:
                await self.observe(windows)
            return windows
        except RpcError:
            return []

    async def _rotate(
        self, runtime: RuntimeCredentials | None, github: InstallationToken | None, model: str
    ) -> None:
        if runtime:
            self.add_secret(runtime.access_token)
            await self.guest.request("openai_token_rotate", runtime.model_dump())
            self.runtime = runtime
        if github:
            self.add_secret(github.token.get_secret_value())
            await self.guest.request(
                "github_token_rotate", {"token": github.token.get_secret_value()}
            )
            self.github = github
        if runtime or github:
            await self.codex.initialize()
            self.thread_id = await self.codex.start_thread(model or None, thread_id=self.thread_id)
            await self.log("Runtime credentials renewed; existing Codex thread resumed.")

    async def turn(self, prompt: str, model: str, effort: str, cancel: asyncio.Event) -> RunReport:
        # Check credentials before issuing another side-effecting turn.
        runtime = await self.auth.runtime_credentials(self.account_id)
        gh = await self.github_token()
        await self._rotate(
            runtime if runtime.access_token != self.runtime.access_token else None,
            gh if gh != self.github else None,
            model,
        )
        try:
            turn_id = await self.codex.start_turn(
                self.thread_id, prompt, model or None, effort, RunReport.model_json_schema()
            )
        except RpcError as error:
            if is_provider_limit(error.data) or is_provider_limit(str(error)):
                raise ProviderLimited("Provider usage limit prevents further work") from error
            raise
        text_parts: list[str] = []
        completed_text: list[str] = []
        pending_runtime: RuntimeCredentials | None = None
        pending_github: InstallationToken | None = None
        interrupted_at: float | None = None
        async with asyncio.timeout(self.timeout):
            while True:
                if self.guest.closed.is_set():
                    raise ConnectionError("Guest/Codex connection lost; turn was not replayed")
                now = time.monotonic()
                if now - self.last_check > 30:
                    self.last_check = now
                    candidate = await self.auth.runtime_credentials(self.account_id)
                    if candidate.access_token != self.runtime.access_token:
                        pending_runtime = candidate
                    candidate_gh = await self.github_token()
                    if candidate_gh != self.github:
                        pending_github = candidate_gh
                    await self.usage()
                if (
                    cancel.is_set() or pending_runtime or pending_github
                ) and interrupted_at is None:
                    await self.codex.interrupt(self.thread_id, turn_id)
                    interrupted_at = now
                if interrupted_at and now - interrupted_at > 60:
                    raise TimeoutError(
                        "Codex did not acknowledge interrupt; state retained for recovery"
                    )
                try:
                    event = await asyncio.wait_for(self.codex.notifications.get(), 2)
                except TimeoutError:
                    continue
                params = event.params
                if event.method == "account/rateLimits/updated":
                    windows = normalize_rate_limits(params)
                    if windows:
                        await self.observe(windows)
                elif event.method == "item/agentMessage/delta":
                    text_parts.append(str(params.get("delta", "")))
                elif event.method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        completed_text.append(str(item.get("text", "")))
                    # Completed units avoid fragments that can split secrets.
                    safe = self.redactor.redact(json.dumps(item, ensure_ascii=False))
                    await self.log(safe)
                elif event.method == "error":
                    await self.log(self.redactor.redact(json.dumps(params)))
                elif event.method == "turn/completed":
                    turn = params.get("turn", {})
                    if turn.get("id") != turn_id:
                        continue
                    status = turn.get("status")
                    if status == "failed":
                        if is_provider_limit(turn.get("error")):
                            raise ProviderLimited("Provider usage limit prevents further work")
                        raise RuntimeError(self.redactor.redact(json.dumps(turn.get("error"))))
                    if status == "interrupted":
                        if pending_runtime or pending_github:
                            await self._rotate(pending_runtime, pending_github, model)
                            return RunReport(
                                summary="Turn interrupted for credential renewal.",
                                remaining=[
                                    "Inspect current changes and process state before continuing."
                                ],
                                suggested_next_action=(
                                    "Continue from observed state; do not replay actions."
                                ),
                            )
                        if cancel.is_set():
                            raise asyncio.CancelledError
                        raise RuntimeError("Codex turn unexpectedly interrupted")
                    if status != "completed":
                        raise RuntimeError(f"Unexpected turn status: {status}")
                    report = parse_report("\n".join(completed_text) or "".join(text_parts))
                    return RunReport.model_validate_json(
                        self.redactor.redact(report.model_dump_json())
                    )

    async def close(self) -> None:
        if hasattr(self, "guest"):
            with contextlib.suppress(Exception):
                async with asyncio.timeout(20):
                    await self.guest.request("codex_stop")
                    await self.guest.request("credentials_clear")
                    await self.guest.request("shutdown")
            await self.guest.close()
