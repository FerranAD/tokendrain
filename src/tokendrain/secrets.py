"""Project secret metadata lives in SQLite; values are never returned from this service."""

import io
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from dotenv.parser import parse_stream
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.credentials.store import CredentialStore
from tokendrain.db.models import Project, SecretEntry
from tokendrain.domain import utcnow

PROTECTED = {
    "HOME",
    "PATH",
    "CODEX_HOME",
    "ACCESS_TOKEN",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_ASKPASS",
    "GH_TOKEN",
    "GITHUB_TOKEN",
}


def secret_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,199}", value) or value in PROTECTED:
        raise ValueError("Secret name is invalid or reserved for runtime credentials")
    return value


def parse_dotenv(text: str, descriptions: dict[str, str]) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for binding in parse_stream(io.StringIO(text)):
        if binding.error:
            raise ValueError(f"Malformed .env entry at line {binding.original.line}")
        if binding.key is None:
            continue
        key = secret_name(binding.key)
        if key in result:
            raise ValueError(f"Duplicate secret name: {key}")
        if binding.value is None:
            raise ValueError(f"Secret {key} requires an explicit value")
        description = descriptions.get(key, "").strip()
        if not description:
            raise ValueError(f"Secret {key} requires a purpose/description")
        result[key] = (binding.value, description)
    if not result:
        raise ValueError("No secrets found")
    return result


class SecretService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], store: CredentialStore) -> None:
        self.sessions, self.store = sessions, store

    async def list_entries(self, project_id: str) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            if not await db.get(Project, project_id):
                raise LookupError("Project not found")
            rows = (
                await db.scalars(
                    select(SecretEntry)
                    .where(SecretEntry.project_id == project_id)
                    .order_by(SecretEntry.name)
                )
            ).all()
            return [
                {"name": row.name, "description": row.description, "updated_at": row.updated_at}
                for row in rows
            ]

    async def put_many(self, project_id: str, values: Mapping[str, tuple[str | None, str]]) -> None:
        new_refs: list[str] = []
        old_refs: list[str] = []
        staged: dict[str, str] = {}
        try:
            for name, (value, description) in values.items():
                secret_name(name)
                if not description.strip():
                    raise ValueError("A purpose/description is required for every secret")
                if value is not None:
                    ref = f"secret-{uuid4().hex}"
                    await self.store.put(ref, value.encode())
                    new_refs.append(ref)
                    staged[name] = ref
            async with self.sessions.begin() as db:
                if not await db.get(Project, project_id):
                    raise LookupError("Project not found")
                for name, (_, description) in values.items():
                    row = await db.get(SecretEntry, (project_id, name))
                    if row:
                        if name in staged:
                            old_refs.append(row.credential_ref)
                            row.credential_ref = staged[name]
                        row.description, row.updated_at = description, utcnow()
                    else:
                        if name not in staged:
                            raise ValueError(f"New secret {name} requires a value")
                        db.add(
                            SecretEntry(
                                project_id=project_id,
                                name=name,
                                description=description,
                                credential_ref=staged[name],
                            )
                        )
        except BaseException:
            for ref in new_refs:
                await self.store.delete(ref)
            raise
        for ref in old_refs:
            await self.store.delete(ref)

    async def delete(self, project_id: str, name: str) -> None:
        async with self.sessions.begin() as db:
            row = await db.get(SecretEntry, (project_id, name))
            if not row:
                raise LookupError("Secret not found")
            ref = row.credential_ref
            await db.delete(row)
        await self.store.delete(ref)
