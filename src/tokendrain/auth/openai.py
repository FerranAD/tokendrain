"""Official public-client SIWC flow and an isolated Codex auth.json adapter."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlparse

import httpx
import jwt
from pydantic import BaseModel, Field

from tokendrain.credentials import CredentialStore

AUTH_ORIGIN = "https://auth.openai.com"
AUTHORIZE_URL = AUTH_ORIGIN + "/api/accounts/authorize"
TOKEN_URL = AUTH_ORIGIN + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"


class AccountInfo(BaseModel):
    id: str
    method: Literal["siwc", "import"]
    subject: str
    email: str | None = None
    client_id: str | None = None
    expires_at: float
    connected: bool = True


class RuntimeCredentials(BaseModel):
    mode: Literal["siwc", "chatgpt"]
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


class AuthorizationStart(BaseModel):
    url: str
    state: str
    expires_at: float


class PendingAuthorization(BaseModel):
    verifier: str
    nonce: str
    redirect_uri: str
    client_id: str
    account_id: str | None
    expires_at: float


def _unverified_claims(token: str) -> dict[str, Any]:
    """Compatibility parsing only; never used to authenticate an OAuth identity."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return dict(claims)
    except jwt.PyJWTError:
        return {}


class OpenAIAuthManager:
    def __init__(
        self,
        store: CredentialStore,
        http: httpx.AsyncClient,
        host_id: str,
        *,
        runtime_dir: Path | None = None,
    ) -> None:
        self.store, self.http, self.host_id = store, http, host_id
        self.runtime_dir = runtime_dir
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending_lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()

    async def _load(self, account_id: str) -> AccountRecord:
        data = await self.store.get(f"openai-{account_id}")
        if data is None:
            raise ValueError("OpenAI account not found")
        return AccountRecord.model_validate_json(data)

    async def _save(self, record: AccountRecord) -> None:
        await self.store.put(f"openai-{record.id}", record.model_dump_json().encode())

    async def accounts(self) -> list[AccountInfo]:
        return [
            AccountInfo(**(await self._load(name[7:])).model_dump())
            for name in await self.store.names()
            if name.startswith("openai-")
        ]

    async def begin_sign_in(
        self, redirect_uri: str, account_id: str | None = None
    ) -> AuthorizationStart:
        uri = urlparse(redirect_uri)
        if (
            uri.scheme != "http"
            or uri.hostname != "127.0.0.1"
            or uri.path != "/auth/callback"
            or uri.query
            or uri.fragment
            or uri.username is not None
            or uri.password is not None
        ):
            raise ValueError(
                "SIWC requires http://127.0.0.1:<port>/auth/callback; "
                "use an SSH tunnel for a remote host"
            )
        record = await self._load(account_id) if account_id else None
        if record and record.method != "siwc":
            raise ValueError(
                "imported Codex accounts cannot be reauthorized as a SIWC registration"
            )
        state, verifier, nonce = (
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(64),
            secrets.token_urlsafe(32),
        )
        expires_at = time.time() + 600
        client_id = record.client_id if record else "dynamic_agent_client"
        assert client_id
        pending = PendingAuthorization(
            verifier=verifier,
            nonce=nonce,
            redirect_uri=redirect_uri,
            client_id=client_id,
            account_id=account_id,
            expires_at=expires_at,
        )
        await self.store.put(f"pending-{state}", pending.model_dump_json().encode())
        parameters = {
            "client_id": client_id,
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("="),
        }
        if record:
            if record.id_token:
                parameters["id_token_hint"] = record.id_token
            if record.email:
                parameters["login_hint"] = record.email
        else:
            parameters["agent_name_hint"] = "tokendrain"
        return AuthorizationStart(
            url=AUTHORIZE_URL + "?" + urlencode(parameters), state=state, expires_at=expires_at
        )

    async def _validate_identity(self, id_token: str, client_id: str, nonce: str) -> dict[str, Any]:
        discovery_response = await self.http.get(AUTH_ORIGIN + "/.well-known/openid-configuration")
        discovery_response.raise_for_status()
        discovery = discovery_response.json()
        if discovery.get("issuer") != AUTH_ORIGIN:
            raise ValueError("unexpected OpenAI OIDC issuer")
        jwks_uri = discovery["jwks_uri"]
        if not jwks_uri.startswith(AUTH_ORIGIN + "/"):
            raise ValueError("unexpected OpenAI signing key location")
        response = await self.http.get(jwks_uri)
        response.raise_for_status()
        header = jwt.get_unverified_header(id_token)
        jwks = jwt.PyJWKSet.from_dict(response.json())
        key = next((key for key in jwks.keys if key.key_id == header.get("kid")), None)
        if key is None:
            raise ValueError("ID token signing key unavailable")
        claims = jwt.decode(
            id_token,
            key.key,
            algorithms=["RS256", "ES256"],
            audience=client_id,
            issuer=AUTH_ORIGIN,
            options={"require": ["exp", "iat", "sub", "iss", "aud", "nonce"]},
        )
        if not secrets.compare_digest(str(claims["nonce"]), nonce):
            raise ValueError("ID token nonce mismatch")
        return dict(claims)

    async def complete_sign_in(
        self,
        state: str,
        code: str = "",
        client_id: str | None = None,
        error: str | None = None,
    ) -> AccountInfo:
        async with self._pending_lock:
            raw = await self.store.get(f"pending-{state}")
            if raw is None:
                raise ValueError("unknown or already consumed OAuth state")
            pending = PendingAuthorization.model_validate_json(raw)
            await self.store.delete(f"pending-{state}")
        if pending.expires_at < time.time():
            raise ValueError("OAuth attempt expired")
        if error:
            raise ValueError("OpenAI authorization was declined")
        if not code:
            raise ValueError("authorization code missing")
        if pending.client_id == "dynamic_agent_client":
            if not client_id or client_id == "dynamic_agent_client":
                raise ValueError("new registration did not return an issued client_id")
        elif client_id and client_id != pending.client_id:
            raise ValueError("reauthorization returned a different client_id")
        else:
            client_id = pending.client_id
        assert client_id
        response = await self.http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": pending.verifier,
                "redirect_uri": pending.redirect_uri,
                "resource": RESOURCE,
            },
        )
        if response.is_error:
            raise ValueError("OpenAI code exchange failed; begin a fresh sign-in")
        tokens = response.json()
        claims = await self._validate_identity(tokens["id_token"], client_id, pending.nonce)
        scopes = tokens.get("scope", "").split()
        if "chatgpt.tokens.use.direct" not in scopes:
            raise ValueError("ChatGPT plan use was not authorized")
        if pending.account_id:
            old = await self._load(pending.account_id)
            if claims["sub"] != old.subject:
                raise ValueError("reauthorization selected a different account")
        account = AccountRecord(
            id=pending.account_id or str(uuid.uuid4()),
            method="siwc",
            subject=claims["sub"],
            email=claims.get("email"),
            client_id=client_id,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token", ""),
            id_token=tokens["id_token"],
            scopes=scopes,
            expires_at=time.time() + float(tokens["expires_in"]),
        )
        async with self._locks.setdefault(account.id, asyncio.Lock()):
            await self._save(account)
        return AccountInfo(**account.model_dump())

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
                raise ValueError("connect an OpenAI account first")
            account_id = accounts[0].id
        async with self._locks.setdefault(account_id, asyncio.Lock()):
            record = await self._load(account_id)
            if not record.connected:
                raise ValueError("OpenAI account is signed out")
            if force_refresh or record.expires_at <= time.time() + 180:
                if record.method == "import":
                    from .codex_import import decode_import, refresh_with_codex

                    raw = await refresh_with_codex(
                        json.dumps(record.imported_auth).encode(), runtime_dir=self.runtime_dir
                    )
                    decoded = decode_import(raw)
                    record = AccountRecord(id=record.id, method="import", **decoded)
                else:
                    if not record.refresh_token:
                        raise ValueError("OpenAI account must sign in again")
                    response = await self.http.post(
                        TOKEN_URL,
                        data={
                            "grant_type": "refresh_token",
                            "client_id": record.client_id or "",
                            "refresh_token": record.refresh_token,
                            "resource": RESOURCE,
                        },
                    )
                    if response.is_error:
                        # Never log the response/request body; it may contain credentials.
                        raise ValueError("OpenAI refresh failed; reconnect the account")
                    tokens = response.json()
                    record.access_token = tokens["access_token"]
                    record.refresh_token = tokens.get("refresh_token", record.refresh_token)
                    record.id_token = tokens.get("id_token", record.id_token)
                    record.expires_at = time.time() + float(tokens["expires_in"])
                    record.scopes = tokens.get("scope", " ".join(record.scopes)).split()
                    if "chatgpt.tokens.use.direct" not in record.scopes:
                        raise ValueError("ChatGPT plan permission was revoked")
                await self._save(record)
            return RuntimeCredentials(
                mode="siwc" if record.method == "siwc" else "chatgpt",
                access_token=record.access_token,
                expires_at=record.expires_at,
                account_id=record.chatgpt_account_id,
                plan_type=record.plan_type,
            )

    async def sign_out(self, account_id: str) -> bool:
        async with self._locks.setdefault(account_id, asyncio.Lock()):
            record = await self._load(account_id)
            revoked = False
            if record.method == "siwc" and record.refresh_token:
                try:
                    discovery = await self.http.get(
                        AUTH_ORIGIN + "/.well-known/openid-configuration"
                    )
                    discovery.raise_for_status()
                    endpoint = discovery.json()["revocation_endpoint"]
                    if not endpoint.startswith(AUTH_ORIGIN + "/"):
                        raise ValueError("unexpected revocation endpoint")
                    response = await self.http.post(
                        endpoint,
                        data={
                            "token": record.refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": record.client_id or "",
                        },
                    )
                    revoked = response.is_success
                except (httpx.HTTPError, KeyError):
                    pass
            record.access_token = record.refresh_token = record.id_token = ""
            record.imported_auth = None
            record.connected = False
            await self._save(record)
            return revoked
