import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tokendrain.api.app import create_app
from tokendrain.application import Application, Overrides
from tokendrain.config import Settings
from tokendrain.domain import UsageWindow, utcnow
from tokendrain.notifications import TOKEN, NtfyInput, NtfyService


async def test_idle_worker_refreshes_usage_without_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[httpx.Request] = []
    probes = 0

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"id": "message"})

    async def probe(app: Application) -> dict[str, object]:
        nonlocal probes
        probes += 1
        await app.runs.observe(
            [
                UsageWindow(
                    limit_id="codex",
                    used_percent=10,
                    window_minutes=10080,
                    resets_at=utcnow() + timedelta(hours=6),
                )
            ]
        )
        return {}

    monkeypatch.setattr("tokendrain.account_metadata.account_probe", probe)
    http = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    app = await Application.open(
        Settings(state_dir=tmp_path, backend="mock", auth_mode="none"),
        Overrides(http=http, start_workers=False),
    )
    try:
        await app.notifications.configure(
            NtfyInput(enabled=True, topic="idle-alerts", rules=[{"id": "weekly"}])
        )
        await app.notifications.tick()
        assert probes == 1 and len(sent) == 1
        await app.notifications.tick()
        assert probes == 1 and len(sent) == 1
    finally:
        await app.close()


async def test_reminders_delivery_retry_dedup_restart_and_new_reset(tmp_path: Path) -> None:
    sent: list[httpx.Request] = []
    fail = True

    def receive(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(503 if fail else 200, json={"id": "notification"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    app = await Application.open(
        Settings(state_dir=tmp_path, backend="mock", auth_mode="none"),
        Overrides(http=http, start_workers=False),
    )
    try:
        now = utcnow()
        window = UsageWindow(
            limit_id="codex",
            used_percent=20,
            window_minutes=10080,
            resets_at=now + timedelta(hours=12),
            observed_at=now,
        )

        async def read() -> list[UsageWindow]:
            return [window]

        service = NtfyService(app.sessions, app.credentials, http, "https://drain.example", read)
        config = NtfyInput(
            enabled=True,
            server_url="https://ntfy.example/prefix",
            topic="private-topic",
            access_token="fake-test-token",
            rules=[{"id": "weekly"}],
        )
        await service.configure(config)
        await service.tick(now)
        assert len(sent) == 1
        assert (await service.status())["delivery"]["last_error"]
        assert sent[0].url == "https://ntfy.example/prefix/"
        assert sent[0].headers["Authorization"] == "Bearer fake-test-token"
        message = json.loads(sent[0].content)
        assert "12.0h" in message["message"] and "80%" in message["message"]
        assert message["click"] == "https://drain.example"

        fail = False
        await service.tick(now)
        await service.tick(now)
        assert len(sent) == 2  # Retry succeeds, then no repeated reminder.
        replacement = NtfyService(
            app.sessions, app.credentials, http, "https://drain.example", read
        )
        await replacement.tick(now)
        assert len(sent) == 2  # Durable history survives worker replacement.
        window.resets_at = now + timedelta(hours=6)
        await replacement.tick(now)
        assert len(sent) == 3
        assert (await replacement.status())["delivery"]["last_error"] is None

        await replacement.configure(config.model_copy(update={"enabled": False}))
        window.resets_at = now + timedelta(hours=1)
        await replacement.tick(now)
        assert len(sent) == 3
        # Manual test uses saved settings even when automatic reminders are off.
        await replacement.test()
        assert len(sent) == 4 and json.loads(sent[-1].content)["title"] == "Tokendrain test"
    finally:
        await app.close()


async def test_api_configuration_secrets_and_safe_delivery_errors(tmp_path: Path) -> None:
    token = "fake-test-ntfy-secret"

    def receive(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"private response: {token}")

    settings = Settings(
        state_dir=tmp_path, backend="mock", auth_mode="none", public_url="http://testserver"
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    web = create_app(settings, Overrides(http=http, start_workers=False))
    async with (
        LifespanManager(web),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web),
            base_url="http://testserver",
            headers={"X-Tokendrain-Request": "1"},
        ) as client,
    ):
        path = "/api/v1/notifications/ntfy"
        assert (await client.get(path)).json()["enabled"] is False
        body = {
            "enabled": True,
            "server_url": "https://ntfy.example",
            "topic": "my-topic",
            "access_token": token,
            "rules": [{"id": "weekly"}],
        }
        response = await client.put(path, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["has_token"] and token not in response.text
        services = web.state.services
        assert await services.credentials.get(TOKEN) == token.encode()
        assert token.encode() not in (tmp_path / "credentials" / TOKEN).read_bytes()
        assert token not in json.dumps(await services.notifications._load("ntfy"))
        del body["access_token"]
        assert (await client.put(path, json=body)).json()["has_token"]
        response = await client.post(path + "/test")
        assert response.status_code == 409
        assert token not in response.text and "private response" not in response.text
        response = await client.put(path, json={**body, "clear_token": True})
        assert not response.json()["has_token"]
        for patch in (
            {"server_url": "https://user:secret@example.com"},
            {"server_url": "file:///tmp/not-ntfy"},
            {"server_url": "https://ntfy.example?token=secret"},
            {"topic": "../../admin"},
            {"rules": [{"hours_before_reset": -1}]},
            {"rules": [{"min_remaining_percent": 101}]},
            {"rules": [{"id": "same"}, {"id": "same"}]},
            {"access_token": "bad\r\nheader"},
        ):
            assert (await client.put(path, json={**body, **patch})).status_code == 422
        assert (
            await client.put(path, json=body, headers={"X-Tokendrain-Request": "0"})
        ).status_code == 403


@pytest.mark.parametrize(
    "patch",
    [
        {"used_percent": 21},
        {"window_minutes": 300},
        {"limit_id": "other"},
        {"resets_at": None},
        {"resets_at": utcnow() - timedelta(hours=1)},
        {"resets_at": utcnow() + timedelta(hours=13)},
        {"observed_at": utcnow() - timedelta(minutes=6)},
        {"observed_at": utcnow() + timedelta(minutes=6)},
    ],
)
def test_nonmatching_usage_is_never_notified(patch: dict[str, object]) -> None:
    from tokendrain.notifications import UsageAlert

    now = utcnow()
    window = UsageWindow.model_validate(
        {
            "limit_id": "codex",
            "window_minutes": 10080,
            "used_percent": 20,
            "resets_at": now + timedelta(hours=11),
            "observed_at": now,
            **patch,
        }
    )
    assert not UsageAlert(limit_id="codex").matches(window, now)
