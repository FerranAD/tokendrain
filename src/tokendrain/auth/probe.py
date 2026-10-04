"""Read account model/usage metadata without a VM or inference turn."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from tokendrain.codex.client import CodexClient
from tokendrain.codex.rpc import RpcError
from tokendrain.credentials.store import SecretRedactor
from tokendrain.logging import sanitize

from .openai import OpenAIAuthManager
from .process import auth_runtime_directory, isolated_codex

log = logging.getLogger(__name__)


def requires_reauthentication(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 401
    message = str(error).lower()
    return any(
        hint in message
        for hint in (
            "401",
            "invalid id token",
            "invalid access token",
            "expired",
            "revoked",
            "unauthorized",
            "usable codex chatgpt access token",
        )
    )


class ModelChoice(BaseModel):
    id: str
    display_name: str
    description: str = ""
    is_default: bool = False
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[str] = Field(default_factory=list)


class AccountProbeResult(BaseModel):
    account_id: str
    observed_at: float
    models: list[ModelChoice] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    usage_error: str | None = None
    reauth_required: bool = False


def parse_model(model: dict[str, Any]) -> ModelChoice:
    identifier = str(model.get("model") or model["id"])
    return ModelChoice(
        id=identifier,
        display_name=str(model.get("displayName") or identifier),
        description=str(model.get("description") or ""),
        is_default=bool(model.get("isDefault", False)),
        default_reasoning_effort=model.get("defaultReasoningEffort"),
        supported_reasoning_efforts=[
            str(item["reasoningEffort"]) for item in model.get("supportedReasoningEfforts", [])
        ],
    )


class AuthProbe:
    def __init__(
        self,
        auth: OpenAIAuthManager,
        http: httpx.AsyncClient,
        runtime_dir: Path | None = None,
        executable: str = "codex",
    ) -> None:
        self.auth, self.http = auth, http
        self.runtime_dir = runtime_dir or auth_runtime_directory()
        self.executable = executable
        self._lock = asyncio.Lock()

    async def read(self, account_id: str | None = None) -> AccountProbeResult:
        # One read-only probe subprocess at a time; no leaked periodic tasks.
        async with self._lock, asyncio.timeout(90):
            accounts = await self.auth.accounts()
            selected = next((account for account in accounts if account.id == account_id), None)
            if account_id is None:
                selected = next((account for account in accounts if account.connected), None)
            if selected is None:
                raise ValueError("connect an OpenAI account before reading usage and models")
            runtime = await self.auth.runtime_credentials(selected.id)
            result = AccountProbeResult(account_id=selected.id, observed_at=time.time())
            async with isolated_codex(self.runtime_dir, executable=self.executable) as (peer, _):
                client = CodexClient(peer)
                stage = "initialize"
                try:
                    await client.initialize()
                    stage = "account/login/start"
                    await peer.request(
                        stage,
                        {
                            "type": "chatgptAuthTokens",
                            "accessToken": runtime.access_token,
                            "chatgptAccountId": runtime.account_id,
                            "chatgptPlanType": runtime.plan_type,
                        },
                    )
                    stage = "model/list"
                    result.models = [parse_model(model) for model in await client.models()]
                    stage = "account/rateLimits/read"
                    result.usage = await client.read_rate_limits()
                except RpcError as exc:
                    # Expected provider/protocol rejection is metadata unavailability,
                    # not an application failure. Never log RPC data or supplied tokens.
                    log.warning(
                        "Codex metadata probe failed at %s (RPC code %s): %s",
                        stage,
                        exc.code,
                        sanitize(str(exc), SecretRedactor([runtime.access_token]))[:500],
                    )
                    result.usage_error = (
                        f"Codex metadata unavailable: {stage} was rejected (RPC code {exc.code}). "
                        "Check the host Codex version and account credentials in Settings."
                    )
                    result.reauth_required = requires_reauthentication(exc)
                    if result.reauth_required:
                        result.usage_error = (
                            "Codex could not authenticate with the imported credentials. "
                            "Run codex logout, then codex login on your Codex computer, "
                            "and replace auth.json in Settings."
                        )
                return result
