import asyncio
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from tokendrain.agents import reset_account, switch_agent
from tokendrain.application import Application, Overrides
from tokendrain.auth.claude import ClaudeAuthManager, decode_credentials, normalize_usage
from tokendrain.automations import AutomationInput
from tokendrain.config import Settings
from tokendrain.credentials.store import EncryptedFileCredentialStore
from tokendrain.db.models import Project
from tokendrain.domain import ExecutionState, ProjectCreate, RunTemplate, utcnow
from tokendrain.notifications import NtfyInput
from tokendrain.secrets import secret_name
from tokendrain_guestd.daemon import GuestDaemon


def oauth(expired: bool = False) -> dict:
    return {
        "accessToken": "private-access",
        "refreshToken": "private-refresh",
        "expiresAt": (time.time() + (-100 if expired else 3600)) * 1000,
        "scopes": ["user:inference", "user:profile"],
    }


async def test_refresh_serialization_cache_and_rate_limit_backoff(tmp_path: Path) -> None:
    refreshes, reads = 0, 0
    limited = False

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes, reads
        if request.method == "POST":
            refreshes += 1
            assert json.loads(request.content)["refresh_token"] == "private-refresh"
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 3600,
                },
            )
        reads += 1
        assert request.headers["authorization"] == "Bearer rotated-access"
        if limited:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(
            200,
            json={
                "five_hour": {"utilization": 23, "resets_at": "2026-10-07T00:00:00Z"},
                "seven_day": None,
            },
        )

    store = EncryptedFileCredentialStore(tmp_path / "credentials", b"x" * 32)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        auth = ClaudeAuthManager(store, http, tmp_path / "runtime")
        await auth.save({"id": "account", "oauth": oauth(True)})
        await asyncio.gather(*(auth.windows() for _ in range(10)))
        assert refreshes == 1 and reads == 1
        first, second = (await auth.windows())[0], (await auth.windows())[0]
        assert first.used_percent == 23 and first.window_minutes == 300
        assert first.observed_at == second.observed_at
        assert first.metadata["account_id"] == "account"
        assert b"rotated-refresh" not in (tmp_path / "credentials" / "claude-account").read_bytes()
        assert (await auth.record())["oauth"]["refreshToken"] == "rotated-refresh"
        limited = True
        with pytest.raises(ValueError, match="unavailable"):
            await auth.windows(force=True)
        with pytest.raises(ValueError, match="backoff"):
            await auth.windows(force=True)
        assert reads == 2 and auth.next_usage_attempt > time.time() + 890


async def test_browser_login_and_cancellation_remove_temporary_credentials(tmp_path: Path) -> None:
    script = tmp_path / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys, time
from pathlib import Path
print("https://claude.com/cai/oauth/authorize?state=test", flush=True)
code = sys.stdin.readline().strip()
if code != "login-code": sys.exit(1)
config = Path(os.environ["CLAUDE_CONFIG_DIR"])
(config / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
    "accessToken": "access-secret", "refreshToken": "refresh-secret",
    "expiresAt": (time.time() + 3600) * 1000, "scopes": ["user:inference", "user:profile"]}}))
(config / ".claude.json").write_text(json.dumps({"oauthAccount": {
    "accountUuid": "account-uuid", "emailAddress": "test@example.com"}}))
"""
    )
    script.chmod(0o700)
    store = EncryptedFileCredentialStore(tmp_path / "credentials", b"x" * 32)
    async with httpx.AsyncClient() as http:
        auth = ClaudeAuthManager(store, http, tmp_path / "runtime", executable=str(script))
        attempt = await auth.start_login()
        async with asyncio.timeout(5):
            while auth.login_status(attempt["id"])["status"] == "starting":  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        assert auth.login_status(attempt["id"])["authorization_url"].endswith("state=test")
        await auth.code(attempt["id"], "login-code")
        await auth.login_task
        assert auth.login_status(attempt["id"])["status"] == "connected"
        assert (await auth.status())["account_label"] == "test@example.com"
        assert "secret" not in json.dumps(await auth.status())
        assert not list((tmp_path / "runtime").iterdir())
        auth.login_timeout = 0.01
        await auth.start_login()
        await auth.login_task
        assert auth.login["status"] == "expired"
        assert not list((tmp_path / "runtime").iterdir())
        auth.login_timeout = 600
        await auth.start_login()
        await asyncio.sleep(0.1)
        await auth.cancel_login()
        assert auth.login["status"] == "cancelled" and auth.master is None
        assert not list((tmp_path / "runtime").iterdir())


async def test_global_switch_preserves_defaults_sessions_and_saved_run_origin(
    tmp_path: Path,
) -> None:
    app = await Application.open(
        Settings(state_dir=tmp_path, backend="mock", auth_mode="none"),
        Overrides(start_workers=False),
    )
    try:
        project_id = await app.projects.create(
            ProjectCreate(name="Both agents", default_model="gpt-5")
        )
        async with app.sessions.begin() as db:
            (await db.get(Project, project_id)).thread_id = "codex-session"
        await switch_agent(app, "claude_code")
        async with app.sessions.begin() as db:
            project = await db.get(Project, project_id)
            assert project.thread_id is None and project.default_model == ""
            project.default_model, project.thread_id = "sonnet", "claude-session"
        template = RunTemplate.model_validate(
            {
                "configured_agent": "codex",
                "projects": [
                    {
                        "project_id": project_id,
                        "model": "old-codex-model",
                        "reasoning_effort": "xhigh",
                    }
                ],
            }
        )
        run = await app.runs.create(template)
        assert run["agent"] == "claude_code"
        execution = run["executions"][0]
        assert execution["agent"] == "claude_code" and execution["model"] == "sonnet"
        assert execution["reasoning_effort"] == "medium"
        with pytest.raises(ValueError, match="active"):
            await switch_agent(app, "codex")
        await app.runs.transition(execution["id"], ExecutionState.CANCELLED)
        await switch_agent(app, "codex")
        async with app.sessions() as db:
            project = await db.get(Project, project_id)
            assert project.default_model == "gpt-5" and project.thread_id == "codex-session"
        await switch_agent(app, "claude_code")
        async with app.sessions() as db:
            project = await db.get(Project, project_id)
            assert project.default_model == "sonnet" and project.thread_id == "claude-session"
    finally:
        await app.close()


async def test_guest_claude_stream_resume_and_host_only_refresh_token(tmp_path: Path) -> None:
    script = tmp_path / "fake-claude"
    script.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, sys
from pathlib import Path
assert "ANTHROPIC_API_KEY" not in os.environ
assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == "access-secret"
assert "refresh-secret" not in str(os.environ)
assert sys.stdin.read() == "Work"
with Path("argv").open("a") as file: file.write(json.dumps(sys.argv) + "\\n")
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "text", "text": "Working"}]}}), flush=True)
print(json.dumps({"type": "result", "is_error": False,
    "structured_output": {"summary": "Done"}}), flush=True)
"""
    )
    script.chmod(0o700)
    guest = GuestDaemon(
        tmp_path / "runtime",
        tmp_path / "codex",
        tmp_path / "workspace",
        claude_executable=str(script),
    )

    class Host:
        closed = asyncio.Event()
        notifications = []

        async def notify(self, method, params):
            self.notifications.append((method, params))

    host = Host()
    guest.host = host
    await guest.handle("credentials_set", {"claude": {"access_token": "access-secret"}})
    initialized = await guest.handle("claude_initialize", {"session_id": None})
    for _ in range(2):
        await guest.handle(
            "claude_turn",
            {"prompt": "Work", "model": "sonnet", "effort": "medium", "schema": {"type": "object"}},
        )
        await guest._claude_task
    args = [json.loads(line) for line in (tmp_path / "workspace" / "argv").read_text().splitlines()]
    assert "--session-id" in args[0] and "--resume" in args[1]
    assert initialized["session_id"] in args[1]
    assert [params for method, params in host.notifications if method == "claude/result"][-1][
        "is_error"
    ] is False
    assert not list((tmp_path / "claude").glob("*credentials*"))


@pytest.mark.parametrize(
    "name", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CONFIG_DIR"]
)
def test_project_secrets_cannot_change_claude_authentication(name: str) -> None:
    with pytest.raises(ValueError, match="reserved"):
        secret_name(name)


def test_subscription_validation_and_missing_usage_are_not_zero() -> None:
    with pytest.raises(ValueError, match="subscription"):
        decode_credentials({"claudeAiOauth": {**oauth(), "scopes": []}})
    assert normalize_usage({"five_hour": None}, "account") == []


async def test_approval_defaults_follow_agent_and_deduplication_distinguishes_accounts(
    tmp_path: Path,
) -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    app = await Application.open(
        Settings(state_dir=tmp_path, backend="mock", auth_mode="none"),
        Overrides(start_workers=False, http=http),
    )
    try:
        project_id = await app.projects.create(
            ProjectCreate(name="Shared project", default_model="gpt-5")
        )
        await app.notifications.configure(NtfyInput(topic="approval"))
        await app.automations.save(
            AutomationInput.model_validate(
                {
                    "name": "Drain",
                    "mode": "approval",
                    "run_template": {
                        "projects": [
                            {
                                "project_id": project_id,
                                "model": "gpt-5",
                                "reasoning_effort": "xhigh",
                            }
                        ]
                    },
                }
            )
        )
        await app.claude.save({"id": "one", "oauth": oauth()})
        await switch_agent(app, "claude_code")
        async with app.sessions.begin() as db:
            (await db.get(Project, project_id)).default_model = "sonnet"
        account = "one"
        reset = (utcnow() + timedelta(hours=6)).isoformat()

        async def usage():
            return normalize_usage({"seven_day": {"utilization": 10, "resets_at": reset}}, account)

        app.automations.read_usage = usage
        app.automations.refresh_usage = usage
        await app.automations.tick()
        occurrence = (await app.automations.occurrences())[0]
        assert occurrence["run_template"]["configured_agent"] == "claude_code"
        assert occurrence["run_template"]["projects"][0]["model"] == "sonnet"
        assert occurrence["run_template"]["projects"][0]["reasoning_effort"] == "medium"
        account = "two"
        await reset_account(app, account)
        await app.automations.tick()
        occurrences = await app.automations.occurrences()
        assert {row["status"] for row in occurrences} == {"pending", "cancelled"}
        assert len(occurrences) == 2
    finally:
        await app.close()
