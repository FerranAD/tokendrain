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

HOST_ID = "urn:uuid:12345678-1234-4234-9234-123456789abc"


@pytest.mark.parametrize(
    "failure_method",
    [None, "initialize", "account/login/start", "model/list", "account/rateLimits/read"],
)
async def test_import_probe_never_starts_thread_and_cleans_isolated_home(
    tmp_path: Path, failure_method: str | None, caplog: pytest.LogCaptureFixture
) -> None:
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
    if method == {failure_method!r}:
        message = ("invalid ID token format runtime-token"
                   if method == "account/login/start" else "rejected runtime-token")
        error = {{"code":-32602,"message":message}}
        print(json.dumps({{"id":request["id"],"error":error}}),flush=True)
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
            OpenAIAuthManager(store, http), http, tmp_path / "runtime", str(executable)
        ).read()
    if failure_method:
        assert result.usage == {}
        if failure_method == "account/login/start":
            assert result.reauth_required
            assert "codex logout, then codex login" in (result.usage_error or "")
        else:
            assert not result.reauth_required
            assert failure_method in (result.usage_error or "")
            assert "-32602" in (result.usage_error or "")
        assert "runtime-token" not in caplog.text
        assert "[REDACTED]" in caplog.text
    else:
        assert result.models[0].id == "model"
        assert result.usage["rateLimits"]["primary"]["usedPercent"] == 12
        assert result.usage_error is None
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
