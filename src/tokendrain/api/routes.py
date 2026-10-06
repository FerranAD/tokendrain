import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from sqlalchemy import delete, func, select, text
from starlette.background import BackgroundTask

from tokendrain import __version__
from tokendrain.account_metadata import account_probe
from tokendrain.agents import AGENT_NAMES, reset_account, switch_agent
from tokendrain.api.app import current, encoded
from tokendrain.api.schemas import (
    AgentInput,
    AuthImport,
    ClaudeLoginCode,
    LoginInput,
    PlatformInput,
    ProjectCreateInput,
    ResizeInput,
    SecretImport,
    SecretInput,
)
from tokendrain.application import Application
from tokendrain.auth.probe import requires_reauthentication
from tokendrain.automations import AutomationInput
from tokendrain.codex.rpc import RpcError
from tokendrain.db.models import (
    Event,
    GitHubApp,
    GitHubInstallation,
    GitHubPolicy,
    Project,
    ProjectExecution,
    ProjectGitHub,
    Schedule,
    SecretEntry,
    Setting,
)
from tokendrain.doctor import inspect_service_checks
from tokendrain.domain import (
    TERMINAL,
    ProjectCreate,
    ProjectPatch,
    RunTemplate,
    ScheduleInput,
    TaskMutation,
    utcnow,
)
from tokendrain.events import event_json
from tokendrain.github.policy import reconcile_all, reserve_pr_policy
from tokendrain.github.provider import IntegrationInput, RepositoryInput, app_manifest
from tokendrain.notifications import NtfyInput
from tokendrain.scheduler.service import next_occurrence
from tokendrain.secrets import SecretService, parse_dotenv
from tokendrain.services import columns, validate_github_access
from tokendrain.storage import FileProjectStorage

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
async def session_info(request: Request) -> dict[str, Any]:
    return {"authenticated": True, "auth_mode": current(request).settings.auth_mode}


@router.get(API + "/projects")
async def projects(request: Request) -> Any:
    return encoded(await current(request).projects.list_projects())


@router.post(API + "/projects", status_code=201)
async def create_project(request: Request, body: ProjectCreateInput) -> Any:
    services = current(request)
    ready(services)
    credential_ref = None
    binding = None
    if body.github:
        app = await github_app(services)
        credential_ref = app.credential_ref
        binding = await resolve_github_repository(request, body.github)
        async with services.sessions() as db:
            await validate_github_access(db, binding, credential_ref)
    project_id = await services.projects.create(
        ProjectCreate.model_validate(body.model_dump(exclude={"github"})),
        binding,
        credential_ref,
    )
    if binding:
        await reconcile_saved(services)
    return encoded(await services.projects.get(project_id))


@router.get(API + "/projects/{project_id}")
async def project(request: Request, project_id: str) -> Any:
    return encoded(await current(request).projects.get(project_id))


@router.patch(API + "/projects/{project_id}")
async def update_project(request: Request, project_id: str, body: ProjectPatch) -> Any:
    return encoded(await current(request).projects.patch(project_id, body))


async def workspace_result(
    request: Request, project_id: str, operation: str, path: str, allow_large: bool = False
) -> tuple[Path, dict[str, Any]]:
    from tokendrain.storage.export import workspace_operation, workspace_path

    services = current(request)
    ready(services)
    path = workspace_path(path)
    await services.projects.require_idle(project_id)
    suffix = {"tree": ".json", "archive": ".zip", "file": ".bin", "download": ".bin"}[operation]
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        if services.settings.backend == "mock":
            # Test backend only: a project-scoped directory stands in for the offline disk.
            from tokendrain.storage.files import durable_io

            root = (
                services.settings.state_dir / "projects" / str(UUID(project_id)) / "mock-workspace"
            )
            root.mkdir(parents=True, exist_ok=True)
            directory = services.settings.state_dir / "exports"
            directory.mkdir(mode=0o700, exist_ok=True)
            destination = directory / (uuid4().hex + suffix)
            try:
                result = await durable_io(
                    workspace_operation, root, destination, operation, path, allow_large
                )
            except FileNotFoundError:
                destination.unlink(missing_ok=True)
                raise HTTPException(404, "Workspace entry not found") from None
            except (OSError, ValueError) as error:
                destination.unlink(missing_ok=True)
                message = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "Workspace entry is inaccessible"
                )
                raise HTTPException(413 if "preview limit" in message else 400, message) from None
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        else:
            from tokendrain.vm.firecracker import FirecrackerBackend

            try:
                result = await FirecrackerBackend(services.settings.helper_socket)._request(
                    "workspace_export",
                    {
                        "project_id": project_id,
                        "operation": operation,
                        "path": path,
                        "allow_large": allow_large,
                    },
                )
            except (RuntimeError, TimeoutError, OSError):
                raise HTTPException(
                    503, "Workspace could not be opened. Check the tokendrain-helper logs."
                ) from None
            if "error" in result:
                raise HTTPException(result.get("status", 400), result["error"])
            export_id = UUID(result["export_id"]).hex
            destination = services.settings.state_dir / "exports" / (export_id + suffix)
    return destination, result


@router.get(API + "/projects/{project_id}/workspace/tree")
async def workspace_tree(request: Request, project_id: str, path: str = "") -> Any:
    destination, _ = await workspace_result(request, project_id, "tree", path)
    try:
        return json.loads(destination.read_text(encoding="utf-8"))
    finally:
        destination.unlink(missing_ok=True)


@router.get(API + "/projects/{project_id}/workspace/file")
@router.get(API + "/projects/{project_id}/workspace/download")
async def workspace_file(
    request: Request, project_id: str, path: str, allow_large: bool = False
) -> Response:
    operation = "download" if request.url.path.endswith("/download") else "file"
    destination, result = await workspace_result(request, project_id, operation, path, allow_large)
    # Never serve untrusted HTML/SVG as an executable same-origin document.
    images = {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
    }
    media_type = (
        result["mime_type"]
        if operation == "file" and result["mime_type"] in images
        else "application/octet-stream"
        if operation == "download"
        else "text/plain"
    )
    return FileResponse(
        destination,
        media_type=media_type,
        filename=path.rsplit("/", 1)[-1],
        background=BackgroundTask(destination.unlink, missing_ok=True),
    )


@router.get(API + "/projects/{project_id}/workspace/archive")
async def workspace_archive(request: Request, project_id: str, path: str = "") -> Response:
    destination, _ = await workspace_result(request, project_id, "archive", path)
    name = path.rsplit("/", 1)[-1] if path else f"tokendrain-{UUID(project_id).hex[:8]}-workspace"
    return FileResponse(
        destination,
        media_type="application/zip",
        filename=name + ".zip",
        background=BackgroundTask(destination.unlink, missing_ok=True),
    )


@router.get(API + "/projects/{project_id}/tasks")
async def project_tasks(request: Request, project_id: str) -> Any:
    return encoded(await current(request).projects.tasks(project_id))


@router.post(API + "/projects/{project_id}/tasks")
async def mutate_tasks(request: Request, project_id: str, body: list[TaskMutation]) -> Any:
    if len(body) > 500:
        raise ValueError("Too many task mutations")
    return encoded(await current(request).projects.mutate_tasks(project_id, body))


@router.delete(API + "/projects/{project_id}/tasks/{task_id}", status_code=204)
async def delete_task(request: Request, project_id: str, task_id: str) -> Response:
    await current(request).projects.delete_task(project_id, task_id)
    return Response(status_code=204)


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
    await reconcile_saved(services)
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
        "virtual_size_bytes": info.virtual_size_bytes,
        "allocated_bytes": info.allocated_bytes,
    }


@router.post(API + "/projects/{project_id}/storage/resize")
async def resize(request: Request, project_id: str, body: ResizeInput) -> Any:
    services = current(request)
    ready(services)
    await services.projects.require_idle(project_id)
    async with services.storage.lease(project_id):
        await services.projects.require_idle(project_id)
        await services.storage.resize(project_id, body.size_gib)
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
            "is_default": value["is_default"],
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
    body.run_template.configured_agent = services.settings.active_agent
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
    if "run_template" in body:
        values.run_template.configured_agent = services.settings.active_agent
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


@router.get(API + "/automations")
async def automations(request: Request) -> Any:
    return encoded(await current(request).automations.list_automations())


@router.get(API + "/automations/{automation_id}")
async def automation(request: Request, automation_id: str) -> Any:
    return encoded(await current(request).automations.get(automation_id))


@router.post(API + "/automations", status_code=201)
async def create_automation(request: Request, body: AutomationInput) -> Any:
    return encoded(await current(request).automations.save(body))


@router.patch(API + "/automations/{automation_id}")
async def update_automation(request: Request, automation_id: str, body: dict[str, Any]) -> Any:
    return encoded(await current(request).automations.save(body, automation_id))


@router.delete(API + "/automations/{automation_id}", status_code=204)
async def delete_automation(request: Request, automation_id: str) -> Response:
    await current(request).automations.delete(automation_id)
    return Response(status_code=204)


@router.get(API + "/automation-occurrences")
async def automation_occurrences(request: Request, automation_id: str | None = None) -> Any:
    return encoded(await current(request).automations.occurrences(automation_id))


@router.get(API + "/automation-occurrences/{occurrence_id}")
async def automation_occurrence(request: Request, occurrence_id: str) -> Any:
    return encoded(await current(request).automations.occurrence(occurrence_id))


@router.post(API + "/automation-occurrences/{occurrence_id}/authorize")
async def authorize_automation(request: Request, occurrence_id: str) -> Any:
    services = current(request)
    ready(services)
    return encoded(await services.automations.authorize(occurrence_id))


@router.post(API + "/automation-occurrences/{occurrence_id}/dismiss")
async def dismiss_automation(request: Request, occurrence_id: str) -> Any:
    return encoded(await current(request).automations.dismiss(occurrence_id))


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
    credential_error = None
    credential_reauth = False
    if account:
        try:
            await services.auth.runtime_credentials(account.id)
        except (RpcError, ValueError, OSError, TimeoutError, httpx.HTTPError) as exc:
            credential_reauth = requires_reauthentication(exc)
            credential_error = (
                "Codex credentials could not be renewed. Sign in again and replace auth.json."
                if credential_reauth
                else "Codex credentials could not be renewed. Check connectivity and retry."
            )
    probe = services.probe_cache if account else None
    return {
        "connected": account is not None,
        "valid": account is not None and credential_error is None,
        "credential_error": credential_error,
        "method": "import" if account else None,
        "account_label": (account.email or account.subject) if account else None,
        "usage_error": (probe or {}).get("usage_error"),
        "connection_ok": False
        if credential_error
        else not bool(probe.get("usage_error"))
        if probe
        else None,
        "connection_checked_at": probe.get("observed_at") if probe else None,
        "reauth_required": bool(credential_reauth or (probe or {}).get("reauth_required")),
    }


@router.get(API + "/agent")
async def active_agent(request: Request) -> Any:
    services = current(request)
    name = services.settings.active_agent
    status = (
        await services.claude.status() if name == "claude_code" else await openai_status(request)
    )
    return {**status, "name": name, "label": AGENT_NAMES[name]}


@router.put(API + "/agent")
async def set_agent(request: Request, body: AgentInput) -> Any:
    await switch_agent(current(request), body.name)
    return await active_agent(request)


@router.get(API + "/agent/models")
async def agent_models(request: Request) -> Any:
    return (await account_probe(current(request))).get("models", [])


@router.get(API + "/auth/claude")
async def claude_status(request: Request) -> Any:
    return await current(request).claude.status()


@router.post(API + "/auth/claude/login", status_code=201)
async def start_claude_login(request: Request) -> Any:
    services = current(request)
    await no_active(services)
    return await services.claude.start_login()


@router.get(API + "/auth/claude/login/{login_id}")
async def claude_login_status(request: Request, login_id: str) -> Any:
    return current(request).claude.login_status(login_id)


@router.post(API + "/auth/claude/login/{login_id}/code")
async def claude_login_code(request: Request, login_id: str, body: ClaudeLoginCode) -> Any:
    await current(request).claude.code(login_id, body.code.get_secret_value())
    return current(request).claude.login_status(login_id)


@router.delete(API + "/auth/claude/login/{login_id}", status_code=204)
async def cancel_claude_login(request: Request, login_id: str) -> Response:
    services = current(request)
    services.claude.login_status(login_id)
    await services.claude.cancel_login()
    return Response(status_code=204)


@router.post(API + "/auth/claude/check")
async def check_claude(request: Request) -> Any:
    services = current(request)
    await services.claude.windows(force=True)
    await services.events.publish("claude.checked")
    return await services.claude.status()


@router.delete(API + "/auth/claude", status_code=204)
@provider_change
async def disconnect_claude(request: Request) -> Response:
    services = current(request)
    await services.claude.disconnect()
    if services.settings.active_agent == "claude_code":
        await reset_account(services, None)
    await services.events.publish("claude.disconnected")
    return Response(status_code=204)


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
    if services.settings.active_agent == "codex":
        await reset_account(services, account.id)
    if services.settings.active_agent == "codex":
        await account_probe(services, force=True)
    await services.events.publish("openai.connected")
    return await openai_status(request)


@router.post(API + "/auth/openai/check")
async def check_openai(request: Request) -> Any:
    services = current(request)
    if not any(account.connected for account in await services.auth.accounts()):
        raise ValueError("Import Codex auth.json before checking the connection.")
    await account_probe(services, force=True)
    await services.events.publish("openai.checked")
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
    if services.settings.active_agent == "codex":
        await reset_account(services, None)
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
        inventory = await db.get(Setting, "github_repositories")
        policies = (await db.scalars(select(GitHubPolicy))).all()
        return {
            "configured": app is not None,
            "name": app.slug if app else None,
            "repository_count": len(inventory.value.get("repositories", [])) if inventory else 0,
            "installation_url": f"https://github.com/apps/{app.slug}/installations/new"
            if app
            else None,
            "policy_errors": [row.error for row in policies if row.error],
        }


async def github_app(services: Application) -> GitHubApp:
    async with services.sessions() as db:
        app = await db.get(GitHubApp, 1)
        if app is None:
            raise ValueError("Connect GitHub first")
        return app


@router.post(API + "/integrations/github/connect")
async def connect_github(request: Request) -> Response:
    services = current(request)
    await no_active(services)
    state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    manifest = app_manifest(services.settings.public_url, "tokendrain-" + uuid4().hex[:8])
    async with services.sessions.begin() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        previous = await db.get(Setting, "github_manifest")
        if previous:
            await db.delete(previous)
            await db.flush()
        db.add(
            Setting(
                key="github_manifest",
                value={
                    "state_hash": hashlib.sha256(state.encode()).hexdigest(),
                    "browser_hash": hashlib.sha256(browser.encode()).hexdigest(),
                    "expires_at": time.time() + 600,
                },
            )
        )
    response = JSONResponse(
        {
            "manifest": manifest,
            "action": "https://github.com/settings/apps/new?state=" + state,
            "state": state,
        }
    )
    response.set_cookie(
        "tokendrain_github_manifest",
        browser,
        httponly=True,
        samesite="lax",
        secure=services.settings.public_url.startswith("https://"),
        max_age=600,
        path=API + "/integrations/github",
    )
    return response


@router.get(API + "/integrations/github/callback")
async def github_callback(request: Request, code: str = "", state: str = "") -> Response:
    services = current(request)
    browser = request.cookies.get("tokendrain_github_manifest", "")
    async with services.sessions.begin() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        pending = await db.get(Setting, "github_manifest")
        if (
            not code
            or len(code) > 200
            or not state
            or not browser
            or pending is None
            or pending.value["expires_at"] <= time.time()
            or not secrets.compare_digest(
                pending.value["state_hash"], hashlib.sha256(state.encode()).hexdigest()
            )
            or not secrets.compare_digest(
                pending.value["browser_hash"], hashlib.sha256(browser.encode()).hexdigest()
            )
        ):
            raise HTTPException(
                400, "GitHub connection expired or could not be verified. Connect GitHub again."
            )
        # Consume before exchange, including failures and concurrent callback replays.
        await db.delete(pending)
    async with services.runs.credentials_change():
        info = await services.github.convert_manifest(code)
        ref = f"github-key-{uuid4().hex}"
        await services.credentials.put(ref, str(info["pem"]).encode())
        try:
            # Old App authority is still available here. Never orphan its managed rules.
            await reconcile_all(services.sessions, services.github, remove_owned=True)
            async with services.sessions.begin() as db:
                old = await db.get(GitHubApp, 1)
                old_ref = old.credential_ref if old else None
                if old:
                    old.app_id, old.slug, old.credential_ref = str(info["id"]), info["slug"], ref
                else:
                    db.add(
                        GitHubApp(
                            id=1, app_id=str(info["id"]), slug=info["slug"], credential_ref=ref
                        )
                    )
                await db.execute(delete(GitHubInstallation))
                await db.execute(delete(Setting).where(Setting.key == "github_repositories"))
        except BaseException:
            async with services.sessions() as db:
                saved = await db.get(GitHubApp, 1)
                referenced = saved is not None and saved.credential_ref == ref
            if not referenced:
                await services.credentials.delete(ref)
            raise
        if old_ref:
            await services.credentials.delete(old_ref)
        services.github.clear_cache()
    # OAuth/client/webhook secrets intentionally discarded; only the PEM is encrypted.
    response = RedirectResponse(
        f"https://github.com/apps/{info['slug']}/installations/new", status_code=303
    )
    response.delete_cookie("tokendrain_github_manifest", path=API + "/integrations/github")
    return response


@router.delete(API + "/integrations/github", status_code=204)
@provider_change
async def disconnect_github(request: Request) -> Response:
    services = current(request)
    app = await github_app(services)
    await reconcile_all(services.sessions, services.github, remove_owned=True)
    async with services.sessions.begin() as db:
        await db.execute(delete(ProjectGitHub))
        await db.execute(delete(GitHubInstallation))
        await db.execute(delete(GitHubApp))
        await db.execute(
            delete(Setting).where(Setting.key.in_(["github_repositories", "github_manifest"]))
        )
    await services.credentials.delete(app.credential_ref)
    services.github.clear_cache()
    await services.events.publish("github.disconnected")
    return Response(status_code=204)


@router.post(API + "/integrations/github/sync")
@provider_change
async def sync_github(request: Request) -> Any:
    services = current(request)
    app = await github_app(services)
    values = await services.github.installations(app.app_id, app.credential_ref)
    inventory: list[dict[str, Any]] = []
    for installation in values:
        repos = await services.github.repositories(
            app.app_id, app.credential_ref, installation["id"]
        )
        inventory.extend({**repo, "installation_id": installation["id"]} for repo in repos)
    async with services.sessions.begin() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        await db.execute(delete(GitHubInstallation))
        db.add_all([GitHubInstallation(**value) for value in values])
        setting = await db.get(Setting, "github_repositories")
        if setting:
            setting.value = {"repositories": inventory}
        else:
            db.add(Setting(key="github_repositories", value={"repositories": inventory}))
        bindings = (await db.scalars(select(ProjectGitHub))).all()
        for binding in bindings:
            repo = next((repo for repo in inventory if repo["id"] == binding.repository_id), None)
            if repo:
                binding.installation_id, binding.repository_name = (
                    repo["installation_id"],
                    repo["full_name"],
                )
                await reserve_pr_policy(db, binding)
    services.github.clear_cache()
    try:
        await reconcile_all(services.sessions, services.github)
    except ValueError:
        # Persisted policy errors are visible in settings and repository selection.
        pass
    await services.events.publish("github.synced")
    return await github_status(request)


@router.get(API + "/integrations/github/setup")
async def github_setup(request: Request) -> Response:
    # Query parameters are hints only. Strict session cookies are absent on this return.
    # The authenticated Settings page automatically POSTs the authoritative API sync.
    return RedirectResponse("/settings?github=installed", status_code=303)


@router.get(API + "/integrations/github/repositories")
async def repositories(request: Request, exclude_project: str | None = None) -> Any:
    services = current(request)
    async with services.sessions() as db:
        inventory = await db.get(Setting, "github_repositories")
        bindings = (await db.execute(select(ProjectGitHub, Project).join(Project))).all()
        policies = {
            row.repository_id: row for row in (await db.scalars(select(GitHubPolicy))).all()
        }
        output = []
        for repo in inventory.value["repositories"] if inventory else []:
            uses = [
                {
                    "project_id": project.id,
                    "project_name": project.name,
                    "access_mode": binding.access_mode,
                }
                for binding, project in bindings
                if binding.repository_id == repo["id"] and project.id != exclude_project
            ]
            policy = policies.get(repo["id"])
            output.append(
                {key: value for key, value in repo.items() if key != "installation_id"}
                | {"used_by": uses, "policy_error": policy.error if policy else None}
            )
        return output


@router.get(API + "/projects/{project_id}/github")
async def project_github(request: Request, project_id: str) -> Any:
    services = current(request)
    await services.projects.get(project_id)
    async with services.sessions() as db:
        row = await db.get(ProjectGitHub, project_id)
        policy = await db.get(GitHubPolicy, row.repository_id) if row else None
        return (
            {
                key: getattr(row, key)
                for key in ("repository_id", "repository_name", "access_mode", "allow_workflows")
            }
            | {"policy_error": policy.error if policy else None}
            if row
            else None
        )


async def resolve_github_repository(request: Request, body: RepositoryInput) -> IntegrationInput:
    services = current(request)
    app = await github_app(services)
    async with services.sessions() as db:
        inventory = await db.get(Setting, "github_repositories")
        repo = (
            next(
                (
                    repo
                    for repo in inventory.value["repositories"]
                    if repo["id"] == body.repository_id
                ),
                None,
            )
            if inventory
            else None
        )
    if repo is None:
        raise ValueError("Repository is not available. Manage repositories on GitHub and retry.")
    # Verify cached selection using authenticated App API before saving the binding.
    available = await services.github.repositories(
        app.app_id, app.credential_ref, repo["installation_id"]
    )
    verified = next((item for item in available if item["id"] == body.repository_id), None)
    if verified is None:
        raise ValueError("Repository access changed. Refresh repositories and retry.")
    return IntegrationInput(
        **body.model_dump(),
        installation_id=repo["installation_id"],
        repository_name=verified["full_name"],
    )


async def reconcile_saved(services: Application) -> None:
    # Configuration is desired state. A GitHub outage cannot roll it back or grant writes.
    try:
        await reconcile_all(services.sessions, services.github)
    except ValueError:
        pass


@router.put(API + "/projects/{project_id}/github")
async def attach_github(request: Request, project_id: str, body: RepositoryInput) -> Any:
    services = current(request)
    await services.projects.require_idle(project_id)
    app_revision = await github_app(services)
    binding = await resolve_github_repository(request, body)
    async with services.sessions.begin() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        await validate_github_access(db, binding, app_revision.credential_ref, project_id)
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
        row = await db.get(ProjectGitHub, project_id)
        if row:
            for key, value in binding.binding().items():
                setattr(row, key, value)
        else:
            db.add(ProjectGitHub(project_id=project_id, **binding.binding()))
        await reserve_pr_policy(db, binding)
    await reconcile_saved(services)
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
    await reconcile_saved(services)
    await services.events.publish("project.github_updated", project_id=project_id)
    return Response(status_code=204)


@router.get(API + "/system")
async def system(request: Request) -> Any:
    services = current(request)
    settings = services.settings
    checks = [
        check.model_dump()
        for check in await inspect_service_checks(settings, services.supervisor.vm)
    ]
    checks.append(
        {
            "name": "reconciliation",
            "scope": "daemon",
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


@router.get(API + "/notifications/ntfy")
async def ntfy_status(request: Request) -> Any:
    return await current(request).notifications.status()


@router.put(API + "/notifications/ntfy")
async def ntfy_configure(request: Request, body: NtfyInput) -> Any:
    notifications = current(request).notifications
    await notifications.configure(body)
    return await notifications.status()


@router.post(API + "/notifications/ntfy/test")
async def ntfy_test(request: Request) -> Any:
    await current(request).notifications.test()
    return {"sent": True}
