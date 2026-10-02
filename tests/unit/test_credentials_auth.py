from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa

from tokendrain.auth.codex_import import decode_import
from tokendrain.auth.openai import AccountRecord, OpenAIAuthManager, PendingAuthorization
from tokendrain.credentials import EncryptedFileCredentialStore, SecretRedactor, load_master_key


@pytest.fixture
def store(tmp_path: Path) -> EncryptedFileCredentialStore:
    return EncryptedFileCredentialStore(tmp_path / "credentials", secrets.token_bytes(32))


async def test_encryption_roundtrip_and_name_binding(store: EncryptedFileCredentialStore) -> None:
    await store.put("one", b"highly confidential")
    assert await store.get("one") == b"highly confidential"
    ciphertext = (store.directory / "one").read_bytes()
    assert b"highly confidential" not in ciphertext
    assert (store.directory / "one").stat().st_mode & 0o777 == 0o600
    (store.directory / "two").write_bytes(ciphertext)
    with pytest.raises(InvalidTag):
        await store.get("two")
    await store.put("one", b"replacement")
    assert await store.get("one") == b"replacement"
    await store.delete("one")
    await store.delete("one")
    assert await store.get("one") is None
    with pytest.raises(ValueError):
        await store.put("../../escape", b"bad")


def test_key_permissions_and_base64(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    path = tmp_path / "key"
    path.write_bytes(base64.urlsafe_b64encode(key))
    path.chmod(0o600)
    assert load_master_key(path) == key
    path.chmod(0o644)
    with pytest.raises(ValueError, match="accessible"):
        load_master_key(path)


def test_redaction_overlapping_values() -> None:
    redactor = SecretRedactor(["abcdef", "abc", "", "✓token"])
    assert redactor.redact("abcdef abc ✓token") == "[REDACTED] [REDACTED] [REDACTED]"


async def test_refresh_is_serialized_and_saved_atomically(
    store: EncryptedFileCredentialStore,
) -> None:
    refreshes = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        refreshes += 1
        assert parse_qs(request.content.decode())["refresh_token"] == ["old-refresh"]
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "access_token": "replacement",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    record = AccountRecord(
        id="account",
        method="siwc",
        subject="subject",
        client_id="oaiapp_client",
        expires_at=0,
        access_token="old-access",
        refresh_token="old-refresh",
        scopes=["chatgpt.tokens.use.direct"],
    )
    await store.put("openai-account", record.model_dump_json().encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        manager = OpenAIAuthManager(store, http, "host-1")
        results = await asyncio.gather(*(manager.runtime_credentials("account") for _ in range(20)))
        assert refreshes == 1
        assert all(result.access_token == "replacement" for result in results)
        public = (await manager.accounts())[0].model_dump()
        assert "access_token" not in public and "refresh_token" not in public
    stored = await store.get("openai-account")
    assert stored and b"new-refresh" in stored
    assert all("refresh" not in key for key in results[0].model_dump())


async def test_pkce_oidc_full_flow_and_replay_rejected(store: EncryptedFileCredentialStore) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "test-key"
    nonce = ""
    expected_challenge = ""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "jwks_uri": "https://auth.openai.com/keys",
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": [jwk]})
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["oaiapp_issued"]
        assert form["redirect_uri"] == ["http://127.0.0.1:8742/auth/callback"]
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest())
            .decode()
            .rstrip("=")
        )
        assert digest == expected_challenge
        now = int(time.time())
        id_token = jwt.encode(
            {
                "iss": "https://auth.openai.com",
                "sub": "user",
                "aud": "oaiapp_issued",
                "iat": now,
                "exp": now + 3600,
                "nonce": nonce,
                "email": "test@example.test",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        return httpx.Response(
            200,
            json={
                "id_token": id_token,
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": "openid chatgpt.tokens.use.direct offline_access",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        manager = OpenAIAuthManager(store, http, "stable-host")
        attempt = await manager.begin_sign_in("http://127.0.0.1:8742/auth/callback")
        query = parse_qs(urlparse(attempt.url).query)
        assert query["client_id"] == ["dynamic_agent_client"]
        assert query["ext_agent_host_id"] == ["stable-host"]
        expected_challenge = query["code_challenge"][0]
        nonce = query["nonce"][0]
        info = await manager.complete_sign_in(attempt.state, "code", "oaiapp_issued")
        assert info.subject == "user"
        assert "access_token" not in info.model_dump()
        with pytest.raises(ValueError, match="consumed"):
            await manager.complete_sign_in(attempt.state, "code", "oaiapp_issued")
        returning = await manager.begin_sign_in("http://127.0.0.1:9999/auth/callback", info.id)
        returning_query = parse_qs(urlparse(returning.url).query)
        assert returning_query["client_id"] == ["oaiapp_issued"]
        assert "agent_name_hint" not in returning_query
        with pytest.raises(ValueError, match="different client_id"):
            await manager.complete_sign_in(returning.state, "code", "malicious-client")


async def test_oidc_rejects_nonce_mismatch(store: EncryptedFileCredentialStore) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "key"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://auth.openai.com",
                    "jwks_uri": "https://auth.openai.com/keys",
                },
            )
        return httpx.Response(200, json={"keys": [jwk]})

    token = jwt.encode(
        {
            "iss": "https://auth.openai.com",
            "aud": "client",
            "sub": "user",
            "nonce": "wrong",
            "iat": int(time.time()),
            "exp": int(time.time()) + 30,
        },
        key,
        algorithm="RS256",
        headers={"kid": "key"},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(ValueError, match="nonce"):
            await OpenAIAuthManager(store, http, "host")._validate_identity(
                token, "client", "right"
            )


async def test_invalid_callback_and_denial_do_not_exchange(
    store: EncryptedFileCredentialStore,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("must not make HTTP request"))
    ) as http:
        manager = OpenAIAuthManager(store, http, "host")
        with pytest.raises(ValueError, match="127.0.0.1"):
            await manager.begin_sign_in("https://public.example/auth/callback")
        attempt = await manager.begin_sign_in("http://127.0.0.1:8742/auth/callback")
        raw = await store.get(f"pending-{attempt.state}")
        assert raw
        assert PendingAuthorization.model_validate_json(raw).expires_at > time.time()
        with pytest.raises(ValueError, match="declined"):
            await manager.complete_sign_in(attempt.state, error="access_denied")


def test_import_adapter_extracts_only_runtime_token() -> None:
    token = jwt.encode(
        {
            "exp": time.time() + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": "workspace"},
        },
        "x" * 32,
    )
    decoded = decode_import(
        json.dumps({"tokens": {"access_token": token, "refresh_token": "very-secret"}}).encode()
    )
    assert decoded["refresh_token"] == ""
    assert decoded["chatgpt_account_id"] == "workspace"
    with pytest.raises(ValueError, match="API keys"):
        decode_import(b'{"OPENAI_API_KEY":"secret"}')


async def test_refresh_failure_keeps_last_encrypted_state_and_never_returns_expired_token(
    store: EncryptedFileCredentialStore,
) -> None:
    record = AccountRecord(
        id="account",
        method="siwc",
        subject="user",
        client_id="oaiapp_client",
        expires_at=0,
        access_token="expired",
        refresh_token="refresh-credential",
        scopes=["chatgpt.tokens.use.direct"],
    )
    original = record.model_dump_json().encode()
    await store.put("openai-account", original)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "sensitive": "do-not-log"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        manager = OpenAIAuthManager(store, http, "host")
        with pytest.raises(ValueError, match="reconnect") as error:
            await manager.runtime_credentials("account")
        assert "do-not-log" not in str(error.value)
    assert await store.get("openai-account") == original


async def test_signout_clears_local_tokens_after_remote_failure(
    store: EncryptedFileCredentialStore,
) -> None:
    record = AccountRecord(
        id="account",
        method="siwc",
        subject="user",
        client_id="oaiapp_client",
        expires_at=0,
        access_token="access",
        refresh_token="refresh",
        id_token="identity",
    )
    await store.put("openai-account", record.model_dump_json().encode())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        manager = OpenAIAuthManager(store, http, "host")
        assert not await manager.sign_out("account")
        raw = await store.get("openai-account")
        assert raw
        signed_out = AccountRecord.model_validate_json(raw)
        assert not signed_out.connected
        assert signed_out.access_token == signed_out.refresh_token == signed_out.id_token == ""
        with pytest.raises(ValueError, match="signed out"):
            await manager.runtime_credentials("account")


async def test_reimport_deduplicates_account_refresh_ownership(
    store: EncryptedFileCredentialStore,
) -> None:
    token = jwt.encode(
        {
            "exp": time.time() + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": "workspace"},
        },
        "x" * 32,
    )
    raw = json.dumps(
        {"auth_mode": "chatgpt", "tokens": {"access_token": token, "refresh_token": "refresh"}}
    ).encode()
    async with httpx.AsyncClient() as http:
        manager = OpenAIAuthManager(store, http, "host")
        accounts = await asyncio.gather(*(manager.import_auth_json(raw) for _ in range(5)))
        assert len({account.id for account in accounts}) == 1
        assert len(await manager.accounts()) == 1


async def test_default_runtime_account_skips_signed_out_accounts(
    store: EncryptedFileCredentialStore,
) -> None:
    for name, connected in [("a", False), ("b", True)]:
        record = AccountRecord(
            id=name,
            method="siwc",
            subject=name,
            access_token=name,
            expires_at=time.time() + 3600,
            connected=connected,
        )
        await store.put(f"openai-{name}", record.model_dump_json().encode())
    async with httpx.AsyncClient() as http:
        manager = OpenAIAuthManager(store, http, "host")
        assert (await manager.runtime_credentials()).access_token == "b"


def test_redaction_covers_json_embedded_secret_values() -> None:
    secret = 'private"value\nline'
    redactor = SecretRedactor([secret])
    encoded = json.dumps({"summary": secret})
    assert redactor.redact(encoded) == '{"summary": "[REDACTED]"}'
