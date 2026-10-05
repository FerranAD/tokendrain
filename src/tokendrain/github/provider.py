"""GitHub App broker. Only scoped installation tokens cross the VM boundary."""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class CredentialStore(Protocol):
    async def put(self, name: str, value: bytes) -> None: ...
    async def get(self, name: str) -> bytes | None: ...
    async def delete(self, name: str) -> None: ...


AccessMode = Literal["read_only", "pull_requests", "direct_write"]
APP_PERMISSIONS = {
    "administration": "write",
    "contents": "write",
    "pull_requests": "write",
    "actions": "read",
    "checks": "read",
    "workflows": "write",
    "issues": "write",
}
MODE_GUIDANCE = {
    "read_only": "GitHub repository access is read-only; do not attempt to publish GitHub changes.",
    "pull_requests": "Work on a non-default branch, push that branch, and open or update a PR "
    "against the default branch. Direct writes to the default branch are not permitted.",
    "direct_write": "Repository write access permits direct publication when appropriate; "
    "PRs may still be used.",
}


def app_manifest(public_url: str, name: str) -> dict[str, Any]:
    origin = urlsplit(public_url)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.netloc
        or origin.username
        or origin.query
        or origin.fragment
    ):
        raise ValueError("Configure a valid browser-facing tokendrain public URL")
    base = public_url.rstrip("/")
    return {
        "name": name,
        "url": base,
        "redirect_url": base + "/api/v1/integrations/github/callback",
        "setup_url": base + "/api/v1/integrations/github/setup",
        # Manifest schema requires a URL even though delivery is disabled.
        "hook_attributes": {"url": base, "active": False},
        "default_events": [],
        "request_oauth_on_install": False,
        "setup_on_update": True,
        "public": True,
        "default_permissions": APP_PERMISSIONS,
    }


class RepositoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: int = Field(gt=0)
    access_mode: AccessMode = "pull_requests"
    allow_workflows: bool = False

    @model_validator(mode="after")
    def workflow_mode(self) -> "RepositoryInput":
        if self.access_mode == "read_only" and self.allow_workflows:
            raise ValueError("Workflow changes require writable repository access")
        return self


class IntegrationInput(RepositoryInput):
    installation_id: int = Field(gt=0)
    repository_name: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")

    @property
    def permissions(self) -> dict[str, str]:
        level = "read" if self.access_mode == "read_only" else "write"
        permissions = {
            "contents": level,
            "pull_requests": level,
            "issues": level,
            "actions": "read",
            "checks": "read",
        }
        if self.allow_workflows:
            permissions["workflows"] = "write"
        return permissions

    def binding(self) -> dict[str, Any]:
        return {**self.model_dump(), "permissions": self.permissions}


class InstallationToken(BaseModel):
    token: SecretStr
    expires_at: datetime
    permissions: dict[str, str] = Field(default_factory=dict)

    def assert_guest_safe(self) -> None:
        if "administration" in self.permissions:
            raise ValueError("Administration credentials must never be sent to guestd")


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
                **({"Authorization": "Bearer " + bearer} if bearer else {}),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return response.json() if response.content else None

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
                if not item.get("suspended_at")
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
            issued.assert_guest_safe()
            if any(
                name != "metadata"
                and (
                    name not in integration.permissions
                    or (level == "write" and integration.permissions[name] != "write")
                )
                for name, level in issued.permissions.items()
            ):
                raise ValueError("GitHub returned broader guest permissions than requested")
            self._tokens[cache_key] = issued
            return issued

    async def convert_manifest(self, code: str) -> dict[str, Any]:
        from urllib.parse import quote

        result: dict[str, Any] = await self._request(
            "POST", "/app-manifests/" + quote(code, safe="") + "/conversions", ""
        )
        return result

    async def policy_token(
        self, app_id: str, ref: str, installation_id: int, repository_id: int
    ) -> str:
        # Separate uncached host token. Never represented as an InstallationToken.
        value = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            await self._jwt(app_id, ref),
            {"repository_ids": [repository_id], "permissions": {"administration": "write"}},
        )
        return str(value["token"])

    def clear_cache(self) -> None:
        self._tokens.clear()
