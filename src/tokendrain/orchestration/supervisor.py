import asyncio
import json
import logging
import time
from datetime import UTC
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from pydantic_core import to_jsonable_python
from sqlalchemy import select, text
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
    DeadlineStop,
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
from tokendrain.github.policy import reconcile_repository
from tokendrain.github.provider import (
    MODE_GUIDANCE,
    GitHubProvider,
    InstallationToken,
    IntegrationInput,
)
from tokendrain.orchestration.driver import (
    ProviderLimited,
    ResumeRequired,
    SessionFactory,
    WorkSession,
)
from tokendrain.orchestration.failures import execution_failure
from tokendrain.services import ProjectService, RunService, latest_checkpoint
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
                execution.termination_reason = "infrastructure_error"
                execution.interrupted = True
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
                (
                    state
                    for state in ("failed", "cancelled", "blocked", "stopped")
                    if state in states
                ),
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
                                select(ProjectExecution, Run)
                                .join(Run)
                                .where(ProjectExecution.status == "queued")
                                .order_by(Run.created_at)
                            )
                        ).all()
                    )
                for _, cancel, run_id in self.active.values():
                    if run_id in cancelling:
                        cancel.set()
                for queued_execution, queued_run in queued:
                    execution_id, run_id = queued_execution.id, queued_run.id
                    parallel, conditions = queued_run.parallel, queued_run.stop_conditions
                    if execution_id in self.active:
                        continue
                    if run_id in cancelling:
                        await self.runs.transition(execution_id, ExecutionState.CANCELLED)
                        await self.finish_run(run_id)
                        continue
                    deadlines = [
                        DeadlineStop.model_validate(rule)
                        for rule in conditions
                        if rule.get("kind") == "deadline"
                    ]
                    if reason := stop_reason(deadlines, [], 0):
                        async with self.sessions.begin() as db:
                            live = await db.get(ProjectExecution, execution_id)
                            assert live
                            live.termination_reason = "reset_deadline"
                            live.termination_detail = reason
                            live.threshold_mode = "hard"
                        await self.runs.transition(execution_id, ExecutionState.STOPPED)
                        await self.finish_run(run_id)
                        continue
                    if len(self.active) >= self.settings.max_concurrency:
                        continue
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
            deadlines = [rule for rule in policy if isinstance(rule, DeadlineStop)]
            previous_report, _ = await latest_checkpoint(db, project_id)
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
        failure_stage = "preparation"
        final = ExecutionState.COMPLETED
        error_text: str | None = None
        report = RunReport(summary="Execution stopped before a report was produced.")
        windows: list[UsageWindow] = []
        starting_windows: list[UsageWindow] = []
        termination_reason: str | None = None
        termination_detail: str | None = None
        interrupted = False
        budget = asyncio.Event()
        turn_cancel = asyncio.Event()
        grace_started = False
        prior_fingerprint: str | None = None
        prior_progress: str | None = None
        repeats = 0
        runtime_values: dict[str, str] = {}
        redactor = SecretRedactor()

        def elapsed() -> float:
            return elapsed_before + time.monotonic() - clock_started

        async def emit(
            message: str, *, kind: str = "execution.log", data: dict[str, Any] | None = None
        ) -> None:
            try:
                await self.events.publish(
                    kind,
                    message,
                    data=data,
                    run_id=run_id,
                    project_id=project_id,
                    execution_id=execution_id,
                )
            except Exception:
                # A full/broken database must never prevent VM teardown.
                log.warning("execution_event_persist_failed", extra={"execution_id": execution_id})

        async def observe(value: list[UsageWindow]) -> None:
            nonlocal windows
            windows = value
            await check_boundary(value)
            await self.runs.observe(value, execution_id)
            windows = value

        async def check_boundary(observed: list[UsageWindow] | None = None) -> None:
            nonlocal termination_reason, termination_detail
            deadline_reason = stop_reason(deadlines, [], elapsed())
            if termination_reason == "reset_deadline":
                return
            if not deadline_reason and (budget.is_set() or grace_started):
                return
            boundary_windows = observed if observed is not None else windows
            reason = deadline_reason or stop_reason(policy, boundary_windows, elapsed())
            if not reason:
                return
            termination_reason = (
                "reset_deadline"
                if deadline_reason
                else "usage_threshold"
                if any(
                    isinstance(rule, UsageStop) and stop_reason([rule], boundary_windows, elapsed())
                    for rule in policy
                )
                else "runtime_limit"
            )
            termination_detail = reason
            budget.set()
            turn_cancel.set()
            async with self.sessions.begin() as db:
                live = await db.get(ProjectExecution, execution_id)
                assert live
                live.termination_reason = termination_reason
                live.termination_detail = reason
                live.threshold_mode = "hard" if deadline_reason else run.threshold_mode
            async with self.sessions() as db:
                live = await db.get(ProjectExecution, execution_id)
                assert live
                state = ExecutionState(live.status)
            if state == ExecutionState.RUNNING:
                await self.runs.transition(execution_id, ExecutionState.STOPPING)
            await self.events.publish(
                "execution.usage_stop",
                reason,
                run_id=run_id,
                project_id=project_id,
                execution_id=execution_id,
                data={
                    "mode": "hard" if deadline_reason else run.threshold_mode,
                    "reason": termination_reason,
                },
            )

        async def monitored_turn(prompt: str, *, finalizing: bool = False) -> RunReport:
            assert session
            turn_cancel.clear()
            await check_boundary()
            if (
                cancel.is_set()
                or termination_reason == "reset_deadline"
                or (budget.is_set() and not finalizing)
            ):
                turn_cancel.set()
                raise asyncio.CancelledError

            async def monitor() -> None:
                nonlocal windows
                interrupt_started: float | None = None
                while True:
                    if cancel.is_set():
                        turn_cancel.set()
                    # Reset deadlines also interrupt the graceful wrap-up turn.
                    if finalizing:
                        await check_boundary()
                    if not finalizing:
                        latest = await self.runs.latest_usage()
                        if latest and (
                            not windows
                            or max(w.observed_at.timestamp() for w in latest)
                            >= max(w.observed_at.timestamp() for w in windows)
                        ):
                            windows = latest
                        await check_boundary()
                    if turn_cancel.is_set():
                        interrupt_started = interrupt_started or time.monotonic()
                        if time.monotonic() - interrupt_started > 12:
                            raise TimeoutError("Codex interrupt timed out; workspace preserved")
                    await asyncio.sleep(0.25)

            watcher = asyncio.create_task(monitor())
            work = asyncio.create_task(
                session.turn(prompt, execution.model, execution.reasoning_effort, turn_cancel)
            )
            try:
                done, _ = await asyncio.wait(
                    {work, watcher},
                    timeout=90 if finalizing else None,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if watcher in done:
                    await watcher
                if not done:
                    turn_cancel.set()
                    await asyncio.wait(
                        {work, watcher}, timeout=12, return_when=asyncio.FIRST_COMPLETED
                    )
                    raise TimeoutError("Grace period expired; workspace preserved")
                return await work
            finally:
                watcher.cancel()
                work.cancel()
                await asyncio.gather(watcher, work, return_exceptions=True)

        async def github_token() -> InstallationToken | None:
            if integration and app is None:
                raise ValueError("Connect GitHub before starting this project")
            if integration and app:
                if integration.access_mode != "read_only":
                    await reconcile_repository(
                        self.sessions, self.github, integration.repository_id
                    )
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
                                "access_mode",
                                "allow_workflows",
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
                    # Fail closed before booting a writable guest. The callback also
                    # reconciles at credential rotation boundaries.
                    await github_token()
                    if reason := stop_reason(deadlines, [], elapsed()):
                        raise BudgetReached(reason)
                    await self.runs.transition(execution_id, ExecutionState.STARTING_VM)
                    start_attempted = True
                    failure_stage = "vm_start"
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
                    failure_stage = "guest"
                    session = await self.factory.connect(
                        handle, runtime_values, github_token, emit, observe
                    )
                    redactor = session.redactor
                    if reason := stop_reason(deadlines, [], elapsed()):
                        raise BudgetReached(reason)
                    thread_id = await session.initialize(project.thread_id, execution.model)
                    async with self.sessions.begin() as db:
                        live_project = await db.get(Project, project_id)
                        live = await db.get(ProjectExecution, execution_id)
                        assert live_project is not None and live is not None
                        live_project.thread_id = live.thread_id = thread_id
                    await self.runs.transition(execution_id, ExecutionState.RUNNING)
                    starting_windows = await session.usage()
                    windows = starting_windows
                    prompt = self.context(
                        project,
                        integration,
                        secrets_rows,
                        previous_report,
                        await self.projects.tasks(project_id),
                    )
                    while True:
                        if cancel.is_set():
                            raise asyncio.CancelledError
                        await check_boundary()
                        if budget.is_set():
                            final = ExecutionState.STOPPED
                            if (
                                run.threshold_mode == "hard"
                                or termination_reason == "reset_deadline"
                            ):
                                break
                            grace_started = True
                            try:
                                report = await monitored_turn(
                                    "The configured budget boundary has been reached. "
                                    "Stop new substantive work. Settle what is reasonable, "
                                    "leave the workspace coherent, save/commit where appropriate, "
                                    "update Kanban and return a final checkpoint. "
                                    "You have at most 90 seconds. Do not start new work.\n"
                                    + json.dumps(
                                        to_jsonable_python(
                                            {"kanban": await self.projects.tasks(project_id)}
                                        )
                                    ),
                                    finalizing=True,
                                )
                                report.usage = ReportUsage(start=starting_windows, end=windows)
                                await self.save_report(execution_id, project_id, report)
                                interrupted = False
                            except (TimeoutError, asyncio.CancelledError):
                                interrupted = True
                                if cancel.is_set():
                                    raise asyncio.CancelledError from None
                                await emit("Grace period ended; workspace preserved.")
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
                        try:
                            report = await monitored_turn(prompt)
                        except ResumeRequired as error:
                            await emit(str(error))
                            prompt += (
                                "\nInspect existing changes and processes; do not replay actions."
                            )
                            continue
                        except TimeoutError:
                            interrupted = True
                            if cancel.is_set():
                                raise asyncio.CancelledError from None
                            if budget.is_set():
                                final = ExecutionState.STOPPED
                                termination_detail = (
                                    termination_detail or "Budget reached"
                                ) + "; interrupt timed out. Workspace preserved."
                                break
                            raise
                        except asyncio.CancelledError:
                            if budget.is_set() and not cancel.is_set():
                                interrupted = True
                                continue
                            raise
                        async with self.sessions.begin() as db:
                            live_project = await db.get(Project, project_id)
                            if (
                                live_project
                                and live_project.next_run_feedback == project.next_run_feedback
                            ):
                                live_project.next_run_feedback = ""
                        report.usage = ReportUsage(start=starting_windows, end=windows)
                        await self.save_report(execution_id, project_id, report)
                        if cancel.is_set():
                            raise asyncio.CancelledError
                        if budget.is_set():
                            continue
                        if report.status == "completed":
                            termination_reason = "project_completed"
                            break
                        if report.status == "blocked":
                            final = ExecutionState.BLOCKED
                            termination_reason = "blocked"
                            break
                        if report.status in {"failed", "cancelled"}:
                            final = (
                                ExecutionState.CANCELLED
                                if report.status == "cancelled"
                                else ExecutionState.FAILED
                            )
                            termination_reason = "agent_" + report.status
                            error_text = report.summary
                            break
                        fingerprint = json.dumps(
                            {
                                "completed": sorted(report.completed),
                                "remaining": sorted(report.remaining),
                                "blockers": sorted(report.blockers),
                                "changes": report.changes.model_dump(),
                                "tasks": [
                                    {
                                        key: task[key]
                                        for key in (
                                            "id",
                                            "title",
                                            "description",
                                            "column",
                                            "position",
                                        )
                                    }
                                    for task in await self.projects.tasks(project_id)
                                ],
                            },
                            sort_keys=True,
                        )
                        # Cumulative change counts do not prove new progress between turns.
                        checkpoint = json.loads(fingerprint)
                        progress = json.dumps(
                            {"changes": checkpoint["changes"], "tasks": checkpoint["tasks"]},
                            sort_keys=True,
                        )
                        equivalent = progress == prior_progress and (
                            fingerprint == prior_fingerprint
                            or SequenceMatcher(None, fingerprint, prior_fingerprint or "").ratio()
                            >= 0.97
                        )
                        repeats = repeats + 1 if equivalent else 0
                        prior_progress = progress
                        prior_fingerprint = fingerprint
                        if repeats >= 2:
                            final = ExecutionState.STOPPED
                            termination_reason = "no_progress"
                            termination_detail = (
                                "Three equivalent checkpoints; stopped to avoid a work loop."
                            )
                            await emit(termination_detail)
                            break
                        windows = await session.usage()
                        async with self.sessions() as db:
                            current = await db.get(Project, project_id)
                            assert current
                        prompt = self.context(
                            current,
                            integration,
                            secrets_rows,
                            report.model_dump(mode="json"),
                            await self.projects.tasks(project_id),
                        )
                except BudgetReached as error:
                    final = ExecutionState.STOPPED
                    termination_reason = (
                        "reset_deadline"
                        if stop_reason(deadlines, [], elapsed())
                        else "runtime_limit"
                    )
                    termination_detail = str(error)
                    await emit(str(error))
                except ProviderLimited:
                    reason = "Provider usage limit prevents another turn; retained last report."
                    final = ExecutionState.STOPPED
                    termination_reason = "provider_limit"
                    termination_detail = reason
                    interrupted = True
                    await emit(reason)
                except asyncio.CancelledError:
                    if cancel.is_set():
                        final = ExecutionState.CANCELLED
                        error_text = "Cancelled by user"
                        termination_reason = "user_cancelled"
                        interrupted = True
                    else:
                        final = ExecutionState.FAILED
                        error_text = (
                            "Daemon stopped during execution; state retained for inspection"
                        )
                        termination_reason = "infrastructure_error"
                        interrupted = True
                except Exception as error:
                    final = ExecutionState.FAILED
                    termination_reason = "infrastructure_error"
                    interrupted = True
                    error_text = redactor.redact(f"{type(error).__name__}: {error}")
                    detail = execution_failure(
                        error, stage=failure_stage, memory_mib=self.settings.default_memory_mib
                    )
                    termination_detail = "\n\n".join(detail.values())
                    await emit(
                        detail["title"],
                        kind="execution.error",
                        data={**detail, "technical_detail": error_text},
                    )
                finally:
                    # Persist stopping when possible, but no DB/event write is
                    # allowed to gate guest or VM cleanup (including disk-full).
                    try:
                        async with self.sessions() as db:
                            current_execution = await db.get(ProjectExecution, execution_id)
                            assert current_execution
                            state = ExecutionState(current_execution.status)
                        if state not in {ExecutionState.QUEUED, ExecutionState.STOPPING}:
                            await self.runs.transition(execution_id, ExecutionState.STOPPING)
                    except Exception as error:
                        final = ExecutionState.FAILED
                        termination_reason = "infrastructure_error"
                        error_text = f"Execution state persistence failed: {type(error).__name__}"
                        await emit(error_text)
                    if session:
                        try:
                            await session.close()
                        except Exception as error:
                            final = ExecutionState.FAILED
                            termination_reason = "infrastructure_error"
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
                    # Resource cleanup is confirmed. Retry persistence if its
                    # earlier failure was transient; otherwise keep reservation.
                    async with self.sessions() as db:
                        current_execution = await db.get(ProjectExecution, execution_id)
                        assert current_execution
                        state = ExecutionState(current_execution.status)
                    if state not in {ExecutionState.QUEUED, ExecutionState.STOPPING}:
                        await self.runs.transition(execution_id, ExecutionState.STOPPING)
                    if state == ExecutionState.QUEUED:
                        final = ExecutionState.FAILED
                    async with self.sessions.begin() as db:
                        live = await db.get(ProjectExecution, execution_id)
                        assert live
                        live.termination_reason = termination_reason or "infrastructure_error"
                        live.termination_detail = termination_detail or error_text
                        live.threshold_mode = (
                            "hard"
                            if termination_reason == "reset_deadline"
                            else run.threshold_mode
                            if budget.is_set()
                            else None
                        )
                        live.interrupted = interrupted
                    # Only valid model checkpoints are saved. Host outcomes never rewrite them.
                    await self.runs.transition(execution_id, final, error_text)
        except asyncio.CancelledError:
            # Cancellation while waiting for the lease never attached disks.
            async with self.sessions() as db:
                live = await db.get(ProjectExecution, execution_id)
                cancelled_state = ExecutionState(live.status) if live else None
            if cancelled_state == ExecutionState.QUEUED:
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

    async def save_report(self, execution_id: str, project_id: str, report: RunReport) -> None:
        async with self.sessions.begin() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            live = await db.get(ProjectExecution, execution_id)
            assert live
            run_id = live.run_id
            row = await db.get(Report, execution_id)
            if row:
                row.content = report.model_dump(mode="json")
                row.created_at = utcnow()
            else:
                db.add(Report(execution_id=execution_id, content=report.model_dump(mode="json")))
        try:
            async with self.sessions.begin() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                await self.projects.mutate_tasks_in(db, project_id, report.task_updates, agent=True)
        except ValueError as error:
            await self.events.publish(
                "execution.activity",
                "Kanban update rejected: " + str(error),
                run_id=run_id,
                execution_id=execution_id,
                project_id=project_id,
            )
        await self.events.publish(
            "agent.checkpoint",
            report.summary,
            run_id=run_id,
            execution_id=execution_id,
            project_id=project_id,
            data={"report": report.model_dump(mode="json")},
        )

    @staticmethod
    def context(
        project: Project,
        integration: ProjectGitHub | None,
        secrets: list[SecretEntry],
        previous_report: dict[str, Any] | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> str:
        context: dict[str, Any] = {
            "project": project.name,
            "description": project.description,
            "kanban": tasks or [],
            "feedback": project.next_run_feedback,
            "workspace": "/workspace",
            "project_machine": "persistent Linux root filesystem",
            "previous_run": previous_report,
            "secrets": [{"name": secret.name, "purpose": secret.description} for secret in secrets],
        }
        if integration:
            context["github"] = {
                "repository": integration.repository_name,
                "access_mode": integration.access_mode,
                "guidance": MODE_GUIDANCE[integration.access_mode],
                "allow_workflow_changes": integration.allow_workflows,
            }
        return (
            "You are the autonomous worker for a persistent software project. You have root "
            "inside an isolated persistent project VM. Inspect the workspace and installed tools "
            "before acting. Make substantial useful progress, run appropriate checks, and continue "
            "without human approvals. Install development tools as needed; "
            "home and Nix state persist. "
            "Use /workspace for source. Secrets are runtime environment variables; "
            "never copy or log "
            "their values into persistent files. Use GitHub only within the listed permissions. "
            "Resume In progress tasks first, then Todo. Backlog is human-controlled approval: "
            "never promote or work Backlog tasks. Create discovered work only in Backlog. "
            "Use task_updates with stable task IDs to edit/move tasks; id null creates a new "
            "Backlog task. When no In progress or Todo tasks remain, report completed because "
            "there is no currently approved work; do not invent work to consume turns. "
            "Return an honest report after a meaningful work unit, marking "
            "completed when approved work is finished, blocked only when you cannot "
            "make useful progress. Do not ask for approvals.\n"
            + json.dumps(to_jsonable_python(context))
        )
