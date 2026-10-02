import asyncio
import json
import logging
import time
from datetime import UTC
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.config import Settings
from tokendrain.credentials.store import CredentialStore, SecretRedactor
from tokendrain.db.models import (
    GitHubApp,
    Project,
    ProjectExecution,
    ProjectGitHub,
    Report,
    Run,
    SecretEntry,
)
from tokendrain.domain import (
    TERMINAL,
    ExecutionState,
    ReportUsage,
    RunReport,
    StopCondition,
    UsageStop,
    UsageWindow,
    stop_reason,
    utcnow,
)
from tokendrain.events import EventBus
from tokendrain.github.provider import GitHubProvider, InstallationToken, IntegrationInput
from tokendrain.orchestration.driver import ProviderLimited, SessionFactory, WorkSession
from tokendrain.services import ProjectService, RunService
from tokendrain.storage.files import ProjectStorage
from tokendrain.vm.models import VmBackend, VmHandle, VmSpec

log = logging.getLogger(__name__)


class BudgetReached(Exception):
    pass


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        runs: RunService,
        projects: ProjectService,
        events: EventBus,
        storage: ProjectStorage,
        vm: VmBackend,
        factory: SessionFactory,
        credentials: CredentialStore,
        github: GitHubProvider,
    ) -> None:
        self.settings, self.sessions, self.runs, self.projects = settings, sessions, runs, projects
        self.events, self.storage, self.vm, self.factory = events, storage, vm, factory
        self.credentials, self.github = credentials, github
        self.active: dict[str, tuple[asyncio.Task[None], asyncio.Event, str]] = {}
        self.ready = False
        self.recovery_error: str | None = None

    async def reconcile(self) -> None:
        # Fail closed: never release a project DB reservation until its VM was stopped.
        handles = await self.vm.reconcile()
        for handle in handles:
            await self.vm.stop(handle)
        async with self.sessions.begin() as db:
            executions = list(
                (
                    await db.scalars(
                        select(ProjectExecution).where(
                            ProjectExecution.status.not_in([state.value for state in TERMINAL]),
                            ProjectExecution.status != "queued",
                        )
                    )
                ).all()
            )
            affected: set[str] = set()
            for execution in executions:
                execution.status = "failed"
                execution.error = (
                    "Daemon restarted during execution. "
                    "Workspace retained; inspect before resuming."
                )
                execution.finished_at = utcnow()
                saved_report = await db.get(Report, execution.id)
                recovery_report = (
                    RunReport.model_validate(saved_report.content)
                    if saved_report
                    else RunReport(summary="Daemon restarted before a final report was available.")
                )
                recovery_report.status = "failed"
                recovery_report.blockers.append(execution.error)
                recovery_report.suggested_next_action = (
                    "Inspect the persisted workspace and thread before continuing."
                )
                if saved_report:
                    saved_report.content = recovery_report.model_dump(mode="json")
                else:
                    db.add(
                        Report(
                            execution_id=execution.id,
                            content=recovery_report.model_dump(mode="json"),
                        )
                    )
                project = await db.get(Project, execution.project_id)
                if project:
                    project.status = "failed"
                affected.add(execution.run_id)
        for run_id in affected:
            await self.finish_run(run_id)
        await self.events.publish("system.reconciled", f"Stopped {len(handles)} orphan VM(s).")
        self.ready = True
        self.recovery_error = None

    async def finish_run(self, run_id: str) -> None:
        async with self.sessions.begin() as db:
            run = await db.get(Run, run_id)
            if run is None:
                return
            states = list(
                (
                    await db.scalars(
                        select(ProjectExecution.status).where(ProjectExecution.run_id == run_id)
                    )
                ).all()
            )
            if not states or any(state not in {s.value for s in TERMINAL} for state in states):
                return
            run.status = next(
                (state for state in ("failed", "cancelled", "blocked") if state in states),
                "completed",
            )
            run.finished_at = utcnow()
        await self.events.publish("run.finished", run_id=run_id)

    async def serve(self) -> None:
        while not self.ready:
            try:
                await self.reconcile()
            except Exception as error:
                self.recovery_error = f"{type(error).__name__}: {error}"
                log.error("reconciliation_failed", extra={"reason": self.recovery_error})
                await asyncio.sleep(10)
        async with asyncio.TaskGroup() as group:
            while True:
                for execution_id, (task, _, _) in list(self.active.items()):
                    if task.done():
                        # Tasks handle their domain failures; this propagates programming errors.
                        task.result()
                        del self.active[execution_id]
                async with self.sessions() as db:
                    cancelling = set(
                        (
                            await db.scalars(select(Run.id).where(Run.cancel_requested.is_(True)))
                        ).all()
                    )
                    queued = list(
                        (
                            await db.execute(
                                select(ProjectExecution.id, ProjectExecution.run_id, Run.parallel)
                                .join(Run)
                                .where(ProjectExecution.status == "queued")
                                .order_by(Run.created_at)
                            )
                        ).all()
                    )
                for _, cancel, run_id in self.active.values():
                    if run_id in cancelling:
                        cancel.set()
                for execution_id, run_id, parallel in queued:
                    if execution_id in self.active:
                        continue
                    if run_id in cancelling:
                        await self.runs.transition(execution_id, ExecutionState.CANCELLED)
                        await self.finish_run(run_id)
                        continue
                    if len(self.active) >= self.settings.max_concurrency:
                        break
                    if not parallel and any(value[2] == run_id for value in self.active.values()):
                        continue
                    cancel = asyncio.Event()
                    task = group.create_task(
                        self.execute(execution_id, cancel), name=f"execution-{execution_id}"
                    )
                    self.active[execution_id] = (task, cancel, run_id)
                await asyncio.sleep(0.5)

    async def execute(self, execution_id: str, cancel: asyncio.Event) -> None:
        async with self.sessions.begin() as db:
            execution = await db.get(ProjectExecution, execution_id)
            assert execution is not None
            project = await db.get(Project, execution.project_id)
            run = await db.get(Run, execution.run_id)
            assert project is not None and run is not None
            run.status, run.started_at = "running", run.started_at or utcnow()
            run_started = (
                run.started_at.replace(tzinfo=UTC)
                if run.started_at.tzinfo is None
                else run.started_at
            )
            elapsed_before = max(0.0, (utcnow() - run_started).total_seconds())
            clock_started = time.monotonic()
            project_id, run_id = project.id, run.id
            policy = TypeAdapter(list[StopCondition]).validate_python(run.stop_conditions)
            previous_report = await db.scalar(
                select(Report.content)
                .join(ProjectExecution)
                .where(ProjectExecution.project_id == project_id)
                .order_by(Report.created_at.desc())
                .limit(1)
            )
            integration = await db.get(ProjectGitHub, project_id)
            app = await db.get(GitHubApp, 1)
            secrets_rows = list(
                (
                    await db.scalars(
                        select(SecretEntry).where(SecretEntry.project_id == project_id)
                    )
                ).all()
            )
        session: WorkSession | None = None
        handle: VmHandle | None = None
        start_attempted = False
        final = ExecutionState.COMPLETED
        error_text: str | None = None
        report = RunReport(summary="Execution stopped before a report was produced.")
        windows: list[UsageWindow] = []
        starting_windows: list[UsageWindow] = []
        original_task_log = project.task_log
        runtime_values: dict[str, str] = {}
        redactor = SecretRedactor()

        def elapsed() -> float:
            return elapsed_before + time.monotonic() - clock_started

        async def emit(message: str) -> None:
            await self.events.publish(
                "execution.log",
                message,
                run_id=run_id,
                project_id=project_id,
                execution_id=execution_id,
            )

        async def observe(value: list[UsageWindow]) -> None:
            nonlocal windows
            windows = value
            await self.runs.observe(value, execution_id)

        async def github_token() -> InstallationToken | None:
            if integration and app:
                return await self.github.token(
                    app.app_id,
                    app.credential_ref,
                    IntegrationInput.model_validate(
                        {
                            key: getattr(integration, key)
                            for key in (
                                "installation_id",
                                "repository_id",
                                "repository_name",
                                "permissions",
                            )
                        }
                    ),
                )
            return None

        try:
            async with self.storage.lease(project_id):
                try:
                    await self.runs.transition(execution_id, ExecutionState.PREPARING)
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    if reason := stop_reason(policy, [], elapsed()):
                        raise BudgetReached(reason)
                    await self.projects.snapshot(project_id, f"Before run {run_id[:8]}")
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    for secret in secrets_rows:
                        value = await self.credentials.get(secret.credential_ref)
                        if value is None:
                            raise ValueError(
                                f"Secret {secret.name} is missing from encrypted storage"
                            )
                        runtime_values[secret.name] = value.decode()
                    redactor.replace_values(list(runtime_values.values()))
                    if reason := stop_reason(policy, [], elapsed()):
                        raise BudgetReached(reason)
                    await self.runs.transition(execution_id, ExecutionState.STARTING_VM)
                    start_attempted = True
                    handle = await self.vm.start(
                        VmSpec(
                            execution_id=execution_id,
                            project_id=project_id,
                            vcpus=self.settings.default_vcpus,
                            memory_mib=self.settings.default_memory_mib,
                        )
                    )
                    async with self.sessions.begin() as db:
                        live = await db.get(ProjectExecution, execution_id)
                        assert live is not None
                        live.vm_metadata = handle.model_dump(mode="json")
                    session = await self.factory.connect(
                        handle, runtime_values, github_token, emit, observe
                    )
                    redactor = session.redactor
                    thread_id = await session.initialize(project.thread_id, execution.model)
                    async with self.sessions.begin() as db:
                        live_project = await db.get(Project, project_id)
                        live = await db.get(ProjectExecution, execution_id)
                        assert live_project is not None and live is not None
                        live_project.thread_id = live.thread_id = thread_id
                    await self.runs.transition(execution_id, ExecutionState.RUNNING)
                    starting_windows = await session.usage()
                    windows = starting_windows
                    prompt = self.context(project, integration, secrets_rows, previous_report)
                    while True:
                        if cancel.is_set():
                            raise asyncio.CancelledError
                        reason = stop_reason(policy, windows, elapsed())
                        if reason:
                            await emit(reason)
                            if report.summary == "Execution stopped before a report was produced.":
                                report.summary = reason
                            break
                        requested = [rule for rule in policy if isinstance(rule, UsageStop)]
                        for rule in requested:
                            matching = [
                                w
                                for w in windows
                                if (not rule.limit_id or rule.limit_id == w.limit_id)
                                and (
                                    not rule.window_minutes
                                    or rule.window_minutes == w.window_minutes
                                )
                            ]
                            if not matching:
                                raise ValueError(
                                    "Requested usage limit cannot be observed from this "
                                    "account/provider. Use a runtime limit or reconnect."
                                )
                        report = await session.turn(
                            prompt, execution.model, execution.reasoning_effort, cancel
                        )
                        async with self.sessions.begin() as db:
                            live_project = await db.get(Project, project_id)
                            if (
                                live_project
                                and live_project.next_run_feedback == project.next_run_feedback
                            ):
                                live_project.next_run_feedback = ""
                        report.usage = ReportUsage(start=starting_windows, end=windows)
                        original_task_log = await self.save_report(
                            execution_id, project_id, report, original_task_log
                        )
                        if report.status == "completed":
                            break
                        if report.status == "blocked":
                            final = ExecutionState.BLOCKED
                            break
                        if report.status in {"failed", "cancelled"}:
                            final = ExecutionState.FAILED
                            error_text = report.summary
                            break
                        # Useful turns are the stopping boundary, never individual shell commands.
                        windows = await session.usage()
                        prompt = (
                            "Continue autonomous work towards the project description. Inspect "
                            "workspace and existing processes first; "
                            "do not replay earlier actions. "
                            "Finish a substantial useful unit, verify it, "
                            "update the durable task log, "
                            "and return the structured report. "
                            "If finished or irrecoverably blocked, "
                            "say so. Previous summary: " + report.summary
                        )
                        async with self.sessions() as db:
                            current = await db.get(Project, project_id)
                            if current:
                                prompt += "\nCurrent description: " + current.description
                                prompt += "\nCurrent task log: " + current.task_log
                except BudgetReached as error:
                    report.summary = str(error)
                    await emit(str(error))
                except ProviderLimited:
                    reason = "Provider usage limit prevents another turn; retained last report."
                    if report.summary == "Execution stopped before a report was produced.":
                        report.summary = reason
                    await emit(reason)
                except asyncio.CancelledError:
                    if cancel.is_set():
                        final = ExecutionState.CANCELLED
                        error_text = "Cancelled by user"
                    else:
                        final = ExecutionState.FAILED
                        error_text = (
                            "Daemon stopped during execution; state retained for inspection"
                        )
                except Exception as error:
                    final = ExecutionState.FAILED
                    error_text = redactor.redact(f"{type(error).__name__}: {error}")
                    await emit(error_text)
                finally:
                    async with self.sessions() as db:
                        current_execution = await db.get(ProjectExecution, execution_id)
                        assert current_execution
                        current_state = ExecutionState(current_execution.status)
                    if current_state != ExecutionState.QUEUED:
                        await self.runs.transition(execution_id, ExecutionState.STOPPING)
                    if session:
                        try:
                            await session.close()
                        except Exception as error:
                            final = ExecutionState.FAILED
                            error_text = f"Guest shutdown failed: {type(error).__name__}"
                            await emit(error_text)
                    if start_attempted and handle is None:
                        # A failed start may have created host resources before
                        # returning its handle. Reconcile only this execution.
                        for orphan in await self.vm.reconcile():
                            if UUID(orphan.execution_id) == UUID(execution_id):
                                await self.vm.stop(orphan)
                    if handle:
                        # A failed host stop MUST keep the project reservation active.
                        await self.vm.stop(handle)
                    report.usage = ReportUsage(start=starting_windows, end=windows)
                    if final in {ExecutionState.FAILED, ExecutionState.CANCELLED}:
                        report.status = "failed" if final == ExecutionState.FAILED else "cancelled"
                        report.blockers.append(error_text or "Infrastructure interrupted execution")
                    await self.save_report(execution_id, project_id, report, original_task_log)
                    if current_state == ExecutionState.QUEUED:
                        final = ExecutionState.FAILED
                    await self.runs.transition(execution_id, final, error_text)
        except asyncio.CancelledError:
            # Cancellation while waiting for the lease never attached disks.
            async with self.sessions() as db:
                live = await db.get(ProjectExecution, execution_id)
                state = ExecutionState(live.status) if live else None
            if state == ExecutionState.QUEUED:
                await self.runs.transition(
                    execution_id,
                    ExecutionState.CANCELLED if cancel.is_set() else ExecutionState.FAILED,
                    "Stopped before preparation",
                )
            else:
                raise
        except Exception as error:
            # Unconfirmed teardown is visible and reserved. Startup reconciliation retries it.
            await emit(
                redactor.redact(f"Resource cleanup/lease failed: {type(error).__name__}: {error}")
            )
            async with self.sessions.begin() as db:
                live = await db.get(ProjectExecution, execution_id)
                if live:
                    live.error = (
                        "Unconfirmed cleanup. Project remains reserved; "
                        "restart daemon to reconcile."
                    )
                    queued_failure = live.status == "queued" and not start_attempted
                else:
                    queued_failure = False
            if queued_failure:
                await self.runs.transition(
                    execution_id,
                    ExecutionState.FAILED,
                    redactor.redact(f"Preparation failed: {type(error).__name__}: {error}"),
                )
        finally:
            runtime_values.clear()
            await self.finish_run(run_id)

    async def save_report(
        self, execution_id: str, project_id: str, report: RunReport, expected_task_log: str
    ) -> str:
        async with self.sessions.begin() as db:
            row = await db.get(Report, execution_id)
            if row:
                row.content = report.model_dump(mode="json")
                row.created_at = utcnow()
            else:
                db.add(Report(execution_id=execution_id, content=report.model_dump(mode="json")))
            project = await db.get(Project, project_id)
            if project and report.task_log and project.task_log == expected_task_log:
                project.task_log = report.task_log
                expected_task_log = report.task_log
        return expected_task_log

    @staticmethod
    def context(
        project: Project,
        integration: ProjectGitHub | None,
        secrets: list[SecretEntry],
        previous_report: dict[str, Any] | None = None,
    ) -> str:
        context: dict[str, Any] = {
            "project": project.name,
            "description": project.description,
            "task_log": project.task_log,
            "feedback": project.next_run_feedback,
            "workspace": "/workspace",
            "persistent_environment": "/persist",
            "previous_run": previous_report,
            "secrets": [{"name": secret.name, "purpose": secret.description} for secret in secrets],
        }
        if integration:
            context["github"] = {
                "repository": integration.repository_name,
                "permissions": integration.permissions,
            }
        return (
            "You are the autonomous worker for a persistent software project. You have root "
            "inside an isolated disposable VM. Inspect the workspace and installed environment "
            "before acting. Make substantial useful progress, run appropriate checks, and continue "
            "without human approvals. Install development tools as needed; "
            "home and Nix state persist. "
            "Use /workspace for source. Secrets are runtime environment variables; "
            "never copy or log "
            "their values into persistent files. Use GitHub only within the listed permissions. "
            "Keep TASK_LOG.md in /workspace updated, and include its current text in task_log in "
            "your structured report. Return an honest report after a meaningful work unit, marking "
            "completed only when the project objective is achieved, blocked only when you cannot "
            "make useful progress. Do not ask for approvals.\n" + json.dumps(context)
        )
