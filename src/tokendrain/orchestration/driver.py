"""A work session owns one guest connection and never blindly retries a turn."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from tokendrain.auth.openai import AccountInfo, RuntimeCredentials
from tokendrain.codex.client import CodexClient
from tokendrain.codex.rpc import RpcError, RpcNotification
from tokendrain.credentials.store import SecretRedactor
from tokendrain.domain import RunReport, UsageWindow
from tokendrain.github.provider import InstallationToken
from tokendrain.protocol import GuestClient
from tokendrain.usage import normalize_rate_limits
from tokendrain.vm.models import VmHandle

LogCallback = Callable[[str], Awaitable[None]]
UsageCallback = Callable[[list[UsageWindow]], Awaitable[None]]
TokenCallback = Callable[[], Awaitable[InstallationToken | None]]


class SessionAuth(Protocol):
    async def accounts(self) -> list[AccountInfo]: ...
    async def runtime_credentials(
        self, account_id: str | None = None, force_refresh: bool = False
    ) -> RuntimeCredentials: ...


class GuestConnection(Protocol):
    closed: asyncio.Event
    notifications: asyncio.Queue[RpcNotification]

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...
    async def credentials_set(self, credentials: dict[str, Any]) -> None: ...
    async def close(self) -> None: ...


class GuestConnector(Protocol):
    async def __call__(
        self,
        vsock_path: Path | str,
        port: int,
        *,
        request_handler: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        timeout: float,  # noqa: ASYNC109 - transport boundary configures handshake
    ) -> GuestConnection: ...


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


class ResumeRequired(Exception):
    """A transport/credential interruption is orchestration state, not an agent checkpoint."""


def is_provider_limit(value: object) -> bool:
    raw = json.dumps(value, default=str).lower()
    return any(
        token in raw
        for token in (
            "usagelimitexceeded",
            "ratelimitexceeded",
            "usage_limit",
            "usage limit reached",
            "subscription_sharing_usage_unavailable",
        )
    )


def parse_report(text: str) -> RunReport:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.partition("\n")[2].rsplit("```", 1)[0]
    try:
        return RunReport.model_validate_json(cleaned)
    except (ValueError, ValidationError) as error:
        raise ValueError("Codex final answer was not a valid structured checkpoint") from error


def report_output_schema() -> dict[str, Any]:
    """Provider-compatible strict report schema; observed usage is host-owned."""
    text_array = {"type": "array", "items": {"type": "string"}}
    changes = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "files_changed": {"type": "integer"},
            "insertions": {"type": "integer"},
            "deletions": {"type": "integer"},
            "commits": text_array,
        },
        "required": ["files_changed", "insertions", "deletions", "commits"],
    }
    properties = {
        "status": {
            "type": "string",
            "enum": ["in_progress", "completed", "blocked", "failed", "cancelled"],
        },
        "summary": {"type": "string"},
        "completed": text_array,
        "remaining": text_array,
        "blockers": text_array,
        "changes": changes,
        "suggested_next_action": {"type": "string"},
        "task_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "id": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "description": {"type": ["string", "null"]},
                    "column": {
                        "type": ["string", "null"],
                        "enum": ["backlog", "todo", "in_progress", "done", None],
                    },
                    "position": {"type": ["integer", "null"]},
                },
                "required": ["id", "title", "description", "column", "position"],
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


def redact_value(value: Any, redactor: SecretRedactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, list):
        return [redact_value(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item, redactor) for key, item in value.items()}
    return value


class RealSessionFactory:
    def __init__(
        self,
        auth: SessionAuth,
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
    ) -> WorkSession:
        accounts = [account for account in await self.auth.accounts() if account.connected]
        if not accounts:
            raise ValueError("Connect an OpenAI account before starting a run")
        account_id = accounts[0].id
        runtime = await self.auth.runtime_credentials(account_id)
        session = RealSession(
            self.auth,
            runtime,
            github_token,
            log,
            observe,
            self.timeout,
            list(secrets.values()),
            connector=self.connector,
        )
        session.account_id = account_id
        session.handle = handle
        session.runtime_secrets = dict(secrets)
        last_error: Exception | None = None
        # Boot readiness is read-only and safely retryable, bounded to two minutes.
        for attempt in range(30):
            try:
                session.guest = await self.connector(
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
        auth: SessionAuth,
        runtime: RuntimeCredentials,
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
        timeout: int,
        secrets: list[str],
        *,
        connector: GuestConnector = GuestClient.connect,
    ) -> None:
        self.auth, self.runtime, self.github_token = auth, runtime, github_token
        self.connector = connector
        self.log, self.observe, self.timeout = log, observe, timeout
        self._secret_values = secrets + [runtime.access_token]
        self.redactor = SecretRedactor(self._secret_values)
        self.guest: GuestConnection
        self.codex: CodexClient
        self.thread_id = ""
        self.github: InstallationToken | None = None
        self.account_id: str | None = None
        self.last_check = 0.0
        self.handle: VmHandle | None = None
        self.runtime_secrets: dict[str, str] = {}
        self.recoveries = 0

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
        if self.account_id is None:
            accounts = [account for account in await self.auth.accounts() if account.connected]
            if not accounts:
                raise ValueError("No connected OpenAI account")
            self.account_id = accounts[0].id
        await self.codex.initialize()
        if thread_id:
            await self.codex.read_thread(thread_id)
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

    async def recover(self, model: str) -> RunReport:
        """Reconnect, inspect saved state, and resume; never replay the lost turn."""
        if not self.handle or not self.thread_id or self.recoveries >= 2:
            raise ConnectionError("Codex recovery exhausted; persisted state needs inspection")
        self.recoveries += 1
        await self.guest.close()
        for attempt in range(3):
            try:
                self.guest = await self.connector(
                    self.handle.vsock_path,
                    self.handle.guest_port,
                    request_handler=self.handle_request,
                    timeout=5,
                )
                runtime = await self.auth.runtime_credentials(self.account_id)
                github = await self.github_token()
                self.add_secret(runtime.access_token)
                self.add_secret(github.token.get_secret_value() if github else "")
                await self.guest.credentials_set(
                    {
                        "openai": runtime.model_dump(),
                        "secrets": self.runtime_secrets,
                        "github_token": github.token.get_secret_value() if github else None,
                    }
                )
                await self.guest.request("codex_start")
                self.codex = CodexClient(self.guest)
                await self.codex.initialize()
                # Reading durable state must succeed before starting further work.
                await self.codex.read_thread(self.thread_id)
                await self.codex.start_thread(model or None, thread_id=self.thread_id)
                self.runtime, self.github = runtime, github
                await self.log(
                    "Codex restarted; durable thread inspected and resumed without replay."
                )
                return RunReport(
                    summary="Codex connection was interrupted; existing thread has been recovered.",
                    remaining=[
                        "Inspect workspace and running processes before choosing the next action."
                    ],
                    suggested_next_action=(
                        "Reconcile observed state; never replay the interrupted turn."
                    ),
                )
            except (ConnectionError, OSError, TimeoutError):
                await self.guest.close()
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5 * (2**attempt))
        raise AssertionError("unreachable")

    async def turn(self, prompt: str, model: str, effort: str, cancel: asyncio.Event) -> RunReport:
        try:
            return await self._turn(prompt, model, effort, cancel)
        except (ConnectionError, OSError):
            if cancel.is_set():
                raise asyncio.CancelledError from None
            recovered = await self.recover(model)
            raise ResumeRequired(recovered.summary) from None

    async def _turn(self, prompt: str, model: str, effort: str, cancel: asyncio.Event) -> RunReport:
        if cancel.is_set():
            raise asyncio.CancelledError
        # Check credentials before issuing another side-effecting turn.
        runtime = await self.auth.runtime_credentials(self.account_id)
        gh = await self.github_token()
        await self._rotate(
            runtime if runtime.access_token != self.runtime.access_token else None,
            gh if gh != self.github else None,
            model,
        )
        if cancel.is_set():
            raise asyncio.CancelledError
        try:
            turn_id = await self.codex.start_turn(
                self.thread_id, prompt, model or None, effort, report_output_schema()
            )
        except RpcError as error:
            if is_provider_limit(error.data) or is_provider_limit(str(error)):
                raise ProviderLimited("Provider usage limit prevents further work") from error
            raise
        final_text: str | None = None
        received_text = 0
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
                    async with asyncio.timeout(5):
                        await self.codex.interrupt(self.thread_id, turn_id)
                    interrupted_at = now
                if interrupted_at and now - interrupted_at > 10:
                    raise TimeoutError(
                        "Codex did not acknowledge interrupt; state retained for recovery"
                    )
                try:
                    event = await asyncio.wait_for(self.codex.notifications.get(), 2)
                except TimeoutError:
                    continue
                params = event.params
                if event.method in {"guest/failure", "guest/codex_exited"}:
                    raise ConnectionError("Guest reported Codex process/event channel failure")
                # Ignore unrelated persisted/subagent thread events.
                if params.get("threadId") not in {None, self.thread_id}:
                    continue
                if params.get("turnId") not in {None, turn_id}:
                    continue
                if event.method == "account/rateLimits/updated":
                    windows = normalize_rate_limits(params)
                    if windows:
                        await self.observe(windows)
                elif event.method == "item/agentMessage/delta":
                    delta = str(params.get("delta", ""))
                    received_text += len(delta.encode())
                    if received_text > 8 * 1024 * 1024:
                        raise RuntimeError("Codex report exceeded the 8 MiB text limit")
                    # Deltas may be commentary; only authoritative completed final items count.
                elif event.method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        completed = str(item.get("text", ""))
                        received_text += len(completed.encode())
                        if received_text > 8 * 1024 * 1024:
                            raise RuntimeError("Codex report exceeded the 8 MiB text limit")
                        if item.get("phase") == "final_answer":
                            final_text = completed
                        elif item.get("phase") is None:
                            # Older app-server versions lack phases. Accept only a valid report.
                            try:
                                parse_report(completed)
                            except ValueError:
                                pass
                            else:
                                final_text = completed
                    # Completed units avoid fragments that can split secrets.
                    safe = json.dumps(redact_value(item, self.redactor), ensure_ascii=False)
                    await self.log(safe)
                elif event.method == "error":
                    await self.log(self.redactor.redact(json.dumps({"type": "error", **params})))
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
                        if not cancel.is_set() and (pending_runtime or pending_github):
                            await self._rotate(pending_runtime, pending_github, model)
                            raise ResumeRequired(
                                "Turn interrupted for credential renewal; "
                                "inspect current state before continuing."
                            )
                        if cancel.is_set():
                            raise asyncio.CancelledError
                        raise RuntimeError("Codex turn unexpectedly interrupted")
                    if status != "completed":
                        raise RuntimeError(f"Unexpected turn status: {status}")
                    report = parse_report(final_text or "")
                    return RunReport.model_validate(
                        redact_value(report.model_dump(), self.redactor)
                    )

    async def close(self) -> None:
        try:
            if hasattr(self, "guest"):
                try:
                    async with asyncio.timeout(20):
                        await self.guest.request("codex_stop")
                        await self.guest.request("credentials_clear")
                        await self.guest.request("shutdown")
                finally:
                    # Close transport even when lifecycle RPC failed; callers
                    # must see that failure instead of claiming clean completion.
                    await self.guest.close()
        finally:
            self.runtime_secrets.clear()
