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
