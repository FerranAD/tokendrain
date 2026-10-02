from __future__ import annotations

import secrets
import shutil
import sys
import time
from pathlib import Path

import httpx
import pytest

from tokendrain.auth.openai import AccountRecord, OpenAIAuthManager
from tokendrain.auth.probe import AuthProbe
from tokendrain.auth.process import isolated_codex
from tokendrain.codex.client import CodexClient
from tokendrain.credentials import EncryptedFileCredentialStore


async def test_siwc_probe_uses_account_catalog_and_explicit_unavailable_usage(
    tmp_path: Path,
) -> None:
    store = EncryptedFileCredentialStore(tmp_path / "credentials", secrets.token_bytes(32))
    record = AccountRecord(
        id="account",
        method="siwc",
        subject="user",
        expires_at=time.time() + 3600,
        access_token="only-runtime-access",
        scopes=["chatgpt.tokens.use.direct"],
    )
    await store.put("openai-account", record.model_dump_json().encode())

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["authorization"] == "Bearer only-runtime-access"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "slug": "visible-model",
                        "display_name": "Visible",
                        "visibility": "list",
                        "supported_reasoning_levels": [{"effort": "medium"}],
                        "default_reasoning_level": "medium",
                    },
                    {"slug": "hidden-model", "display_name": "Hidden", "visibility": "hide"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        probe = AuthProbe(OpenAIAuthManager(store, http, "host"), http, tmp_path / "runtime")
        result = await probe.read()
        assert [model.id for model in result.models] == ["visible-model"]
        assert result.models[0].supported_reasoning_efforts == ["medium"]
        assert result.usage == {} and result.usage_error
        assert not (tmp_path / "runtime").exists()
        assert "only-runtime-access" not in result.model_dump_json()


async def test_import_probe_never_starts_thread_and_cleans_isolated_home(tmp_path: Path) -> None:
    # The fake subprocess rejects any unexpected API, particularly inference.
    executable = tmp_path / "fake-codex"
    audit = tmp_path / "audit"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + f"""
import json, os, sys
from pathlib import Path
audit = Path({str(audit)!r})
assert not (Path(os.environ["CODEX_HOME"]) / "auth.json").exists()
assert "OPENAI_API_KEY" not in os.environ
assert "ACCESS_TOKEN" not in os.environ
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    with audit.open("a") as stream:
        stream.write(method+"\\n")
    if "id" not in request:
        continue
    result = {{}}
    if method == "account/login/start":
        assert request["params"]["accessToken"] == "runtime-token"
        assert "refreshToken" not in request["params"]
    elif method == "model/list":
        result = {{"data":[{{"id":"model", "displayName":"Model",
                   "supportedReasoningEfforts":[{{"reasoningEffort":"low"}}]}}]}}
    elif method == "account/rateLimits/read":
        result = {{"rateLimits":{{"limitId":"codex", "primary":{{"usedPercent":12,
                   "windowDurationMins":300,"resetsAt":2000000000}}}}}}
    elif method != "initialize":
        raise AssertionError("unexpected method "+method)
    print(json.dumps({{"id":request["id"],"result":result}}),flush=True)
"""
    )
    executable.chmod(0o700)
    store = EncryptedFileCredentialStore(tmp_path / "credentials", secrets.token_bytes(32))
    record = AccountRecord(
        id="account",
        method="import",
        subject="user",
        expires_at=time.time() + 3600,
        access_token="runtime-token",
        chatgpt_account_id="workspace",
        imported_auth={"tokens": {"refresh_token": "host-only-refresh"}},
    )
    await store.put("openai-account", record.model_dump_json().encode())
    async with httpx.AsyncClient() as http:
        result = await AuthProbe(
            OpenAIAuthManager(store, http, "host"), http, tmp_path / "runtime", str(executable)
        ).read()
    assert result.models[0].id == "model"
    assert result.usage["rateLimits"]["primary"]["usedPercent"] == 12
    assert not list((tmp_path / "runtime").iterdir())
    assert "thread/start" not in audit.read_text() and "turn/start" not in audit.read_text()


@pytest.mark.skipif(shutil.which("codex") is None, reason="installed Codex unavailable")
async def test_installed_codex_protocol_without_credentials(tmp_path: Path) -> None:
    """No account data or inference: validates exact protocol against local Codex."""
    async with isolated_codex(tmp_path / "runtime") as (peer, home):
        client = CodexClient(peer)
        await client.initialize()
        account = await peer.request("account/read", {"refreshToken": False})
        assert account["account"] is None
        models = await client.models()
        assert models
        # Thread creation has no inference. This catches documented enum drift:
        # thread sandbox is kebab-case; turn sandboxPolicy type is camelCase.
        thread_id = await client.start_thread(cwd=str(home))
        assert thread_id
        assert not (home / "auth.json").exists()
    assert not list((tmp_path / "runtime").iterdir())
