from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.exceptions import InvalidTag

from tokendrain.auth.codex_import import decode_import
from tokendrain.auth.openai import AccountRecord, OpenAIAuthManager
from tokendrain.credentials import EncryptedFileCredentialStore, SecretRedactor, load_master_key

HOST_ID = "urn:uuid:12345678-1234-4234-9234-123456789abc"


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
        manager = OpenAIAuthManager(store, http)
        accounts = await asyncio.gather(*(manager.import_auth_json(raw) for _ in range(5)))
        assert len({account.id for account in accounts}) == 1
        assert len(await manager.accounts()) == 1


async def test_default_runtime_account_skips_signed_out_accounts(
    store: EncryptedFileCredentialStore,
) -> None:
    for name, connected in [("a", False), ("b", True)]:
        record = AccountRecord(
            id=name,
            method="import",
            subject=name,
            access_token=name,
            expires_at=time.time() + 3600,
            connected=connected,
        )
        await store.put(f"openai-{name}", record.model_dump_json().encode())
    async with httpx.AsyncClient() as http:
        manager = OpenAIAuthManager(store, http)
        assert (await manager.runtime_credentials()).access_token == "b"


def test_redaction_covers_json_embedded_secret_values() -> None:
    secret = 'private"value\nline'
    redactor = SecretRedactor([secret])
    encoded = json.dumps({"summary": secret})
    assert redactor.redact(encoded) == '{"summary": "[REDACTED]"}'


@pytest.mark.parametrize("later_action", ["replace", "delete"])
async def test_cancelled_credential_write_cannot_overtake_next_mutation(
    tmp_path: Path,
    later_action: str,
) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()

    class SlowStore(EncryptedFileCredentialStore):
        def _write(self, name: str, value: bytes) -> None:
            if value == b"first":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test worker was not released")
            super()._write(name, value)

    store = SlowStore(tmp_path / "credentials", secrets.token_bytes(32))
    first = asyncio.create_task(store.put("credential", b"first"))
    later = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        later = asyncio.create_task(
            store.put("credential", b"replacement")
            if later_action == "replace"
            else store.delete("credential")
        )
        await asyncio.sleep(0)
        assert not first.done() and not later.done()
        # Repeated owner cancellation still cannot release the credential lock.
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await later
        assert await store.get("credential") == (
            b"replacement" if later_action == "replace" else None
        )
    finally:
        release.set()
        await asyncio.gather(first, *([later] if later else []), return_exceptions=True)


async def test_cancelled_credential_delete_cannot_remove_replacement(tmp_path: Path) -> None:
    import threading

    entered, release = threading.Event(), threading.Event()

    class SlowStore(EncryptedFileCredentialStore):
        def _delete(self, name: str) -> None:
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test worker was not released")
            super()._delete(name)

    store = SlowStore(tmp_path / "credentials", secrets.token_bytes(32))
    await store.put("credential", b"old")
    deleting = asyncio.create_task(store.delete("credential"))
    replacing = None
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        deleting.cancel()
        replacing = asyncio.create_task(store.put("credential", b"new"))
        await asyncio.sleep(0)
        assert not deleting.done() and not replacing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await deleting
        await replacing
        assert await store.get("credential") == b"new"
    finally:
        release.set()
        await asyncio.gather(deleting, *([replacing] if replacing else []), return_exceptions=True)


async def test_import_refresh_is_serialized_and_persists_renewal(store, monkeypatch):
    from tokendrain.auth import codex_import

    refreshes = 0
    original = json.dumps(
        {
            "tokens": {
                "account_id": "workspace",
                "access_token": jwt.encode({"exp": time.time() - 3600}, "x" * 32),
                "refresh_token": "host-refresh",
            }
        }
    ).encode()
    renewed = jwt.encode({"exp": time.time() + 3600}, "x" * 32)

    async def refresh(raw, **kwargs):
        nonlocal refreshes
        refreshes += 1
        assert b"host-refresh" in raw
        await asyncio.sleep(0.01)
        return json.dumps(
            {
                "tokens": {
                    "account_id": "workspace",
                    "access_token": renewed,
                    "refresh_token": "renewed-refresh",
                }
            }
        ).encode()

    monkeypatch.setattr(codex_import, "refresh_with_codex", refresh)
    async with httpx.AsyncClient() as http:
        manager = OpenAIAuthManager(store, http)
        account = await manager.import_auth_json(original)
        results = await asyncio.gather(
            *(manager.runtime_credentials(account.id) for _ in range(10))
        )
        assert refreshes == 1 and all(r.access_token == renewed for r in results)
        saved = await store.get("openai-" + account.id)
        assert saved and b"renewed-refresh" in saved
        assert all("refresh_token" not in r.model_dump() for r in results)
        await manager.sign_out(account.id)
        assert await manager.accounts() == []


@pytest.mark.parametrize("token", ["redacted", "opaque", "not.a.jwt"])
async def test_malformed_import_leaves_existing_credentials_intact(store, token):
    good = jwt.encode({"exp": time.time() + 3600}, "x" * 32)
    async with httpx.AsyncClient() as http:
        manager = OpenAIAuthManager(store, http)
        existing = await manager.import_auth_json(
            json.dumps(
                {
                    "tokens": {
                        "access_token": good,
                        "account_id": "workspace",
                        "refresh_token": "refresh",
                    }
                }
            ).encode()
        )
        with pytest.raises(ValueError, match="codex logout, then codex login") as failure:
            await manager.import_auth_json(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": token,
                            "account_id": "workspace",
                            "refresh_token": "sensitive-refresh",
                        }
                    }
                ).encode()
            )
        assert "sensitive-refresh" not in str(failure.value)
        assert (await manager.runtime_credentials(existing.id)).access_token == good
        assert len(await manager.accounts()) == 1
