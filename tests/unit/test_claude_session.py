import asyncio
from typing import Any

import pytest

from tokendrain.codex.rpc import RpcNotification
from tokendrain.orchestration.claude import ClaudeSession
from tokendrain.orchestration.driver import ProviderLimited


class Auth:
    async def access(self):
        return "secret-access", "account"

    async def record(self):
        return {"id": "account"}


class Guest:
    def __init__(self, outcome: str):
        self.outcome = outcome
        self.closed = asyncio.Event()
        self.notifications = asyncio.Queue()
        self.credentials: dict[str, Any] = {}
        self.interrupted = False

    async def credentials_set(self, value):
        self.credentials = value

    async def request(self, method, params=None):
        if method == "claude_initialize":
            return {"session_id": "session"}
        if method == "claude_interrupt":
            self.interrupted = True
        if method == "claude_turn":
            await self.notifications.put(
                RpcNotification(
                    method="claude/event",
                    params={
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "Using secret-access"}]},
                    },
                )
            )
            await self.notifications.put(
                RpcNotification(
                    method="claude/event",
                    params={
                        "type": "rate_limit_event",
                        "rate_limit_info": {
                            "rate_limit_type": "five_hour",
                            "utilization": 0.42,
                            "resets_at": 1791338400,
                        },
                    },
                )
            )
            if self.outcome == "disconnect":
                self.closed.set()
            elif self.outcome != "hang":
                result = {"structured_output": {"summary": "Done secret-access"}}
                if self.outcome == "invalid":
                    result = {"structured_output": "invalid"}
                if self.outcome == "limited":
                    result = {"is_error": True, "provider_limited": True}
                await self.notifications.put(RpcNotification(method="claude/result", params=result))
        return {}


async def github():
    return None


@pytest.mark.parametrize("outcome", ["completed", "invalid", "limited", "disconnect", "hang"])
async def test_stream_checkpoint_validation_redaction_and_terminal_paths(outcome: str) -> None:
    guest = Guest(outcome)
    logs, observations = [], []

    async def log(value):
        logs.append(value)

    async def observe(value):
        observations.extend(value)

    session = ClaudeSession(Auth(), guest, {}, github, log, observe, 0.05)
    assert await session.initialize(None, "sonnet") == "session"
    if outcome == "completed":
        report = await session.turn("Work", "sonnet", "medium", asyncio.Event())
        assert "secret-access" not in report.summary
        assert observations[0].used_percent == 42
        assert observations[0].metadata["account_id"] == "account"
    else:
        expected = {
            "invalid": ValueError,
            "limited": ProviderLimited,
            "disconnect": ConnectionError,
            "hang": TimeoutError,
        }[outcome]
        with pytest.raises(expected):
            await session.turn("Work", "sonnet", "medium", asyncio.Event())
    assert all("secret-access" not in value for value in logs)
    assert guest.credentials["claude"] == {"access_token": "secret-access"}
    if outcome == "hang":
        assert guest.interrupted
