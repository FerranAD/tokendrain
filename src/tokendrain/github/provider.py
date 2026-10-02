"""GitHub App broker. Only scoped installation tokens cross the VM boundary."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx
import jwt
from pydantic import BaseModel, Field, SecretStr, field_validator


class CredentialStore(Protocol):
    async def put(self, name: str, value: bytes) -> None: ...
    async def get(self, name: str) -> bytes | None: ...
    async def delete(self, name: str) -> None: ...


class GitHubConfig(BaseModel):
    app_id: str = Field(min_length=1, max_length=100)
    private_key: SecretStr
    app_slug: str = ""


class IntegrationInput(BaseModel):
    installation_id: int = Field(gt=0)
    repository_id: int = Field(gt=0)
    repository_name: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    permissions: dict[str, Literal["read", "write"]]

    @field_validator("permissions")
    @classmethod
    def valid_permissions(
        cls, value: dict[str, Literal["read", "write"]]
    ) -> dict[str, Literal["read", "write"]]:
        if not value or set(value) - {"contents", "pull_requests", "issues", "actions"}:
            raise ValueError("Select contents, pull_requests, issues or actions permissions")
        if value.get("actions") == "write":
            raise ValueError("Only actions:read is supported")
        return value


class InstallationToken(BaseModel):
    token: SecretStr
    expires_at: datetime
    permissions: dict[str, str] = Field(default_factory=dict)


class GitHubProvider:
    def __init__(self, store: CredentialStore, http: httpx.AsyncClient) -> None:
        self.store, self.http = store, http
        self._locks: dict[str, asyncio.Lock] = {}
        self._tokens: dict[str, InstallationToken] = {}

    async def _jwt(self, app_id: str, credential_ref: str) -> str:
        key = await self.store.get(credential_ref)
        if not key:
            raise ValueError("GitHub App private key is missing")
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": app_id}, key, algorithm="RS256"
        )

    async def _request(
        self, method: str, path: str, bearer: str, body: dict[str, Any] | None = None
    ) -> Any:
        response = await self.http.request(
            method,
            "https://api.github.com" + path,
            headers={
                "Authorization": "Bearer " + bearer,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    async def inspect_app(self, app_id: str, ref: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", "/app", await self._jwt(app_id, ref))
        return result

    async def installations(self, app_id: str, ref: str) -> list[dict[str, Any]]:
        bearer = await self._jwt(app_id, ref)
        output: list[dict[str, Any]] = []
        for page in range(1, 1001):
            items = await self._request(
                "GET", f"/app/installations?per_page=100&page={page}", bearer
            )
            output.extend(
                {
                    "id": item["id"],
                    "account": item["account"]["login"],
                    "permissions": item.get("permissions", {}),
                }
                for item in items
            )
            if len(items) < 100:
                return output
        raise ValueError("GitHub installation pagination limit exceeded")

    async def repositories(
        self, app_id: str, ref: str, installation_id: int
    ) -> list[dict[str, Any]]:
        # Discovery token deliberately grants metadata only; never enters a guest.
        bearer = await self._jwt(app_id, ref)
        issued = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            bearer,
            {"permissions": {"metadata": "read"}},
        )
        output: list[dict[str, Any]] = []
        for page in range(1, 1001):
            result = await self._request(
                "GET", f"/installation/repositories?per_page=100&page={page}", issued["token"]
            )
            items = result["repositories"]
            output.extend(
                {
                    "id": item["id"],
                    "full_name": item["full_name"],
                    "private": item["private"],
                    "default_branch": item["default_branch"],
                }
                for item in items
            )
            if len(items) < 100:
                return output
        raise ValueError("GitHub repository pagination limit exceeded")

    async def token(
        self, app_id: str, ref: str, integration: IntegrationInput
    ) -> InstallationToken:
        cache_key = f"{app_id}:{integration.model_dump_json()}"
        async with self._locks.setdefault(cache_key, asyncio.Lock()):
            current = self._tokens.get(cache_key)
            if current and (current.expires_at - datetime.now(UTC)).total_seconds() > 300:
                return current
            value = await self._request(
                "POST",
                f"/app/installations/{integration.installation_id}/access_tokens",
                await self._jwt(app_id, ref),
                {
                    "repository_ids": [integration.repository_id],
                    "permissions": integration.permissions,
                },
            )
            issued = InstallationToken.model_validate(value)
            self._tokens[cache_key] = issued
            return issued

    def clear_cache(self) -> None:
        self._tokens.clear()
