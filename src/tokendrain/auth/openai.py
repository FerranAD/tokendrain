"""Encrypted imported Codex credentials, refreshed through Codex when necessary."""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from tokendrain.credentials import CredentialStore


class AccountInfo(BaseModel):
    id: str
    method: Literal["import"]
    subject: str
    email: str | None = None
    client_id: str | None = None
    expires_at: float
    connected: bool = True


class RuntimeCredentials(BaseModel):
    mode: Literal["chatgpt"]
    access_token: str = Field(repr=False)
    expires_at: float
    account_id: str | None = None
    plan_type: str | None = None


class AccountRecord(AccountInfo):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(default="", repr=False)
    id_token: str = Field(default="", repr=False)
    scopes: list[str] = Field(default_factory=list)
    chatgpt_account_id: str | None = None
    plan_type: str | None = None
    imported_auth: dict[str, Any] | None = Field(default=None, repr=False)


class OpenAIAuthManager:
    def __init__(
        self, store: CredentialStore, http: httpx.AsyncClient, *, runtime_dir: Path | None = None
    ) -> None:
        self.store, self.http, self.runtime_dir = store, http, runtime_dir
        self._locks: dict[str, asyncio.Lock] = {}
        self._import_lock = asyncio.Lock()

    async def _load(self, account_id: str) -> AccountRecord:
        data = await self.store.get(f"openai-{account_id}")
        if data is None:
            raise ValueError("OpenAI account not found")
        return AccountRecord.model_validate_json(data)

    async def _save(self, record: AccountRecord) -> None:
        await self.store.put(f"openai-{record.id}", record.model_dump_json().encode())

    async def accounts(self) -> list[AccountInfo]:
        accounts = []
        for name in await self.store.names():
            if not name.startswith("openai-"):
                continue
            raw = await self.store.get(name)
            if raw is None:
                continue
            # Retired SIWC registrations cannot authorize this provider anymore.
            if json.loads(raw).get("method") != "import":
                continue
            accounts.append(AccountInfo(**AccountRecord.model_validate_json(raw).model_dump()))
        return accounts

    async def import_auth_json(self, raw: bytes) -> AccountInfo:
        from .codex_import import decode_import

        decoded = decode_import(raw)
        async with self._import_lock:
            account_id = str(uuid.uuid4())
            for existing in await self.accounts():
                if existing.method != "import":
                    continue
                record = await self._load(existing.id)
                if (
                    record.subject == decoded["subject"]
                    and record.chatgpt_account_id == decoded["chatgpt_account_id"]
                ):
                    account_id = existing.id
                    break
            account = AccountRecord(id=account_id, method="import", **decoded)
            async with self._locks.setdefault(account.id, asyncio.Lock()):
                await self._save(account)
            return AccountInfo(**account.model_dump())

    async def runtime_credentials(
        self, account_id: str | None = None, force_refresh: bool = False
    ) -> RuntimeCredentials:
        if account_id is None:
            accounts = [account for account in await self.accounts() if account.connected]
            if not accounts:
                raise ValueError("Import Codex auth.json first")
            account_id = accounts[0].id
        async with self._locks.setdefault(account_id, asyncio.Lock()):
            record = await self._load(account_id)
            if not record.connected:
                raise ValueError("Codex credentials disconnected")
            if force_refresh or record.expires_at <= time.time() + 180:
                from .codex_import import decode_import, refresh_with_codex

                raw = await refresh_with_codex(
                    json.dumps(record.imported_auth).encode(), runtime_dir=self.runtime_dir
                )
                record = AccountRecord(id=record.id, method="import", **decode_import(raw))
                await self._save(record)
            return RuntimeCredentials(
                mode="chatgpt",
                access_token=record.access_token,
                expires_at=record.expires_at,
                account_id=record.chatgpt_account_id,
                plan_type=record.plan_type,
            )

    async def sign_out(self, account_id: str) -> bool:
        async with self._locks.setdefault(account_id, asyncio.Lock()):
            await self.store.delete(f"openai-{account_id}")
        return True
