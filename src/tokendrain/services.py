"""Application services, transaction boundaries and resource ownership."""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.db.models import (
    Project,
    ProjectExecution,
    ProjectSnapshot,
    Report,
    Run,
    UsageSnapshot,
    new_id,
)
from tokendrain.domain import (
    TERMINAL,
    ExecutionState,
    ProjectCreate,
    ProjectPatch,
    RunTemplate,
    UsageWindow,
    utcnow,
    validate_transition,
)
from tokendrain.events import EventBus
from tokendrain.storage.files import ProjectStorage


def columns(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class ProjectService:
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], storage: ProjectStorage, events: EventBus
    ) -> None:
        self.sessions, self.storage, self.events = sessions, storage, events

    async def create(self, values: ProjectCreate) -> str:
        project_id = new_id()
        storage = await self.storage.create(project_id)
        async with self.sessions.begin() as db:
            db.add(
                Project(
                    id=project_id,
                    **values.model_dump(),
                    environment_metadata={"size_bytes": storage.environment_bytes},
                    workspace_metadata={"size_bytes": storage.workspace_bytes},
                )
            )
        await self.events.publish("project.created", project_id=project_id)
        return project_id

    async def get(self, project_id: str) -> dict[str, Any]:
        async with self.sessions() as db:
            project = await db.get(Project, project_id)
            if not project:
                raise LookupError("Project not found")
            value = columns(project)
            report = await db.scalar(
                select(Report)
                .join(ProjectExecution)
                .where(ProjectExecution.project_id == project_id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            value["latest_report"] = report.content if report else None
            return value

    async def list_projects(self) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            ids = list(
                (await db.scalars(select(Project.id).order_by(Project.updated_at.desc()))).all()
            )
        return [await self.get(project_id) for project_id in ids]

    async def patch(self, project_id: str, values: ProjectPatch) -> dict[str, Any]:
        async with self.sessions.begin() as db:
            project = await db.get(Project, project_id)
            if not project:
                raise LookupError("Project not found")
            for key, value in values.model_dump(exclude_unset=True, exclude_none=True).items():
                setattr(project, key, value)
            project.updated_at = utcnow()
        await self.events.publish("project.updated", project_id=project_id)
        return await self.get(project_id)

    async def require_idle(self, project_id: str) -> None:
        async with self.sessions() as db:
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

    async def snapshot(self, project_id: str, name: str) -> dict[str, Any]:
        value = await self.storage.snapshot(project_id)
        row = ProjectSnapshot(
            id=value.id,
            project_id=project_id,
            name=name,
            created_at=value.created_at,
            environment_bytes=value.environment_bytes,
            workspace_bytes=value.workspace_bytes,
        )
        async with self.sessions.begin() as db:
            db.add(row)
        return columns(row)


class RunService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], events: EventBus) -> None:
        self.sessions, self.events = sessions, events

    async def create_run_in(
        self,
        db: AsyncSession,
        template: RunTemplate,
        *,
        schedule_id: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> str:
        run = Run(
            id=new_id(),
            parallel=template.parallel,
            stop_conditions=[rule.model_dump(mode="json") for rule in template.stop_conditions],
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
        )
        for config in template.projects:
            project = await db.get(Project, config.project_id)
            if not project:
                raise ValueError(f"Project {config.project_id} no longer exists")
            active = await db.scalar(
                select(ProjectExecution.id)
                .where(
                    ProjectExecution.project_id == project.id,
                    ProjectExecution.status.not_in([state.value for state in TERMINAL]),
                )
                .limit(1)
            )
            if active:
                raise ValueError(
                    f"Project {project.name} already has an active or queued execution"
                )
            db.add(
                ProjectExecution(
                    run_id=run.id,
                    project_id=project.id,
                    model=config.model or project.default_model,
                    reasoning_effort=config.reasoning_effort,
                )
            )
        db.add(run)
        try:
            await db.flush()
        except IntegrityError as error:
            raise ValueError("A project is already reserved by another Run") from error
        return run.id

    async def create(self, template: RunTemplate) -> dict[str, Any]:
        async with self.sessions.begin() as db:
            run_id = await self.create_run_in(db, template)
        await self.events.publish("run.created", run_id=run_id)
        return await self.get(run_id)

    async def executions(
        self, *, project_id: str | None = None, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            query = (
                select(ProjectExecution, Project.name, Report.content)
                .join(Project)
                .outerjoin(Report, Report.execution_id == ProjectExecution.id)
            )
            if project_id:
                query = query.where(ProjectExecution.project_id == project_id)
            if run_id:
                query = query.where(ProjectExecution.run_id == run_id)
            rows = await db.execute(query.order_by(ProjectExecution.started_at.desc()).limit(500))
            return [
                {**columns(row), "project_name": name, "report": report}
                for row, name, report in rows
            ]

    async def get(self, run_id: str) -> dict[str, Any]:
        async with self.sessions() as db:
            row = await db.get(Run, run_id)
            if not row:
                raise LookupError("Run not found")
            result = columns(row)
        result["executions"] = await self.executions(run_id=run_id)
        return result

    async def list_runs(self) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            ids = list(
                (await db.scalars(select(Run.id).order_by(Run.created_at.desc()).limit(200))).all()
            )
        return [await self.get(run_id) for run_id in ids]

    async def transition(
        self, execution_id: str, state: ExecutionState, error: str | None = None
    ) -> None:
        async with self.sessions.begin() as db:
            execution = await db.get(ProjectExecution, execution_id)
            if execution is None:
                raise LookupError("Execution not found")
            validate_transition(ExecutionState(execution.status), state)
            execution.status = state.value
            if state == ExecutionState.PREPARING:
                execution.started_at = utcnow()
            if state in TERMINAL:
                execution.finished_at = utcnow()
                execution.error = error
            project = await db.get(Project, execution.project_id)
            if project:
                project.status = state.value
                if state == ExecutionState.PREPARING:
                    project.last_run_at = utcnow()
            run_id, project_id = execution.run_id, execution.project_id
        await self.events.publish(
            "execution.state",
            state.value,
            run_id=run_id,
            execution_id=execution_id,
            project_id=project_id,
        )

    async def cancel(self, run_id: str) -> None:
        async with self.sessions.begin() as db:
            run = await db.get(Run, run_id)
            if run is None:
                raise LookupError("Run not found")
            if run.status in {state.value for state in TERMINAL}:
                return
            run.cancel_requested = True
            run.status = "stopping"
        await self.events.publish("run.cancelling", run_id=run_id)

    async def observe(self, windows: list[UsageWindow], execution_id: str | None = None) -> None:
        value = [window.model_dump(mode="json") for window in windows]
        async with self.sessions.begin() as db:
            db.add(UsageSnapshot(execution_id=execution_id, windows=value))
        await self.events.publish(
            "usage.updated", execution_id=execution_id, data={"windows": value}
        )

    async def latest_usage(self) -> list[UsageWindow]:
        async with self.sessions() as db:
            row = await db.scalar(select(UsageSnapshot).order_by(UsageSnapshot.id.desc()).limit(1))
            return [UsageWindow.model_validate(value) for value in row.windows] if row else []

    async def prune_events(self, before: datetime) -> None:
        from tokendrain.db.models import Event

        async with self.sessions.begin() as db:
            await db.execute(delete(Event).where(Event.timestamp < before))
