import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, cast
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy import delete, func, select, text

from tokendrain import __version__
from tokendrain.api.app import current, encoded
from tokendrain.api.schemas import (
    AuthImport,
    LoginInput,
    PlatformInput,
    ResizeInput,
    RestoreInput,
    SecretImport,
    SecretInput,
    SnapshotInput,
)
from tokendrain.application import Application
from tokendrain.auth.probe import AuthProbe
from tokendrain.db.models import (
    Event,
    GitHubApp,
    GitHubInstallation,
    Project,
    ProjectExecution,
    ProjectGitHub,
    ProjectSnapshot,
    Schedule,
    SecretEntry,
    Setting,
    UsageSnapshot,
)
from tokendrain.doctor import inspect_system
from tokendrain.domain import (
    TERMINAL,
    ProjectCreate,
    ProjectPatch,
    RunTemplate,
    ScheduleInput,
    utcnow,
)
from tokendrain.events import event_json
from tokendrain.github.provider import GitHubConfig, IntegrationInput
from tokendrain.scheduler.service import next_occurrence
from tokendrain.secrets import SecretService, parse_dotenv
from tokendrain.services import columns
from tokendrain.storage import FileProjectStorage
from tokendrain.usage import normalize_rate_limits

router = APIRouter()
API = "/api/v1"


def provider_change[F: Callable[..., Awaitable[Any]]](handler: F) -> F:
    @wraps(handler)
    async def guarded(request: Request, *args: Any, **kwargs: Any) -> Any:
        async with current(request).runs.credentials_change():
            return await handler(request, *args, **kwargs)

    return cast(F, guarded)


def ready(services: Application) -> None:
    if not services.supervisor.ready:
        raise HTTPException(503, "VM reconciliation is not complete; check system status")


async def no_active(services: Application) -> None:
    async with services.sessions() as db:
        active = await db.scalar(
            select(ProjectExecution.id)
            .where(ProjectExecution.status.not_in([state.value for state in TERMINAL]))
            .limit(1)
        )
        if active:
            raise ValueError("Cancel or finish active Runs before changing account credentials")


@router.post(API + "/session")
async def login(request: Request, body: LoginInput) -> Response:
    services = current(request)
    if not services.tokens.authenticate(body.token.get_secret_value()):
        raise HTTPException(401, "Administrative token was not accepted")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        "tokendrain_session",
        services.tokens.issue(),
        httponly=True,
        secure=services.settings.public_url.startswith("https://"),
        samesite="strict",
        max_age=43200,
    )
    return response


@router.delete(API + "/session", status_code=204)
async def logout() -> Response:
    response = Response(status_code=204)
    response.delete_cookie("tokendrain_session")
    return response


@router.get(API + "/session")
async def session_info() -> dict[str, bool]:
    return {"authenticated": True}


@router.get(API + "/projects")
async def projects(request: Request) -> Any:
    return encoded(await current(request).projects.list_projects())


@router.post(API + "/projects", status_code=201)
async def create_project(request: Request, body: ProjectCreate) -> Any:
    services = current(request)
    ready(services)
    project_id = await services.projects.create(body)
    return encoded(await services.projects.get(project_id))


@router.get(API + "/projects/{project_id}")
async def project(request: Request, project_id: str) -> Any:
    return encoded(await current(request).projects.get(project_id))


@router.patch(API + "/projects/{project_id}")
async def update_project(request: Request, project_id: str, body: ProjectPatch) -> Any:
    return encoded(await current(request).projects.patch(project_id, body))


@router.delete(API + "/projects/{project_id}", status_code=204)
async def remove_project(request: Request, project_id: str) -> Response:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        async with services.sessions.begin() as db:
            # Serialize admission/deletion in SQLite, including scheduler-created Runs.
            await db.execute(text("BEGIN IMMEDIATE"))
            active = await db.scalar(
                select(ProjectExecution.id)
                .where(
                    ProjectExecution.project_id == project_id,
                    ProjectExecution.status.not_in([state.value for state in TERMINAL]),
                )
                .limit(1)
            )
            if active:
                raise ValueError("Project has an active or queued execution")
            refs = list(
                (
                    await db.scalars(
                        select(SecretEntry.credential_ref).where(
                            SecretEntry.project_id == project_id
                        )
                    )
                ).all()
            )
            # Deletion is explicit; other projects' executions and Runs remain intact.
            await db.execute(
                delete(ProjectExecution).where(ProjectExecution.project_id == project_id)
            )
            await db.execute(delete(Project).where(Project.id == project_id))
        await services.storage.delete_project(project_id)
        for ref in refs:
            await services.credentials.delete(ref)
    await services.events.publish("project.deleted", project_id=project_id)
    return Response(status_code=204)


@router.get(API + "/projects/{project_id}/executions")
async def history(request: Request, project_id: str) -> Any:
    await current(request).projects.get(project_id)
    return encoded(await current(request).runs.executions(project_id=project_id))


@router.get(API + "/projects/{project_id}/storage")
async def storage_info(request: Request, project_id: str) -> Any:
    services = current(request)
    await services.projects.get(project_id)
    info = await services.storage.usage(project_id)
    return {
        "environment": {
            "size_bytes": info.environment_bytes,
            "used_bytes": info.environment_allocated_bytes,
        },
        "workspace": {
            "size_bytes": info.workspace_bytes,
            "used_bytes": info.workspace_allocated_bytes,
        },
    }


@router.get(API + "/projects/{project_id}/snapshots")
async def snapshots(request: Request, project_id: str) -> Any:
    services = current(request)
    await services.projects.get(project_id)
    async with services.sessions() as db:
        rows = (
            await db.scalars(
                select(ProjectSnapshot).where(ProjectSnapshot.project_id == project_id)
            )
        ).all()
        names = {row.id: row.name for row in rows}
    values = await services.storage.list_snapshots(project_id)
    return [
        {**value.model_dump(mode="json"), "name": names.get(value.id, "Recovered snapshot")}
        for value in values
    ]


@router.post(API + "/projects/{project_id}/snapshots", status_code=201)
async def snapshot(request: Request, project_id: str, body: SnapshotInput) -> Any:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        value = await services.projects.snapshot(project_id, body.name)
    await services.events.publish("project.snapshot", project_id=project_id)
    return encoded(value)


@router.post(API + "/projects/{project_id}/snapshots/{snapshot_id}/restore")
async def restore(request: Request, project_id: str, snapshot_id: str, body: RestoreInput) -> Any:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        await services.storage.restore(project_id, snapshot_id, body.scope)
        async with services.sessions.begin() as db:
            row = await db.get(Project, project_id)
            assert row
            # Restored files can contradict saved conversations; start a new inspected thread.
            row.thread_id = None
    await services.events.publish("project.restored", project_id=project_id)
    return await storage_info(request, project_id)


@router.delete(API + "/projects/{project_id}/snapshots/{snapshot_id}", status_code=204)
async def delete_snapshot(request: Request, project_id: str, snapshot_id: str) -> Response:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        await services.storage.delete_snapshot(project_id, snapshot_id)
        async with services.sessions.begin() as db:
            await db.execute(
                delete(ProjectSnapshot).where(
                    ProjectSnapshot.id == snapshot_id, ProjectSnapshot.project_id == project_id
                )
            )
    await services.events.publish("project.snapshot_deleted", project_id=project_id)
    return Response(status_code=204)


@router.post(API + "/projects/{project_id}/environment/reset")
async def reset_environment(request: Request, project_id: str) -> Any:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        await services.projects.snapshot(project_id, "Before environment reset")
        await services.storage.reset_environment(project_id)
        async with services.sessions.begin() as db:
            row = await db.get(Project, project_id)
            assert row
            row.thread_id = None
    await services.events.publish("project.environment_reset", project_id=project_id)
    return await storage_info(request, project_id)


@router.post(API + "/projects/{project_id}/storage/resize")
async def resize(request: Request, project_id: str, body: ResizeInput) -> Any:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        await services.storage.resize(project_id, body.scope, body.size_gib)
    await services.events.publish("project.storage_resized", project_id=project_id)
    return await storage_info(request, project_id)


@router.get(API + "/runs")
async def runs(request: Request) -> Any:
    return encoded(await current(request).runs.list_runs())


@router.post(API + "/runs", status_code=201)
async def create_run(request: Request, body: RunTemplate) -> Any:
    services = current(request)
    ready(services)
    if services.settings.backend != "mock" and not any(
        a.connected for a in await services.auth.accounts()
    ):
        raise ValueError("Connect OpenAI before starting a Run")
    return encoded(await services.runs.create(body))


@router.get(API + "/runs/{run_id}")
async def run(request: Request, run_id: str) -> Any:
    return encoded(await current(request).runs.get(run_id))


@router.post(API + "/runs/{run_id}/cancel")
async def cancel(request: Request, run_id: str) -> Any:
    services = current(request)
    await services.runs.cancel(run_id)
    return encoded(await services.runs.get(run_id))


@router.get(API + "/runs/{run_id}/events")
async def run_events(request: Request, run_id: str) -> Any:
    services = current(request)
    await services.runs.get(run_id)
    async with services.sessions() as db:
        rows = list(
            (
                await db.scalars(
                    select(Event).where(Event.run_id == run_id).order_by(Event.id.desc()).limit(500)
                )
            ).all()
        )
    return [event_json(row) for row in reversed(rows)]


@router.get(API + "/events")
async def events(request: Request) -> StreamingResponse:
    services = current(request)
    try:
        after = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        raise HTTPException(400, "Invalid Last-Event-ID") from None
    if after == 0:
        async with services.sessions() as db:
            after = max(0, int(await db.scalar(select(func.max(Event.id))) or 0) - 100)
    return StreamingResponse(
        services.events.stream(after),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


async def account_probe(services: Application, *, force: bool = False) -> dict[str, Any]:
    assert services.probe_lock is not None
    async with services.probe_lock:
        cached = services.probe_cache
        if not force and cached and time.time() - float(str(cached["observed_at"])) < 60:
            return cached
        accounts = await services.auth.accounts()
        if not any(account.connected for account in accounts):
            return {
                "models": [],
                "usage": {},
                "observed_at": time.time(),
                "usage_error": "Connect OpenAI to observe usage.",
            }
        result = await AuthProbe(
            services.auth, services.http, services.settings.auth_runtime_dir
        ).read()
        value = result.model_dump()
        services.probe_cache = value
        windows = normalize_rate_limits(result.usage)
        if windows:
            await services.runs.observe(windows)
        return value


@router.get(API + "/usage")
async def usage(request: Request) -> Any:
    services = current(request)
    # Live execution observations are authoritative and avoid extra host subprocesses.
    windows = await services.runs.latest_usage()
    if windows and (utcnow() - windows[0].observed_at).total_seconds() < 60:
        return [window.model_dump(mode="json") for window in windows]
    try:
        await account_probe(services)
    except (ValueError, OSError, TimeoutError, httpx.HTTPError):
        # Preserve timestamped last observation, never invent a zero-usage window.
        pass
    return [window.model_dump(mode="json") for window in await services.runs.latest_usage()]


@router.get(API + "/auth/openai/models")
async def models(request: Request) -> Any:
    values = (await account_probe(current(request)))["models"]
    return [
        {
            "id": value["id"],
            "name": value["display_name"],
            "reasoning_efforts": value["supported_reasoning_efforts"],
        }
        for value in values
    ]


@router.get(API + "/schedules")
async def schedules(request: Request) -> Any:
    async with current(request).sessions() as db:
        rows = (await db.scalars(select(Schedule).order_by(Schedule.next_run_at))).all()
        return encoded([columns(row) for row in rows])


async def validate_projects(services: Application, template: RunTemplate) -> None:
    async with services.sessions() as db:
        for config in template.projects:
            if not await db.get(Project, config.project_id):
                raise ValueError(f"Project {config.project_id} does not exist")


@router.post(API + "/schedules", status_code=201)
async def create_schedule(request: Request, body: ScheduleInput) -> Any:
    services = current(request)
    await validate_projects(services, body.run_template)
    row = Schedule(
        **body.model_dump(mode="json"),
        next_run_at=next_occurrence(body.cron, body.timezone, utcnow()),
    )
    async with services.sessions.begin() as db:
        db.add(row)
    await services.events.publish("schedule.created")
    return encoded(columns(row))


@router.patch(API + "/schedules/{schedule_id}")
async def update_schedule(request: Request, schedule_id: str, body: dict[str, Any]) -> Any:
    services = current(request)
    async with services.sessions() as db:
        row = await db.get(Schedule, schedule_id)
        if row is None:
            raise LookupError("Schedule not found")
        merged = {key: getattr(row, key) for key in ScheduleInput.model_fields}
    merged.update(body)
    values = ScheduleInput.model_validate(merged)
    await validate_projects(services, values.run_template)
    async with services.sessions.begin() as db:
        row = await db.get(Schedule, schedule_id)
        if row is None:
            raise LookupError("Schedule not found")
        for key, value in values.model_dump(mode="json").items():
            setattr(row, key, value)
        row.next_run_at = next_occurrence(values.cron, values.timezone, utcnow())
    await services.events.publish("schedule.updated")
    return encoded(columns(row))


@router.delete(API + "/schedules/{schedule_id}", status_code=204)
async def delete_schedule(request: Request, schedule_id: str) -> Response:
    services = current(request)
    async with services.sessions.begin() as db:
        row = await db.get(Schedule, schedule_id)
        if row is None:
            raise LookupError("Schedule not found")
        await db.delete(row)
    await services.events.publish("schedule.deleted")
    return Response(status_code=204)


@router.get(API + "/projects/{project_id}/secrets")
async def secrets_list(request: Request, project_id: str) -> Any:
    services = current(request)
    return encoded(
        await SecretService(services.sessions, services.credentials).list_entries(project_id)
    )


@router.put(API + "/projects/{project_id}/secrets/{name}")
async def put_secret(request: Request, project_id: str, name: str, body: SecretInput) -> Any:
    services = current(request)
    await SecretService(services.sessions, services.credentials).put_many(
        project_id,
        {
            name: (
                body.value.get_secret_value() if body.value is not None else None,
                body.description,
            )
        },
    )
    await services.events.publish("project.secrets_updated", project_id=project_id)
    return next(entry for entry in await secrets_list(request, project_id) if entry["name"] == name)


@router.post(API + "/projects/{project_id}/secrets/import")
async def import_secrets(request: Request, project_id: str, body: SecretImport) -> Any:
    services = current(request)
    values = parse_dotenv(body.dotenv.get_secret_value(), body.descriptions)
    await SecretService(services.sessions, services.credentials).put_many(project_id, values)
    await services.events.publish("project.secrets_updated", project_id=project_id)
    return await secrets_list(request, project_id)


@router.delete(API + "/projects/{project_id}/secrets/{name}", status_code=204)
async def delete_secret(request: Request, project_id: str, name: str) -> Response:
    services = current(request)
    await SecretService(services.sessions, services.credentials).delete(project_id, name)
    await services.events.publish("project.secrets_updated", project_id=project_id)
    return Response(status_code=204)


@router.get(API + "/auth/openai")
async def openai_status(request: Request) -> Any:
    services = current(request)
    accounts = [account for account in await services.auth.accounts() if account.connected]
    account = accounts[0] if accounts else None
    return {
        "connected": account is not None,
        "method": ("chatgpt" if account.method == "siwc" else "import") if account else None,
        "account_label": (account.email or account.subject) if account else None,
        "usage_error": (services.probe_cache or {}).get("usage_error"),
    }


@router.post(API + "/auth/openai/login")
async def openai_login(request: Request) -> Any:
    services = current(request)
    await no_active(services)
    existing = next((a for a in await services.auth.accounts() if a.method == "siwc"), None)
    result = await services.auth.begin_sign_in(
        services.settings.openai_redirect_uri, existing.id if existing else None
    )
    return {"id": result.state, "url": result.url}


@router.get("/auth/callback")
@provider_change
async def openai_callback(
    request: Request,
    state: str,
    code: str = "",
    client_id: str | None = None,
    error: str | None = None,
) -> Response:
    services = current(request)
    await no_active(services)
    account = await services.auth.complete_sign_in(state, code, client_id, error)
    # One selected account per host; disconnected records retain returning-client metadata.
    for old in await services.auth.accounts():
        if old.connected and old.id != account.id:
            await services.auth.sign_out(old.id)
    services.probe_cache = None
    async with services.sessions.begin() as db:
        await db.execute(delete(UsageSnapshot))
    await services.events.publish("openai.connected")
    return HTMLResponse(
        "<html><body><h1>OpenAI connected</h1><p>You can close this tab and "
        "return to tokendrain.</p><a href='/settings'>Open settings</a></body></html>"
    )


@router.post(API + "/auth/openai/import")
@provider_change
async def import_openai(request: Request, body: AuthImport) -> Any:
    services = current(request)
    await no_active(services)
    account = await services.auth.import_auth_json(json.dumps(body.auth_json).encode())
    for old in await services.auth.accounts():
        if old.connected and old.id != account.id:
            await services.auth.sign_out(old.id)
    services.probe_cache = None
    async with services.sessions.begin() as db:
        await db.execute(delete(UsageSnapshot))
    await services.events.publish("openai.connected")
    return await openai_status(request)


@router.delete(API + "/auth/openai", status_code=204)
@provider_change
async def disconnect_openai(request: Request) -> Response:
    services = current(request)
    await no_active(services)
    revoked = True
    for account in await services.auth.accounts():
        if account.connected:
            revoked = await services.auth.sign_out(account.id) and revoked
    services.probe_cache = None
    async with services.sessions.begin() as db:
        await db.execute(delete(UsageSnapshot))
    await services.events.publish(
        "openai.disconnected",
        "Provider revocation succeeded"
        if revoked
        else "Disconnected locally; provider revocation was unavailable",
    )
    return Response(status_code=204)


@router.get(API + "/integrations/github")
async def github_status(request: Request) -> Any:
    services = current(request)
    async with services.sessions() as db:
        app = await db.get(GitHubApp, 1)
        installations = (await db.scalars(select(GitHubInstallation))).all()
        return {
            "configured": app is not None,
            "app_id": app.app_id if app else None,
            "app_slug": app.slug if app else None,
            "installation_url": f"https://github.com/apps/{app.slug}/installations/new"
            if app
            else None,
            "installations": [{**columns(row), "id": str(row.id)} for row in installations],
        }


async def github_app(services: Application) -> GitHubApp:
    async with services.sessions() as db:
        app = await db.get(GitHubApp, 1)
        if app is None:
            raise ValueError("Configure a GitHub App first")
        return app


@router.put(API + "/integrations/github")
@provider_change
async def configure_github(request: Request, body: GitHubConfig) -> Any:
    services = current(request)
    await no_active(services)
    ref = f"github-key-{uuid4().hex}"
    try:
        await services.credentials.put(ref, body.private_key.get_secret_value().encode())
        info = await services.github.inspect_app(body.app_id, ref)
        async with services.sessions.begin() as db:
            old = await db.get(GitHubApp, 1)
            old_ref = old.credential_ref if old else None
            changed_app = old is not None and old.app_id != body.app_id
            if old:
                old.app_id, old.slug, old.credential_ref = body.app_id, info["slug"], ref
            else:
                db.add(GitHubApp(id=1, app_id=body.app_id, slug=info["slug"], credential_ref=ref))
            await db.execute(delete(GitHubInstallation))
            # Grants must be reselected for a different App, but survive key rotation.
            if changed_app:
                await db.execute(delete(ProjectGitHub))
    except BaseException:
        # Cancellation during commit has an uncertain result. Never remove a key
        # that SQLite already references; an encrypted orphan is safe to retain.
        try:
            async with services.sessions() as db:
                saved = await db.get(GitHubApp, 1)
                referenced = saved is not None and saved.credential_ref == ref
            if not referenced:
                await services.credentials.delete(ref)
        except Exception:
            pass
        raise
    if old_ref:
        await services.credentials.delete(old_ref)
    services.github.clear_cache()
    await sync_github(request)
    return await github_status(request)


@router.post(API + "/integrations/github/sync")
@provider_change
async def sync_github(request: Request) -> Any:
    services = current(request)
    app = await github_app(services)
    values = await services.github.installations(app.app_id, app.credential_ref)
    async with services.sessions.begin() as db:
        await db.execute(delete(GitHubInstallation))
        db.add_all([GitHubInstallation(**value) for value in values])
    await services.events.publish("github.synced")
    return await github_status(request)


@router.get(API + "/integrations/github/setup")
async def github_setup(request: Request) -> Response:
    # Public redirect only: Strict cookies are absent on a cross-site GitHub return.
    # The authenticated settings page performs a CSRF-protected authoritative sync.
    return RedirectResponse("/settings?github=installed", status_code=303)


@router.get(API + "/integrations/github/installations/{installation_id}/repositories")
async def repositories(request: Request, installation_id: int) -> Any:
    services = current(request)
    app = await github_app(services)
    async with services.sessions() as db:
        if not await db.get(GitHubInstallation, installation_id):
            raise LookupError("Installation not found; sync GitHub first")
    return await services.github.repositories(app.app_id, app.credential_ref, installation_id)


@router.get(API + "/projects/{project_id}/github")
async def project_github(request: Request, project_id: str) -> Any:
    services = current(request)
    await services.projects.get(project_id)
    async with services.sessions() as db:
        row = await db.get(ProjectGitHub, project_id)
        return {**columns(row), "installation_id": str(row.installation_id)} if row else None


@router.put(API + "/projects/{project_id}/github")
async def attach_github(request: Request, project_id: str, body: IntegrationInput) -> Any:
    services = current(request)
    await services.projects.require_idle(project_id)
    app_revision = await github_app(services)
    repos = await repositories(request, body.installation_id)
    repository = next((repo for repo in repos if repo["id"] == body.repository_id), None)
    if repository is None or repository["full_name"] != body.repository_name:
        raise ValueError("Repository is not available to this installation")
    async with services.sessions.begin() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        app = await db.get(GitHubApp, 1)
        if (
            await db.get(Setting, "provider_change")
            or app is None
            or app.credential_ref != app_revision.credential_ref
        ):
            raise ValueError("GitHub configuration changed; reload repositories and retry")
        if not await db.get(Project, project_id):
            raise LookupError("Project not found")
        active = await db.scalar(
            select(ProjectExecution.id)
            .where(
                ProjectExecution.project_id == project_id,
                ProjectExecution.status.not_in([state.value for state in TERMINAL]),
            )
            .limit(1)
        )
        if active:
            raise ValueError("Project has an active or queued execution")
        installation = await db.get(GitHubInstallation, body.installation_id)
        if installation is None:
            raise ValueError("Installation changed; sync GitHub and reload repositories")
        for permission, requested in body.permissions.items():
            maximum = installation.permissions.get(permission, "none")
            if maximum == "none" or (requested == "write" and maximum != "write"):
                raise ValueError(f"Installation does not grant {permission}:{requested}")
        row = await db.get(ProjectGitHub, project_id)
        if row:
            for key, value in body.model_dump().items():
                setattr(row, key, value)
        else:
            db.add(ProjectGitHub(project_id=project_id, **body.model_dump()))
    await services.events.publish("project.github_updated", project_id=project_id)
    return await project_github(request, project_id)


@router.delete(API + "/projects/{project_id}/github", status_code=204)
async def detach_github(request: Request, project_id: str) -> Response:
    services = current(request)
    await services.projects.require_idle(project_id)
    async with services.sessions.begin() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        active = await db.scalar(
            select(ProjectExecution.id)
            .where(
                ProjectExecution.project_id == project_id,
                ProjectExecution.status.not_in([state.value for state in TERMINAL]),
            )
            .limit(1)
        )
        if active:
            raise ValueError("Project has an active or queued execution")
        await db.execute(delete(ProjectGitHub).where(ProjectGitHub.project_id == project_id))
    await services.events.publish("project.github_updated", project_id=project_id)
    return Response(status_code=204)


@router.get(API + "/system")
async def system(request: Request) -> Any:
    services = current(request)
    settings = services.settings
    checks = await asyncio.to_thread(inspect_system, settings)
    checks.append(
        {
            "name": "reconciliation",
            "ok": services.supervisor.ready,
            "message": services.supervisor.recovery_error or "VM recovery complete",
        }
    )
    return {
        "version": __version__,
        "backend": settings.backend,
        "concurrency": settings.max_concurrency,
        "vm_defaults": {
            "vcpus": settings.default_vcpus,
            "memory_mib": settings.default_memory_mib,
            "disk_gib": settings.default_disk_gib,
        },
        "uptime_seconds": time.monotonic() - services.started_at,
        "active_executions": len(services.supervisor.active),
        "checks": checks,
    }


@router.patch(API + "/system")
async def update_system(request: Request, body: PlatformInput) -> Any:
    services = current(request)
    settings = services.settings
    if body.concurrency > settings.concurrency_limit:
        raise ValueError("Concurrency exceeds the NixOS platform limit")
    if body.vm_defaults.vcpus > settings.vcpus_limit:
        raise ValueError("vCPUs exceed the NixOS platform limit")
    if body.vm_defaults.memory_mib > settings.memory_mib_limit:
        raise ValueError("Memory exceeds the NixOS platform limit")
    async with services.sessions.begin() as db:
        row = await db.get(Setting, "platform")
        if row:
            row.value = body.model_dump()
        else:
            db.add(Setting(key="platform", value=body.model_dump()))
    settings = services.settings
    settings.max_concurrency = body.concurrency
    settings.default_vcpus = body.vm_defaults.vcpus
    settings.default_memory_mib = body.vm_defaults.memory_mib
    settings.default_disk_gib = body.vm_defaults.disk_gib
    if isinstance(services.storage, FileProjectStorage):
        services.storage.disk_gib = body.vm_defaults.disk_gib
    await services.events.publish("system.updated")
    return await system(request)
