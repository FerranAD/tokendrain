import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from tokendrain.credentials.store import EncryptedFileCredentialStore
from tokendrain.github.provider import GitHubProvider, IntegrationInput


async def test_jwt_scoped_tokens_and_refresh_lock(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    store = EncryptedFileCredentialStore(tmp_path, b"x" * 32)
    await store.put("key", private)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        claims = jwt.decode(
            request.headers["authorization"].removeprefix("Bearer "),
            key.public_key(),
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        assert claims["iss"] == "1234"
        assert 0 < claims["exp"] - claims["iat"] <= 600
        assert request.headers["x-github-api-version"] == "2026-03-10"
        assert request.url.path == "/app/installations/7/access_tokens"
        assert json.loads(request.content) == {
            "repository_ids": [99],
            "permissions": {"contents": "write", "issues": "read"},
        }
        return httpx.Response(
            201,
            json={
                "token": "short-lived-installation-token",
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                "permissions": {"contents": "write", "issues": "read"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = GitHubProvider(store, http)
        integration = IntegrationInput(
            installation_id=7,
            repository_id=99,
            repository_name="owner/repo",
            permissions={"contents": "write", "issues": "read"},
        )
        results = await asyncio.gather(
            *(provider.token("1234", "key", integration) for _ in range(12))
        )
        assert len(requests) == 1
        assert all(
            result.token.get_secret_value() == "short-lived-installation-token"
            for result in results
        )
        assert "short-lived-installation-token" not in repr(results[0])


async def test_github_api_setup_scope_validation_and_key_rotation(tmp_path: Path) -> None:
    from asgi_lifespan import LifespanManager

    from tokendrain.api.app import create_app
    from tokendrain.application import Overrides
    from tokendrain.config import Settings

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"id": 123, "slug": "test-app"})
        if path == "/app/installations":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "account": {"login": "owner"},
                        "permissions": {"contents": "read", "issues": "write"},
                    }
                ],
            )
        if path == "/app/installations/7/access_tokens":
            assert json.loads(request.content) == {"permissions": {"metadata": "read"}}
            return httpx.Response(201, json={"token": "metadata-only"})
        if path == "/installation/repositories":
            assert request.headers["authorization"] == "Bearer metadata-only"
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 99,
                            "full_name": "owner/repo",
                            "private": True,
                            "default_branch": "main",
                        }
                    ]
                },
            )
        raise AssertionError(path)

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    token_file = tmp_path / "admin-token"
    token_file.write_text("t" * 48)
    token_file.chmod(0o600)
    app = create_app(
        Settings(
            state_dir=tmp_path,
            backend="mock",
            public_url="http://testserver",
            admin_token_file=token_file,
        ),
        Overrides(http=upstream, start_workers=False),
    )
    async with LifespanManager(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tokendrain-Request": "1"},
        ) as client:
            await client.post(
                "/api/v1/session", json={"token": (tmp_path / "admin-token").read_text()}
            )
            configured = await client.put(
                "/api/v1/integrations/github", json={"app_id": "123", "private_key": private}
            )
            assert configured.status_code == 200, configured.text
            assert "PRIVATE KEY" not in configured.text
            assert configured.json()["installations"][0]["id"] == "7"
            project = (
                await client.post("/api/v1/projects", json={"name": "GitHub project"})
            ).json()["id"]
            binding = {
                "installation_id": "7",
                "repository_id": 99,
                "repository_name": "owner/repo",
                "permissions": {"contents": "write"},
            }
            denied = await client.put(f"/api/v1/projects/{project}/github", json=binding)
            before = len((await client.get("/api/v1/projects")).json())
            invalid_create = await client.post(
                "/api/v1/projects", json={"name": "Denied integration", "github": binding}
            )
            assert invalid_create.status_code == 409, invalid_create.text
            assert len((await client.get("/api/v1/projects")).json()) == before
            assert denied.status_code == 409 and "does not grant" in denied.text
            binding["permissions"] = {"contents": "read", "issues": "write"}
            attached = await client.put(f"/api/v1/projects/{project}/github", json=binding)
            assert attached.status_code == 200, attached.text
            assert attached.json()["permissions"] == binding["permissions"]
            created = await client.post(
                "/api/v1/projects",
                json={
                    "name": "Integrated from creation",
                    "default_model": "codex-model",
                    "default_reasoning_effort": "high",
                    "github": binding,
                    "initial_tasks": [{"title": "Approved task", "column": "todo"}],
                },
            )
            assert created.status_code == 201, created.text
            created_id = created.json()["id"]
            assert created.json()["default_model"] == "codex-model"
            initial_binding = (await client.get(f"/api/v1/projects/{created_id}/github")).json()
            assert initial_binding["permissions"] == binding["permissions"]
            tasks = (await client.get(f"/api/v1/projects/{created_id}/tasks")).json()
            assert tasks[0]["title"] == "Approved task"
            # A new key for the same App must retain the explicit project grant.
            assert (
                await client.put(
                    "/api/v1/integrations/github", json={"app_id": "123", "private_key": private}
                )
            ).status_code == 200
            assert (await client.get(f"/api/v1/projects/{project}/github")).json()[
                "repository_id"
            ] == 99
            binding["repository_id"] = 999
            assert (
                await client.put(f"/api/v1/projects/{project}/github", json=binding)
            ).status_code == 409
            assert (await client.delete(f"/api/v1/projects/{project}/github")).status_code == 204
