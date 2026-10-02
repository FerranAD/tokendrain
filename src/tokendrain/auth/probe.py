"""Read account model/usage metadata without a VM or inference turn."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from tokendrain.codex.client import CodexClient
from tokendrain.codex.rpc import RpcError

from .openai import OpenAIAuthManager
from .process import auth_runtime_directory, isolated_codex


class ModelChoice(BaseModel):
    id: str
    display_name: str
    description: str = ""
    default_reasoning_effort: str | None = None
    supported_reasoning_efforts: list[str] = Field(default_factory=list)


class AccountProbeResult(BaseModel):
    account_id: str
    observed_at: float
    models: list[ModelChoice] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    usage_error: str | None = None


def parse_model(model: dict[str, Any], *, siwc: bool = False) -> ModelChoice:
    identifier = str(model["slug"] if siwc else model.get("model", model["id"]))
    efforts = (
        model.get("supported_reasoning_levels", [])
        if siwc
        else model.get("supportedReasoningEfforts", [])
    )
    return ModelChoice(
        id=identifier,
        display_name=str(model.get("display_name" if siwc else "displayName") or identifier),
        description=str(model.get("description") or ""),
        default_reasoning_effort=model.get(
            "default_reasoning_level" if siwc else "defaultReasoningEffort"
        ),
        supported_reasoning_efforts=[
            str(item["effort"] if siwc else item["reasoningEffort"]) for item in efforts
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
            if runtime.mode == "siwc":
                response = await self.http.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {runtime.access_token}"},
                )
                if response.is_error:
                    raise ValueError(
                        f"OpenAI model catalog request failed (HTTP {response.status_code})"
                    )
                payload = response.json()
                result.models = [
                    parse_model(model, siwc=True)
                    for model in payload["models"]
                    if model.get("visibility") == "list"
                ]
                result.usage_error = (
                    "The Sign in with ChatGPT Responses provider does not publish usage windows "
                    "through the documented account/rateLimits/read interface. "
                    "Use duration/provider-limit stopping rules or import a Codex ChatGPT account."
                )
                return result
            async with isolated_codex(self.runtime_dir, executable=self.executable) as (peer, _):
                client = CodexClient(peer)
                await client.initialize()
                await peer.request(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": runtime.access_token,
                        "chatgptAccountId": runtime.account_id,
                        "chatgptPlanType": runtime.plan_type,
                    },
                )
                result.models = [parse_model(model) for model in await client.models()]
                try:
                    result.usage = await client.read_rate_limits()
                except RpcError:
                    result.usage_error = "Codex could not read usage windows for this account."
                return result
