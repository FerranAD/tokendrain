"""Application services, transaction boundaries and resource ownership."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.db.models import (
    Event,
    Project,
    ProjectExecution,
    ProjectSnapshot,
    ProjectTask,
    Report,
    Run,
    Setting,
    UsageSnapshot,
    new_id,
)
from tokendrain.domain import (
    TERMINAL,
    ExecutionState,
    ProjectCreate,
    ProjectPatch,
    RunReport,
    RunTemplate,
    TaskMutation,
    UsageWindow,
    utcnow,
    validate_transition,
)
from tokendrain.events import EventBus
from tokendrain.storage.files import ProjectStorage


def columns(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


async def valid_checkpoint(
    db: AsyncSession, execution: ProjectExecution
) -> tuple[dict[str, Any] | None, str | None]:
    """Reports are model-owned. Recover historical final items from the old log format."""
    import json

    from tokendrain.orchestration.driver import parse_report

    row = await db.get(Report, execution.id)
    if row:
        content = {
            key: value for key, value in row.content.items() if key in RunReport.model_fields
        }
        summary = str(content.get("summary", ""))
        # Old releases fabricated host failures/cancellations or concatenated protocol JSON.
        raw_summary = summary.lstrip().startswith(("{", "```")) or (
            '"status"' in summary and '"summary"' in summary
        )
        legacy = "task_log" in row.content or raw_summary
        if not legacy:
            try:
                return RunReport.model_validate(content).model_dump(mode="json"), execution.id
            except ValueError:
                pass
    logs = await db.scalars(
        select(Event)
        .where(
            Event.execution_id == execution.id,
            Event.type.in_(["execution.log", "agent.checkpoint"]),
        )
        .order_by(Event.id.desc())
    )
    for event in logs:
        try:
            item = event.data.get("raw") or json.loads(event.message)
            if item.get("type") != "agentMessage" or item.get("phase") != "final_answer":
                continue
            raw = json.loads(item["text"])
            raw.pop("task_log", None)
            return parse_report(json.dumps(raw)).model_dump(mode="json"), execution.id
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    if row and content.get("status") not in {"cancelled", "failed"} and not raw_summary:
        try:
            return RunReport.model_validate(content).model_dump(mode="json"), execution.id
        except ValueError:
            pass
    return None, None


async def latest_checkpoint(
    db: AsyncSession, project_id: str, *, before: datetime | None = None
) -> tuple[dict[str, Any] | None, str | None]:
    query = select(ProjectExecution).join(Run).where(ProjectExecution.project_id == project_id)
    if before:
        query = query.where(Run.created_at <= before)
    executions = await db.scalars(query.order_by(Run.created_at.desc()).limit(100))
    for execution in executions:
        report, source = await valid_checkpoint(db, execution)
        if report:
            return report, source
    return None, None


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
                    **values.model_dump(exclude={"initial_tasks"}),
                    environment_metadata={"size_bytes": storage.environment_bytes},
                    workspace_metadata={"size_bytes": storage.workspace_bytes},
                )
            )
            await db.flush()
            for position, task in enumerate(values.initial_tasks):
                db.add(
                    ProjectTask(
                        project_id=project_id, position=position, origin="user", **task.model_dump()
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
            report, source = await latest_checkpoint(db, project_id)
            value["latest_report"] = report
            value["checkpoint_from_execution_id"] = source
            latest = await db.scalar(
                select(ProjectExecution)
                .join(Run)
                .where(ProjectExecution.project_id == project_id)
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            value["latest_execution"] = columns(latest) if latest else None
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

    async def snapshot(
        self, project_id: str, name: str, run_id: str | None = None
    ) -> dict[str, Any]:
        value = await self.storage.snapshot(project_id)
        row = ProjectSnapshot(
            id=value.id,
            project_id=project_id,
            name=name,
            run_id=run_id,
            created_at=value.created_at,
            environment_bytes=value.environment_bytes,
            workspace_bytes=value.workspace_bytes,
        )
        async with self.sessions.begin() as db:
            db.add(row)
        return columns(row)

    async def tasks(self, project_id: str) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            if not await db.get(Project, project_id):
                raise LookupError("Project not found")
            rows = await db.scalars(
                select(ProjectTask)
                .where(ProjectTask.project_id == project_id)
                .order_by(ProjectTask.position, ProjectTask.id)
            )
            return [columns(row) for row in rows]

    @staticmethod
    async def mutate_tasks_in(
        db: AsyncSession, project_id: str, mutations: list[TaskMutation], *, agent: bool = False
    ) -> list[str]:
        if not await db.get(Project, project_id):
            raise LookupError("Project not found")
        rows = list(
            (
                await db.scalars(
                    select(ProjectTask)
                    .where(ProjectTask.project_id == project_id)
                    .order_by(ProjectTask.position, ProjectTask.id)
                )
            ).all()
        )
        ids = []
        for mutation in mutations:
            original_position: int | None = None
            if mutation.id:
                task = next((row for row in rows if row.id == mutation.id), None)
                if task is None:
                    raise ValueError("Task no longer exists in this project; reload the board")
                target = mutation.column or task.column
                if target == task.column:
                    original_position = [row for row in rows if row.column == target].index(task)
                if agent and task.column == "backlog" and target != "backlog":
                    raise ValueError("Only users may approve or start Backlog work")
                for key in ("title", "description"):
                    value = getattr(mutation, key)
                    if value is not None:
                        setattr(task, key, value)
                rows.remove(task)
            else:
                if not mutation.title:
                    raise ValueError("New tasks require a title")
                target = "backlog" if agent else mutation.column or "todo"
                task = ProjectTask(
                    id=new_id(),
                    project_id=project_id,
                    title=mutation.title,
                    description=mutation.description or "",
                    column=target,
                    origin="agent" if agent else "user",
                )
                db.add(task)
            task.column = target
            same = [row for row in rows if row.column == target]
            requested_position = (
                mutation.position if mutation.position is not None else original_position
            )
            position = (
                len(same) if requested_position is None else min(requested_position, len(same))
            )
            if position < len(same):
                rows.insert(rows.index(same[position]), task)
            else:
                rows.append(task)
            task.updated_at = utcnow()
            ids.append(task.id)
        for column in ("backlog", "todo", "in_progress", "done"):
            for position, row in enumerate(row for row in rows if row.column == column):
                row.position = position
        return ids

    async def mutate_tasks(
        self, project_id: str, mutations: list[TaskMutation]
    ) -> list[dict[str, Any]]:
        async with self.sessions.begin() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            await self.mutate_tasks_in(db, project_id, mutations)
        await self.events.publish("project.tasks_updated", project_id=project_id)
        return await self.tasks(project_id)

    async def delete_task(self, project_id: str, task_id: str) -> None:
        async with self.sessions.begin() as db:
            task = await db.get(ProjectTask, task_id)
            if task is None or task.project_id != project_id:
                raise LookupError("Task not found")
            await db.delete(task)
        await self.events.publish("project.tasks_updated", project_id=project_id)


class RunService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], events: EventBus) -> None:
        self.sessions, self.events = sessions, events
        self._credential_owner: asyncio.Task[Any] | None = None

    @asynccontextmanager
    async def credentials_change(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is not None and self._credential_owner is task:
            yield
            return
        async with self.sessions.begin() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            if await db.get(Setting, "provider_change"):
                raise ValueError("Another credential change is in progress")
            active = await db.scalar(
                select(ProjectExecution.id)
                .where(ProjectExecution.status.not_in([state.value for state in TERMINAL]))
                .limit(1)
            )
            if active:
                raise ValueError("Cancel or finish active Runs before changing credentials")
            db.add(Setting(key="provider_change", value={"started_at": utcnow().isoformat()}))
        self._credential_owner = task
        try:
            yield
        finally:
            self._credential_owner = None
            async with self.sessions.begin() as db:
                await db.execute(delete(Setting).where(Setting.key == "provider_change"))

    async def create_run_in(
        self,
        db: AsyncSession,
        template: RunTemplate,
        *,
        schedule_id: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> str:
        if await db.get(Setting, "provider_change"):
            raise ValueError("Credential configuration is changing; retry after it completes")
        run = Run(
            id=new_id(),
            parallel=template.parallel,
            threshold_mode=template.threshold_mode,
            stop_conditions=[rule.model_dump(mode="json") for rule in template.stop_conditions],
            schedule_id=schedule_id,
            scheduled_for=scheduled_for,
        )
        pending: list[ProjectExecution] = []
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
            pending.append(
                ProjectExecution(
                    run_id=run.id,
                    project_id=project.id,
                    model=config.model or project.default_model,
                    reasoning_effort=(
                        config.reasoning_effort
                        if "reasoning_effort" in config.model_fields_set
                        else project.default_reasoning_effort
                    ),
                )
            )
        db.add(run)
        try:
            await db.flush()
            db.add_all(pending)
            await db.flush()
        except IntegrityError as error:
            raise ValueError("A project is already reserved by another Run") from error
        return run.id

    async def create(self, template: RunTemplate) -> dict[str, Any]:
        async with self.sessions.begin() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            run_id = await self.create_run_in(db, template)
        await self.events.publish("run.created", run_id=run_id)
        return await self.get(run_id)

    async def executions(
        self, *, project_id: str | None = None, run_id: str | None = None
    ) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            query = select(ProjectExecution).join(Project)
            if project_id:
                query = query.where(ProjectExecution.project_id == project_id)
            if run_id:
                query = query.where(ProjectExecution.run_id == run_id)
            rows = await db.scalars(query.order_by(ProjectExecution.started_at.desc()).limit(500))
            result = []
            names: dict[str, str] = {}
            for row in rows:
                if row.project_id not in names:
                    project = await db.get(Project, row.project_id)
                    names[row.project_id] = project.name if project else row.project_id
                name = names[row.project_id]
                report, source = await valid_checkpoint(db, row)
                if report is None:
                    owner = await db.get(Run, row.run_id)
                    report, source = await latest_checkpoint(
                        db, row.project_id, before=owner.created_at if owner else None
                    )
                result.append(
                    {
                        **columns(row),
                        "project_name": name,
                        "report": report,
                        "checkpoint_from_execution_id": source,
                    }
                )
            return result

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
            await db.execute(text("BEGIN IMMEDIATE"))
            execution = await db.get(ProjectExecution, execution_id)
            if execution is None:
                raise LookupError("Execution not found")
            current = ExecutionState(execution.status)
            # Threshold observers and teardown can request stopping concurrently.
            if state == ExecutionState.STOPPING and (current == state or current in TERMINAL):
                return
            validate_transition(current, state)
            execution.status = state.value
            if state == ExecutionState.PREPARING:
                execution.started_at = utcnow()
            if state in TERMINAL:
                if execution.termination_reason is None:
                    owner = await db.get(Run, execution.run_id)
                    execution.termination_reason = {
                        ExecutionState.COMPLETED: "project_completed",
                        ExecutionState.BLOCKED: "blocked",
                        ExecutionState.FAILED: "infrastructure_error",
                        ExecutionState.CANCELLED: "user_cancelled"
                        if owner and owner.cancel_requested
                        else "agent_cancelled",
                    }.get(state)
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
            execution = await db.get(ProjectExecution, execution_id) if execution_id else None
            run_id = execution.run_id if execution else None
            project_id = execution.project_id if execution else None
        await self.events.publish(
            "usage.updated",
            execution_id=execution_id,
            run_id=run_id,
            project_id=project_id,
            data={"windows": value},
        )

    async def latest_usage(self) -> list[UsageWindow]:
        async with self.sessions() as db:
            row = await db.scalar(select(UsageSnapshot).order_by(UsageSnapshot.id.desc()).limit(1))
            return [UsageWindow.model_validate(value) for value in row.windows] if row else []

    async def prune_events(self, before: datetime) -> None:
        from tokendrain.db.models import Event

        async with self.sessions.begin() as db:
            await db.execute(delete(Event).where(Event.timestamp < before))
