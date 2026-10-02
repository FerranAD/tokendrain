"""The only adapter aware of Codex's private auth.json format.

Codex itself performs refresh. We never implement its private OAuth refresh
endpoint/client identifiers, and only exported access tokens enter a guest.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from tokendrain.codex.client import CodexClient


def decode_import(raw: bytes) -> dict[str, Any]:
    from .openai import _unverified_claims

    if len(raw) > 1024 * 1024:
        raise ValueError("auth.json exceeds 1 MiB")
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        raise ValueError(
            "expected Codex ChatGPT auth.json with tokens; API keys are not a ChatGPT usage account"
        )
    if data.get("auth_mode") not in {None, "chatgpt"} or data.get("OPENAI_API_KEY"):
        raise ValueError("import requires a managed ChatGPT login, not another Codex auth mode")
    tokens = data["tokens"]
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ValueError("auth.json has no ChatGPT access token")
    claims = _unverified_claims(access_token)
    id_claims = _unverified_claims(tokens.get("id_token", ""))
    auth_claims = claims.get("https://api.openai.com/auth", {})
    account_id = tokens.get("account_id") or auth_claims.get("chatgpt_account_id")
    if not account_id:
        raise ValueError("auth.json has no selected ChatGPT account identifier")
    # Imported data is explicitly supplied by the administrator, not an identity proof.
    return {
        "subject": str(id_claims.get("sub", account_id)),
        "email": id_claims.get("email"),
        "access_token": access_token,
        "refresh_token": "",
        "id_token": "",
        "expires_at": float(claims.get("exp", time.time())),
        "chatgpt_account_id": str(account_id),
        "plan_type": auth_claims.get("chatgpt_plan_type"),
        "imported_auth": data,
    }


async def refresh_with_codex(
    raw: bytes, *, runtime_dir: Path | None = None, executable: str = "codex"
) -> bytes:
    """Ask the installed Codex to refresh a private host tmpfs copy atomically."""
    from .process import auth_runtime_directory, isolated_codex

    async with isolated_codex(
        runtime_dir or auth_runtime_directory(),
        executable=executable,
        imported_auth=raw,
    ) as (peer, home):
        await CodexClient(peer).initialize()
        await peer.request("account/read", {"refreshToken": True})
        return await asyncio.to_thread((home / "auth.json").read_bytes)
