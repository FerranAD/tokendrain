import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from asgi_lifespan import LifespanManager
from sqlalchemy import select

from tokendrain.api.app import create_app
from tokendrain.application import Application, Overrides
from tokendrain.config import Settings
from tokendrain.db.models import Event, ProjectExecution, Schedule, SecretEntry, Setting
from tokendrain.domain import utcnow


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Application]]:
    settings = Settings(state_dir=tmp_path, backend="mock", public_url="http://testserver")
    app = create_app(settings, Overrides(start_workers=False))
    async with LifespanManager(app):
        services = app.state.services
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"X-Tokendrain-Request": "1"},
        ) as client:
            result = await client.post(
                "/api/v1/session", json={"token": (tmp_path / "admin-token").read_text()}
            )
            assert result.status_code == 200
            yield client, services


async def new_project(client: httpx.AsyncClient, name: str = "Build useful software") -> str:
    result = await client.post("/api/v1/projects", json={"name": name, "description": "Make a CLI"})
    assert result.status_code == 201, result.text
    return str(result.json()["id"])


async def test_auth_origin_host_upload_and_secret_validation(
    api: tuple[httpx.AsyncClient, Application],
):
    client, services = api
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/api/v1/system", headers={"host": "evil.example"})).status_code == 400
    assert (
        await client.post(
            "/api/v1/projects",
            json={"name": "cross site"},
            headers={"origin": "https://evil.example"},
        )
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/projects",
            json={"name": "missing header"},
            headers={"X-Tokendrain-Request": "0"},
        )
    ).status_code == 403
    assert (
        await client.post("/api/v1/session", content=b"x" * (2 * 1024 * 1024 + 1))
    ).status_code == 413
    response = await client.put(
        "/api/v1/projects/none/secrets/KEY",
        json={"value": {"secret": "never-echo-me"}, "description": "purpose"},
    )
    assert response.status_code == 422
    assert "never-echo-me" not in response.text
    assert (await client.delete("/api/v1/session")).status_code == 204
    assert (await client.get("/api/v1/projects")).status_code == 401
    token = (services.settings.state_dir / "admin-token").read_text()
    assert (
        await client.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200
    assert (await client.post("/api/v1/session", json={"token": "wrong"})).status_code == 401


async def test_project_run_reservation_crud(api: tuple[httpx.AsyncClient, Application]):
    client, services = api
    first, second = await new_project(client, "first"), await new_project(client, "second")
    value = await client.patch(
        f"/api/v1/projects/{first}",
        json={
            "task_log": "- [ ] Build",
            "next_run_feedback": "Test carefully",
            "description": "New goal",
        },
    )
    assert value.json()["task_log"] == "- [ ] Build"
    template = {
        "projects": [{"project_id": first}, {"project_id": second}],
        "parallel": False,
        "stop_conditions": [{"kind": "elapsed", "seconds": 60}],
    }
    result = await client.post("/api/v1/runs", json=template)
    assert result.status_code == 201, result.text
    run = result.json()
    assert len(run["executions"]) == 2
    assert run["parallel"] is False
    assert (await client.post("/api/v1/runs", json=template)).status_code == 409
    assert (await client.delete(f"/api/v1/projects/{first}")).status_code == 409
    assert (
        await client.post(f"/api/v1/projects/{first}/environment/reset", json={})
    ).status_code == 409
    cancelled = await client.post(f"/api/v1/runs/{run['id']}/cancel", json={})
    assert cancelled.json()["cancel_requested"]
    # Distinct project reservations are atomic; no partial run survives a failed overlap.
    async with services.sessions() as db:
        executions = (await db.scalars(select(ProjectExecution))).all()
        assert len(executions) == 2
    assert len((await client.get("/api/v1/runs")).json()) == 1
    assert len((await client.get(f"/api/v1/projects/{first}/executions")).json()) == 1
    assert (await client.get("/api/v1/projects/missing")).status_code == 404


async def test_snapshots_domain_restore_reset_resize_and_delete(
    api: tuple[httpx.AsyncClient, Application],
):
    client, services = api
    project = await new_project(client)
    storage = await services.storage.usage(project)
    await asyncio.to_thread(storage.workspace.write_bytes, b"original workspace")
    response = await client.post(f"/api/v1/projects/{project}/snapshots", json={"name": "safe"})
    assert response.status_code == 201, response.text
    snapshot = response.json()["id"]
    await asyncio.to_thread(storage.workspace.write_bytes, b"modified workspace")
    await asyncio.to_thread(storage.environment.write_bytes, b"keep environment")
    restored = await client.post(
        f"/api/v1/projects/{project}/snapshots/{snapshot}/restore", json={"scope": "workspace"}
    )
    assert restored.status_code == 200, restored.text
    assert storage.workspace.read_bytes() == b"original workspace"
    assert storage.environment.read_bytes() == b"keep environment"
    assert (
        await client.post(f"/api/v1/projects/{project}/environment/reset", json={})
    ).status_code == 200
    assert storage.workspace.read_bytes() == b"original workspace"
    assert storage.environment.read_bytes() != b"keep environment"
    assert len((await client.get(f"/api/v1/projects/{project}/snapshots")).json()) == 2
    assert (
        await client.post(
            f"/api/v1/projects/{project}/storage/resize",
            json={"scope": "workspace", "size_gib": 50},
        )
    ).status_code == 200
    assert (
        await client.delete(f"/api/v1/projects/{project}/snapshots/{snapshot}")
    ).status_code == 204
    assert (await client.delete(f"/api/v1/projects/{project}")).status_code == 204
    assert not storage.workspace.exists()
    assert (await client.get("/api/v1/projects")).json() == []


async def test_secrets_never_return_values_and_import_is_atomic(
    api: tuple[httpx.AsyncClient, Application],
):
    client, services = api
    project = await new_project(client)
    base = f"/api/v1/projects/{project}/secrets"
    result = await client.post(
        base + "/import",
        json={
            "dotenv": 'TOKEN="sensitive-value"\nURL=https://example.com/${TOKEN}\n',
            "descriptions": {"TOKEN": "Testing account", "URL": "Test server"},
        },
    )
    assert result.status_code == 200, result.text
    assert "sensitive-value" not in result.text
    async with services.sessions() as db:
        row = await db.get(SecretEntry, (project, "URL"))
        assert row
        assert await services.credentials.get(row.credential_ref) == b"https://example.com/${TOKEN}"
    assert (
        await client.put(base + "/TOKEN", json={"description": "Updated purpose"})
    ).status_code == 200
    invalid = await client.post(
        base + "/import",
        json={"dotenv": "FIRST=one\nSECOND=two", "descriptions": {"FIRST": "described"}},
    )
    assert invalid.status_code == 409
    assert {r["name"] for r in (await client.get(base)).json()} == {"TOKEN", "URL"}
    assert (
        await client.put(base + "/PATH", json={"description": "reserved", "value": "bad"})
    ).status_code == 409
    for path in (services.settings.state_dir / "credentials").iterdir():
        assert b"sensitive-value" not in path.read_bytes()
    assert b"sensitive-value" not in services.settings.database_path.read_bytes()
    assert (await client.delete(base + "/TOKEN")).status_code == 204


async def test_schedule_creates_ordinary_run_and_skips_overlap(
    api: tuple[httpx.AsyncClient, Application],
):
    client, services = api
    project = await new_project(client)
    template = {"projects": [{"project_id": project}], "parallel": True}
    result = await client.post(
        "/api/v1/schedules",
        json={
            "name": "Nightly",
            "cron": "0 3 * * *",
            "timezone": "Europe/Madrid",
            "enabled": True,
            "run_template": template,
        },
    )
    assert result.status_code == 201, result.text
    schedule_id = result.json()["id"]
    now = utcnow()
    async with services.sessions.begin() as db:
        row = await db.get(Schedule, schedule_id)
        assert row
        row.next_run_at = now - timedelta(minutes=1)
    await services.scheduler.tick(now)
    await services.scheduler.tick(now)
    runs = (await client.get("/api/v1/runs")).json()
    assert len(runs) == 1 and runs[0]["schedule_id"] == schedule_id
    await services.scheduler.tick(now + timedelta(days=2))
    assert len((await client.get("/api/v1/runs")).json()) == 1
    async with services.sessions() as db:
        skipped = (await db.scalars(select(Event).where(Event.type == "schedule.skipped"))).all()
        assert len(skipped) == 1
    update = await client.patch(f"/api/v1/schedules/{schedule_id}", json={"enabled": False})
    assert update.status_code == 200 and update.json()["enabled"] is False
    assert (await client.delete(f"/api/v1/schedules/{schedule_id}")).status_code == 204


async def test_sse_replay_and_live_delivery(api: tuple[httpx.AsyncClient, Application]):
    _, services = api
    await services.events.publish("test.first", "first")
    async with services.sessions() as db:
        row = await db.scalar(select(Event).where(Event.type == "test.first"))
        assert row
        first_id = row.id
    stream = services.events.stream(first_id - 1)
    first = await anext(stream)
    assert f"id: {first_id}" in first and '"message": "first"' in first
    async with asyncio.TaskGroup() as group:
        pending = group.create_task(anext(stream))
        await services.events.publish("test.second", "second")
        value = await asyncio.wait_for(pending, 2)
        assert "second" in value
    await stream.aclose()
    resumed = services.events.stream(first_id)
    assert "second" in await anext(resumed)
    await resumed.aclose()


async def test_single_daemon_lock(api: tuple[httpx.AsyncClient, Application]):
    _, services = api
    with pytest.raises(RuntimeError, match="Another tokendraind"):
        await Application.open(services.settings, Overrides(start_workers=False))


async def test_settings_persist_and_disconnected_metadata(
    api: tuple[httpx.AsyncClient, Application],
):
    client, _ = api
    assert (await client.get("/api/v1/usage")).json() == []
    assert (await client.get("/api/v1/auth/openai/models")).json() == []
    assert (await client.get("/api/v1/auth/openai")).json()["connected"] is False
    assert (await client.get("/api/v1/integrations/github")).json()["configured"] is False
    project = await new_project(client)
    assert (await client.get(f"/api/v1/projects/{project}/github")).json() is None
    result = await client.patch(
        "/api/v1/system",
        json={"concurrency": 3, "vm_defaults": {"vcpus": 2, "memory_mib": 2048, "disk_gib": 20}},
    )
    assert result.status_code == 200
    assert result.json()["concurrency"] == 3
    assert "master.key" not in json.dumps(result.json().get("vm_defaults"))


async def test_chunked_upload_is_bounded_without_content_length(api):
    client, _ = api

    async def chunks():
        for _ in range(4):
            yield b"x" * 1024 * 1024

    response = await client.post("/api/v1/session", content=chunks())
    assert response.status_code == 413


async def test_public_github_callback_is_redirect_only(api):
    client, _ = api
    await client.delete("/api/v1/session")
    response = await client.get("/api/v1/integrations/github/setup?installation_id=123")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?github=installed"


async def test_platform_resource_ceiling(api):
    client, services = api
    services.settings.concurrency_limit = 2
    result = await client.patch(
        "/api/v1/system",
        json={"concurrency": 3, "vm_defaults": {"vcpus": 2, "memory_mib": 2048, "disk_gib": 20}},
    )
    assert result.status_code == 409


async def test_run_creation_and_deletion_are_atomic_under_contention(api):
    from sqlalchemy import text

    from tokendrain.db.models import Run

    client, services = api
    for _ in range(8):
        project_id = await new_project(client)
        # Hold the SQLite writer so both operations reach admission concurrently.
        async with services.sessions.begin() as blocker:
            await blocker.execute(text("BEGIN IMMEDIATE"))
            creating = asyncio.create_task(
                client.post("/api/v1/runs", json={"projects": [{"project_id": project_id}]})
            )
            deleting = asyncio.create_task(client.delete(f"/api/v1/projects/{project_id}"))
            await asyncio.sleep(0.01)
        try:
            created, removed = await asyncio.gather(creating, deleting)
        finally:
            for task in (creating, deleting):
                if not task.done():
                    task.cancel()
            await asyncio.gather(creating, deleting, return_exceptions=True)
        assert (created.status_code, removed.status_code) in {(201, 409), (409, 204)}
    async with services.sessions() as db:
        run_rows = (await db.scalars(select(Run))).all()
        execution_rows = (await db.scalars(select(ProjectExecution))).all()
        assert len(run_rows) == len(execution_rows)
        assert {row.id for row in run_rows} == {row.run_id for row in execution_rows}


async def test_provider_configuration_serializes_with_run_admission(api):
    client, services = api
    project_id = await new_project(client)
    template = {"projects": [{"project_id": project_id}]}
    async with services.runs.credentials_change():
        # Configuration can invoke a second provider operation in the same task.
        async with services.runs.credentials_change():
            assert (await client.post("/api/v1/runs", json=template)).status_code == 409

        async def competing_change():
            with pytest.raises(ValueError, match="in progress"):
                async with services.runs.credentials_change():
                    pytest.fail("Concurrent provider operation admitted")

        await asyncio.create_task(competing_change())
    assert (await client.post("/api/v1/runs", json=template)).status_code == 201
    with pytest.raises(ValueError, match="active Runs"):
        async with services.runs.credentials_change():
            pytest.fail("Provider changed under an active Run")


async def test_provider_configuration_cancellation_releases_admission(api):
    client, services = api
    project_id = await new_project(client)
    entered = asyncio.Event()

    async def changing():
        async with services.runs.credentials_change():
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(changing())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with services.sessions() as db:
        assert await db.get(Setting, "provider_change") is None
    assert (
        await client.post("/api/v1/runs", json={"projects": [{"project_id": project_id}]})
    ).status_code == 201


async def test_restart_clears_stale_provider_operation(tmp_path):
    settings = Settings(state_dir=tmp_path, backend="mock", public_url="http://testserver")
    first = await Application.open(settings, Overrides(start_workers=False))
    async with first.sessions.begin() as db:
        db.add(Setting(key="provider_change", value={"started_at": utcnow().isoformat()}))
    await first.close()
    second = await Application.open(settings, Overrides(start_workers=False))
    try:
        async with second.sessions() as db:
            assert await db.get(Setting, "provider_change") is None
    finally:
        await second.close()


async def test_secret_mutations_reject_reserved_project_and_preserve_values(api):
    from tokendrain.secrets import SecretService

    client, services = api
    project_id = await new_project(client)
    base = f"/api/v1/projects/{project_id}/secrets"
    assert (
        await client.put(base + "/TOKEN", json={"value": "original", "description": "test"})
    ).status_code == 200
    async with services.sessions() as db:
        row = await db.get(SecretEntry, (project_id, "TOKEN"))
        assert row
        original_ref = row.credential_ref
    assert (
        await client.post("/api/v1/runs", json={"projects": [{"project_id": project_id}]})
    ).status_code == 201
    for response in [
        await client.put(base + "/TOKEN", json={"value": "changed", "description": "new"}),
        await client.put(base + "/TOKEN", json={"description": "changed purpose"}),
        await client.delete(base + "/TOKEN"),
    ]:
        assert response.status_code == 409
    assert await services.credentials.get(original_ref) == b"original"
    # Cleanup after an uncertain commit must preserve an already referenced value.
    secret_service = SecretService(services.sessions, services.credentials)
    await services.credentials.put("orphan", b"discard")
    await secret_service._discard_unreferenced([original_ref, "orphan"])
    assert await services.credentials.get(original_ref) == b"original"
    assert await services.credentials.get("orphan") is None
    assert (await client.get(base)).json()[0]["description"] == "test"
