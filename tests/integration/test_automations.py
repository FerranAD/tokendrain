import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from tokendrain.api.app import create_app
from tokendrain.application import Application, Overrides
from tokendrain.automations import AutomationInput, AutomationService
from tokendrain.config import Settings
from tokendrain.db.models import AutomationOccurrence, Project
from tokendrain.domain import ExecutionState, ProjectCreate, RunTemplate, UsageWindow, utcnow
from tokendrain.notifications import NtfyInput


@dataclass
class Harness:
    app: Application
    windows: list[UsageWindow]
    project_id: str
    sent: list[httpx.Request] = field(default_factory=list)
    fail_delivery: bool = False
    refreshes: int = 0

    async def usage(self) -> list[UsageWindow]:
        return self.windows

    async def refresh(self) -> list[UsageWindow]:
        self.refreshes += 1
        return self.windows

    def replacement(self) -> AutomationService:
        return AutomationService(
            self.app.sessions,
            self.app.runs,
            self.app.events,
            self.app.notifications,
            self.usage,
            self.refresh,
        )

    async def rule(self, **values: Any) -> dict[str, Any]:
        return await self.app.automations.save(
            AutomationInput.model_validate(
                {
                    "name": "Drain weekly",
                    "mode": "automatic",
                    "trigger": {"min_remaining_percent": 20},
                    "run_template": {
                        "projects": [{"project_id": self.project_id}],
                        "stop_conditions": [
                            {"kind": "usage", "window_minutes": 10080, "used_percent": 100},
                            {"kind": "provider_limit"},
                            {"kind": "project_completed"},
                        ],
                    },
                    **values,
                }
            )
        )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    def receive(request: httpx.Request) -> httpx.Response:
        h.sent.append(request)
        return httpx.Response(503 if h.fail_delivery else 200, json={"id": "notification"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    app = await Application.open(
        Settings(state_dir=tmp_path, backend="mock", auth_mode="none"),
        Overrides(http=http, start_workers=False),
    )
    project_id = await app.projects.create(ProjectCreate(name="Approved project"))
    h = Harness(
        app,
        [
            UsageWindow(
                limit_id="codex",
                window_minutes=10080,
                used_percent=20,
                resets_at=utcnow() + timedelta(hours=6),
            )
        ],
        project_id,
    )
    app.automations.read_usage, app.automations.refresh_usage = h.usage, h.refresh
    await app.notifications.configure(NtfyInput(topic="approval-topic"))
    try:
        yield h
    finally:
        await app.close()


async def test_automatic_launch_deduplicates_concurrent_ticks_and_restart(harness: Harness) -> None:
    await harness.rule()
    await asyncio.gather(harness.app.automations.tick(), harness.replacement().tick())
    await harness.replacement().tick()
    runs = await harness.app.runs.list_runs()
    assert len(runs) == 1 and not harness.sent
    occurrences = await harness.app.automations.occurrences()
    assert occurrences[0]["status"] == "launched"
    assert runs[0]["automation_occurrence_id"] == occurrences[0]["id"]
    assert runs[0]["automation_name"] == "Drain weekly"
    assert runs[0]["stop_conditions"][0]["used_percent"] == 100
    assert runs[0]["stop_conditions"][0]["limit_id"] == "codex"
    assert runs[0]["stop_conditions"][-1] == {
        "kind": "deadline",
        "at": harness.windows[0].resets_at.isoformat().replace("+00:00", "Z"),
    }
    await harness.app.runs.transition(runs[0]["executions"][0]["id"], ExecutionState.CANCELLED)
    await harness.app.automations.tick()
    assert len(await harness.app.runs.list_runs()) == 1
    harness.windows[0].resets_at += timedelta(hours=1)
    await harness.app.automations.tick()
    assert len(await harness.app.runs.list_runs()) == 2


async def test_approval_delivery_retry_then_explicit_concurrent_authorization(
    harness: Harness,
) -> None:
    await harness.rule(mode="approval")
    harness.fail_delivery = True
    await harness.app.automations.tick()
    pending = (await harness.app.automations.occurrences())[0]
    assert pending["status"] == "pending" and pending["delivery_error"]
    assert not await harness.app.runs.list_runs()
    harness.fail_delivery = False
    await harness.replacement().tick()
    await harness.replacement().tick()
    assert len(harness.sent) == 2
    message = json.loads(harness.sent[-1].content)
    assert message["click"].endswith("/automation-occurrences/" + pending["id"])
    assert "80%" in message["message"]
    results = await asyncio.gather(
        harness.app.automations.authorize(pending["id"]),
        harness.replacement().authorize(pending["id"]),
    )
    assert results[0]["run_id"] == results[1]["run_id"]
    assert len(await harness.app.runs.list_runs()) == 1
    assert (await harness.app.automations.authorize(pending["id"]))["run_id"] == results[0][
        "run_id"
    ]


async def test_approval_rechecks_usage_and_conflicts_require_another_click(
    harness: Harness,
) -> None:
    await harness.rule(mode="approval")
    await harness.app.automations.tick()
    occurrence = (await harness.app.automations.occurrences())[0]
    harness.windows[0].used_percent = 99
    with pytest.raises(ValueError, match="still match"):
        await harness.app.automations.authorize(occurrence["id"])
    assert harness.refreshes == 1
    harness.windows[0].used_percent = 20
    occupied = await harness.app.runs.create(
        RunTemplate.model_validate(
            {
                "projects": [{"project_id": harness.project_id}],
            }
        )
    )
    with pytest.raises(ValueError, match="active or queued"):
        await harness.app.automations.authorize(occurrence["id"])
    await harness.app.runs.transition(occupied["executions"][0]["id"], ExecutionState.CANCELLED)
    await harness.app.automations.tick()
    assert (await harness.app.automations.occurrence(occurrence["id"]))["status"] == "pending"
    assert len(await harness.app.runs.list_runs()) == 1
    await harness.app.automations.authorize(occurrence["id"])
    assert len(await harness.app.runs.list_runs()) == 2


async def test_automatic_overlap_retries_without_duplicate_occurrences(harness: Harness) -> None:
    occupied = await harness.app.runs.create(
        RunTemplate.model_validate(
            {
                "projects": [{"project_id": harness.project_id}],
            }
        )
    )
    await harness.rule()
    await harness.app.automations.tick()
    first = (await harness.app.automations.occurrences())[0]
    assert first["status"] == "ready" and first["last_error"]
    await harness.app.runs.transition(occupied["executions"][0]["id"], ExecutionState.CANCELLED)
    await harness.app.automations.tick()
    occurrences = await harness.app.automations.occurrences()
    assert len(occurrences) == 1 and occurrences[0]["id"] == first["id"]
    assert occurrences[0]["status"] == "launched" and occurrences[0]["last_error"] is None


@pytest.mark.parametrize("operation", ["pause", "edit", "delete", "dismiss"])
async def test_rule_changes_and_dismissal_cancel_pending_occurrences(
    harness: Harness, operation: str
) -> None:
    rule = await harness.rule(mode="approval")
    await harness.app.automations.tick()
    occurrence = (await harness.app.automations.occurrences())[0]
    if operation == "delete":
        await harness.app.automations.delete(rule["id"])
    elif operation == "dismiss":
        await harness.app.automations.dismiss(occurrence["id"])
    else:
        await harness.app.automations.save(
            {"enabled": False} if operation == "pause" else {"name": "Changed"},
            rule["id"],
        )
    with pytest.raises(ValueError, match="no longer pending"):
        await harness.app.automations.authorize(occurrence["id"])
    await harness.app.automations.tick()
    assert len(await harness.app.automations.occurrences()) == 1
    assert not await harness.app.runs.list_runs()


async def test_approval_expiration_is_immediate_without_worker_tick(harness: Harness) -> None:
    await harness.rule(mode="approval")
    await harness.app.automations.tick()
    occurrence = (await harness.app.automations.occurrences())[0]
    async with harness.app.sessions.begin() as db:
        row = await db.get(AutomationOccurrence, occurrence["id"])
        row.resets_at = utcnow() - timedelta(seconds=1)
    assert (await harness.app.automations.occurrence(occurrence["id"]))["status"] == "expired"
    with pytest.raises(ValueError, match="no longer pending"):
        await harness.app.automations.authorize(occurrence["id"])
    assert harness.refreshes == 0 and not await harness.app.runs.list_runs()


async def test_stale_missing_or_expired_usage_never_launches(harness: Harness) -> None:
    await harness.rule()
    window = harness.windows[0]
    window.observed_at = utcnow() - timedelta(minutes=6)
    await harness.app.automations.tick()
    assert (await harness.app.automations.list_automations())[0]["last_error"]
    window.observed_at = utcnow()
    reset = window.resets_at
    window.resets_at = None
    await harness.app.automations.tick()
    window.resets_at = utcnow() - timedelta(seconds=1)
    await harness.app.automations.tick()
    window.resets_at = reset
    window.window_minutes = 300
    await harness.app.automations.tick()
    assert not await harness.app.runs.list_runs()
    assert not await harness.app.automations.occurrences()


async def test_missing_project_surfaces_configuration_error(harness: Harness) -> None:
    await harness.rule()
    async with harness.app.sessions.begin() as db:
        await db.delete(await db.get(Project, harness.project_id))
    await harness.app.automations.tick()
    occurrence = (await harness.app.automations.occurrences())[0]
    assert (
        occurrence["status"] == "configuration_error"
        and "no longer exists" in occurrence["last_error"]
    )
    await harness.app.automations.tick()
    assert len(await harness.app.automations.occurrences()) == 1


async def test_worker_runs_immediately_then_waits_fifteen_minutes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    intervals: list[int] = []

    async def tick() -> None:
        nonlocal calls
        calls += 1

    async def sleep(seconds: int) -> None:
        intervals.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(harness.app.automations, "tick", tick)
    monkeypatch.setattr("tokendrain.automations.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await harness.app.automations.serve()
    assert calls == 1 and intervals == [900]


async def test_authenticated_api_crud_and_same_origin_protection(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("a" * 40)
    settings = Settings(
        state_dir=tmp_path,
        backend="mock",
        admin_token_file=token_file,
        public_url="http://testserver",
    )
    web = create_app(settings, Overrides(start_workers=False))
    async with (
        LifespanManager(web),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=web),
            base_url="http://testserver",
            headers={"X-Tokendrain-Request": "1"},
        ) as client,
    ):
        assert (await client.get("/api/v1/automations")).status_code == 401
        await client.post("/api/v1/session", json={"token": "a" * 40})
        project = (await client.post("/api/v1/projects", json={"name": "project"})).json()
        body = {
            "name": "Weekly",
            "mode": "automatic",
            "run_template": {"projects": [{"project_id": project["id"]}]},
        }
        denied = await client.post(
            "/api/v1/automations", json=body, headers={"X-Tokendrain-Request": ""}
        )
        assert denied.status_code == 403
        created = await client.post("/api/v1/automations", json=body)
        assert created.status_code == 201
        rule = created.json()
        path = "/api/v1/automations/" + rule["id"]
        assert (await client.get(path)).json()["trigger"]["hours_before_reset"] == 12
        assert (await client.patch(path, json={"enabled": False})).json()["enabled"] is False
        assert (await client.patch(path, json={"mode": "unsupported"})).status_code == 409
        assert (
            await client.post("/api/v1/automations", json={**body, "mode": "approval"})
        ).status_code == 409
        assert (await client.get("/api/v1/automation-occurrences")).json() == []
        assert (await client.delete(path)).status_code == 204
        assert (await client.get(path)).status_code == 404


async def test_duplicate_provider_observations_send_one_approval(harness: Harness) -> None:
    harness.windows.append(harness.windows[0].model_copy())
    await harness.rule(mode="approval")
    await harness.app.automations.tick()
    assert len(harness.sent) == 1
    assert len(await harness.app.automations.occurrences()) == 1


async def test_can_pause_rule_after_project_is_removed(harness: Harness) -> None:
    rule = await harness.rule()
    async with harness.app.sessions.begin() as db:
        await db.delete(await db.get(Project, harness.project_id))
    paused = await harness.app.automations.save({"enabled": False}, rule["id"])
    assert paused["enabled"] is False
    with pytest.raises(ValueError, match="no longer exists"):
        await harness.app.automations.save({"enabled": True}, rule["id"])


async def test_enabled_idle_automation_refreshes_usage_without_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = 0

    async def probe(app: Application) -> dict[str, Any]:
        nonlocal probes
        probes += 1
        await app.runs.observe(
            [
                UsageWindow(
                    limit_id="codex",
                    window_minutes=10080,
                    used_percent=10,
                    resets_at=utcnow() + timedelta(hours=6),
                )
            ]
        )
        return {}

    monkeypatch.setattr("tokendrain.account_metadata.account_probe", probe)
    app = await Application.open(
        Settings(state_dir=tmp_path, backend="mock", auth_mode="none"),
        Overrides(start_workers=False),
    )
    try:
        await app.automations.tick()
        assert probes == 0
        project_id = await app.projects.create(ProjectCreate(name="Idle project"))
        await app.automations.save(
            AutomationInput.model_validate(
                {
                    "name": "Idle drain",
                    "mode": "automatic",
                    "run_template": {"projects": [{"project_id": project_id}]},
                }
            )
        )
        await app.automations.tick()
        assert probes == 1
        runs = await app.runs.list_runs()
        assert len(runs) == 1
        assert runs[0]["status"] == "queued" and not app.supervisor.active
    finally:
        await app.close()
