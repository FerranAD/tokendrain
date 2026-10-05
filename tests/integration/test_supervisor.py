from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.config import Settings
from tokendrain.credentials import EncryptedFileCredentialStore, SecretRedactor
from tokendrain.db.engine import migrate, open_database
from tokendrain.db.models import Event, Run, Schedule
from tokendrain.domain import (
    ElapsedStop,
    ExecutionState,
    ProjectConfig,
    ProjectCreate,
    ProjectPatch,
    RunReport,
    RunTemplate,
    TaskMutation,
    UsageStop,
    UsageWindow,
    utcnow,
)
from tokendrain.events import EventBus
from tokendrain.github.provider import GitHubProvider
from tokendrain.orchestration.driver import LogCallback, TokenCallback, UsageCallback, WorkSession
from tokendrain.orchestration.mock import MockStorage, MockVmBackend
from tokendrain.orchestration.supervisor import Supervisor
from tokendrain.scheduler.service import Scheduler
from tokendrain.services import ProjectService, RunService
from tokendrain.vm.models import VmHandle, VmSpec


class ScriptSession:
    def __init__(self) -> None:
        self.thread_id = "durable-thread"
        self.redactor = SecretRedactor()
        self.prompts: list[str] = []
        self.windows: list[list[UsageWindow]] = [[]]
        self.reports = [RunReport(status="completed", summary="Implemented and verified")]
        self.closed = False
        self.started = asyncio.Event()
        self.action: Callable[[], Awaitable[None]] | None = None
        self.wait_until_cancel = False
        self.log: LogCallback | None = None
        self.observe: UsageCallback | None = None

    async def initialize(self, thread_id: str | None, model: str) -> str:
        if thread_id:
            self.thread_id = thread_id
        return self.thread_id

    async def usage(self) -> list[UsageWindow]:
        value = self.windows.pop(0) if len(self.windows) > 1 else self.windows[0]
        if self.observe:
            await self.observe(value)
        return value

    async def turn(self, prompt: str, model: str, effort: str, cancel: asyncio.Event) -> RunReport:
        self.prompts.append(prompt)
        self.started.set()
        if self.wait_until_cancel:
            await cancel.wait()
            raise asyncio.CancelledError
        if self.action:
            await self.action()
        return self.reports.pop(0)

    async def close(self) -> None:
        self.closed = True


class ScriptFactory:
    def __init__(self, session: ScriptSession) -> None:
        self.session = session

    async def connect(
        self,
        handle: VmHandle,
        secrets: dict[str, str],
        github_token: TokenCallback,
        log: LogCallback,
        observe: UsageCallback,
    ) -> WorkSession:
        self.session.log, self.session.observe = log, observe
        self.session.redactor.replace_values(list(secrets.values()))
        return self.session


class TrackingVm(MockVmBackend):
    def __init__(self) -> None:
        super().__init__()
        self.starts = 0
        self.fail_stop = False
        self.partial_start = False

    async def start(self, spec: VmSpec) -> VmHandle:
        self.starts += 1
        handle = await super().start(spec)
        if self.partial_start:
            raise RuntimeError("VM boot failed after resource creation")
        return handle

    async def stop(self, handle: VmHandle) -> None:
        if self.fail_stop:
            raise RuntimeError("host failed to confirm VM stop")
        await super().stop(handle)


@dataclass
class Harness:
    sessions: async_sessionmaker[AsyncSession]
    supervisor: Supervisor
    projects: ProjectService
    runs: RunService
    session: ScriptSession
    vm: TrackingVm
    events: EventBus
    storage: MockStorage

    async def run(self, conditions: list[Any] | None = None) -> tuple[str, str, str]:
        project_id = await self.projects.create(
            ProjectCreate(name="persistent project", description="Ship working code")
        )
        template = RunTemplate(projects=[ProjectConfig(project_id=project_id)])
        if conditions is not None:
            template.stop_conditions = conditions
        run = await self.runs.create(template)
        return project_id, run["id"], run["executions"][0]["id"]


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    path = tmp_path / "db.sqlite"
    await migrate(path)
    await migrate(path)
    engine, sessions = open_database(path)
    storage = MockStorage(tmp_path / "storage", disk_gib=1)
    events = EventBus(sessions)
    projects = ProjectService(sessions, storage, events)
    runs = RunService(sessions, events)
    session = ScriptSession()
    vm = TrackingVm()
    credentials = EncryptedFileCredentialStore(tmp_path / "credentials", secrets.token_bytes(32))
    async with httpx.AsyncClient() as http:
        supervisor = Supervisor(
            Settings(state_dir=tmp_path, max_concurrency=2),
            sessions,
            runs,
            projects,
            events,
            storage,
            vm,
            ScriptFactory(session),
            credentials,
            GitHubProvider(credentials, http),
        )
        yield Harness(sessions, supervisor, projects, runs, session, vm, events, storage)
    await engine.dispose()


async def test_run_stops_at_completed_turn_budget_boundary(harness: Harness) -> None:
    project, run, execution = await harness.run([UsageStop(window_minutes=300, used_percent=95)])
    harness.session.windows = [
        [UsageWindow(limit_id="actual", window_minutes=300, used_percent=20)],
        [UsageWindow(limit_id="actual", window_minutes=300, used_percent=95)],
    ]
    harness.session.reports = [
        RunReport(summary="Useful work done"),
        RunReport(summary="Saved checkpoint at budget boundary", status="in_progress"),
    ]
    await harness.supervisor.execute(execution, asyncio.Event())
    state = await harness.runs.get(run)
    assert state["status"] == "stopped"
    assert len(harness.session.prompts) == 2
    assert harness.session.closed and not harness.vm.handles
    assert state["executions"][0]["termination_reason"] == "usage_threshold"
    assert state["executions"][0]["report"]["usage"]["end"][0]["used_percent"] == 95
    assert (await harness.storage.usage(project)).vm_path.exists()


async def test_unobservable_usage_fails_closed_and_preserves_feedback(harness: Harness) -> None:
    project, run, execution = await harness.run([UsageStop(window_minutes=300, used_percent=95)])
    await harness.projects.patch(project, ProjectPatch(next_run_feedback="Important next action"))
    await harness.supervisor.execute(execution, asyncio.Event())
    state = await harness.runs.get(run)
    assert state["status"] == "failed"
    assert "cannot be observed" in state["executions"][0]["error"]
    assert not harness.session.prompts
    assert (await harness.projects.get(project))["next_run_feedback"] == "Important next action"
    assert harness.session.closed and not harness.vm.handles


async def test_infrastructure_failure_is_not_completion(harness: Harness) -> None:
    project, run, execution = await harness.run()

    async def fail() -> None:
        raise ConnectionError("Codex lost transport")

    harness.session.action = fail
    await harness.supervisor.execute(execution, asyncio.Event())
    state = await harness.runs.get(run)
    assert state["status"] == "failed"
    assert state["executions"][0]["termination_reason"] == "infrastructure_error"
    assert (await harness.projects.get(project))["thread_id"] == "durable-thread"
    assert harness.session.closed and not harness.vm.handles


async def test_vm_connection_loss_is_actionable_and_keeps_checkpoint(harness: Harness) -> None:
    _, run, execution = await harness.run()
    harness.supervisor.settings.default_memory_mib = 512
    harness.session.reports = [RunReport(summary="Parser changes saved locally")]
    calls = 0

    async def disconnect() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionRefusedError(111, "Connection refused")

    harness.session.action = disconnect
    await harness.supervisor.execute(execution, asyncio.Event())
    result = (await harness.runs.get(run))["executions"][0]
    assert result["status"] == "failed"
    assert "512 MiB" in result["termination_detail"]
    assert "4096 MiB" in result["termination_detail"]
    assert result["report"]["summary"] == "Parser changes saved locally"
    assert result["error"] == "ConnectionRefusedError: [Errno 111] Connection refused"
    async with harness.sessions() as db:
        event = await db.scalar(select(Event).where(Event.type == "execution.error"))
        assert event
        assert event.message == "Connection to the project VM was lost"
        assert event.data["technical_detail"] == result["error"]
        assert event.project_id == result["project_id"]
    assert harness.session.closed and not harness.vm.handles


@pytest.mark.parametrize("user_cancel", [True, False])
async def test_user_and_owner_cancellation_cleanup(harness: Harness, user_cancel: bool) -> None:
    _, run, execution = await harness.run()
    harness.session.wait_until_cancel = True
    cancel = asyncio.Event()
    task = asyncio.create_task(harness.supervisor.execute(execution, cancel))
    await asyncio.wait_for(harness.session.started.wait(), 3)
    if user_cancel:
        cancel.set()
    else:
        task.cancel()
    await asyncio.wait_for(task, 3)
    assert (await harness.runs.get(run))["status"] == ("cancelled" if user_cancel else "failed")
    assert harness.session.closed and not harness.vm.handles


async def test_unconfirmed_teardown_keeps_reservation_until_reconcile(harness: Harness) -> None:
    project, run, execution = await harness.run()
    harness.vm.fail_stop = True
    await harness.supervisor.execute(execution, asyncio.Event())
    state = await harness.runs.get(run)
    assert state["executions"][0]["status"] == "stopping"
    with pytest.raises(ValueError, match="active"):
        await harness.runs.create(RunTemplate(projects=[ProjectConfig(project_id=project)]))
    harness.vm.fail_stop = False
    await harness.supervisor.reconcile()
    assert not harness.vm.handles
    state = await harness.runs.get(run)
    assert state["status"] == "failed"
    assert state["executions"][0]["termination_reason"] == "infrastructure_error"


async def test_partial_vm_start_is_reconciled(harness: Harness) -> None:
    _, run, execution = await harness.run()
    harness.vm.partial_start = True
    await harness.supervisor.execute(execution, asyncio.Event())
    assert not harness.vm.handles
    assert (await harness.runs.get(run))["status"] == "failed"


async def test_runwide_elapsed_limit_stops_before_next_vm_boot(harness: Harness) -> None:
    _, run, execution = await harness.run([ElapsedStop(seconds=2)])
    async with harness.sessions.begin() as db:
        row = await db.get(Run, run)
        assert row
        row.started_at = utcnow() - timedelta(seconds=10)
    await harness.supervisor.execute(execution, asyncio.Event())
    assert harness.vm.starts == 0
    state = await harness.runs.get(run)
    assert state["status"] == "stopped"
    assert "Runtime reached" in state["executions"][0]["termination_detail"]


async def test_scheduler_creates_ordinary_run_once_and_skips_overlap(harness: Harness) -> None:
    project = await harness.projects.create(ProjectCreate(name="scheduled"))
    now = utcnow()
    async with harness.sessions.begin() as db:
        db.add(
            Schedule(
                id="schedule",
                name="Nightly",
                cron="0 3 * * *",
                timezone="Europe/Madrid",
                run_template=RunTemplate(projects=[ProjectConfig(project_id=project)]).model_dump(
                    mode="json"
                ),
                next_run_at=now - timedelta(days=2),
            )
        )
    scheduler = Scheduler(harness.sessions, harness.runs, harness.events)
    await scheduler.tick(now)
    await scheduler.tick(now)
    runs = await harness.runs.list_runs()
    assert len(runs) == 1 and runs[0]["schedule_id"] == "schedule"
    await scheduler.tick(now + timedelta(days=1))
    assert len(await harness.runs.list_runs()) == 1
    async with harness.sessions() as db:
        skipped = await db.scalar(select(Event).where(Event.type == "schedule.skipped"))
        assert skipped


async def test_previous_report_and_persisted_thread_reach_next_session(harness: Harness) -> None:
    project, run, execution = await harness.run()
    await harness.supervisor.execute(execution, asyncio.Event())
    previous = (await harness.runs.get(run))["executions"][0]["report"]
    harness.session.closed = False
    harness.session.reports = [RunReport(status="completed", summary="Second run verified")]
    new_run = await harness.runs.create(RunTemplate(projects=[ProjectConfig(project_id=project)]))
    await harness.supervisor.execute(new_run["executions"][0]["id"], asyncio.Event())
    assert previous["summary"] in harness.session.prompts[-1]
    assert harness.session.thread_id == "durable-thread"


async def test_sse_replays_persisted_event(harness: Harness) -> None:
    await harness.events.publish("test.observed", "durable event")
    stream = harness.events.stream()
    try:
        event = await asyncio.wait_for(anext(stream), 1)
        assert '"type": "test.observed"' in event and event.startswith("id: ")
    finally:
        await stream.aclose()


@pytest.mark.parametrize("parallel", [True, False])
async def test_dispatcher_enforces_run_parallelism_and_global_concurrency(
    harness: Harness,
    parallel: bool,
) -> None:
    started: asyncio.Queue[ScriptSession] = asyncio.Queue()
    release: dict[int, asyncio.Event] = {}

    class Factory:
        async def connect(
            self,
            handle: VmHandle,
            secrets: dict[str, str],
            github_token: TokenCallback,
            log: LogCallback,
            observe: UsageCallback,
        ) -> WorkSession:
            session = ScriptSession()
            event = asyncio.Event()
            release[id(session)] = event

            async def hold() -> None:
                await started.put(session)
                await event.wait()

            session.action = hold
            return session

    harness.supervisor.factory = Factory()
    projects = [await harness.projects.create(ProjectCreate(name=f"project {i}")) for i in range(3)]
    run = await harness.runs.create(
        RunTemplate(projects=[ProjectConfig(project_id=p) for p in projects], parallel=parallel)
    )
    owner = asyncio.create_task(harness.supervisor.serve())
    try:
        first = await asyncio.wait_for(started.get(), 3)
        if parallel:
            second = await asyncio.wait_for(started.get(), 3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(started.get(), 0.1)
            release[id(first)].set()
            third = await asyncio.wait_for(started.get(), 3)
            release[id(second)].set()
            release[id(third)].set()
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(started.get(), 0.1)
            release[id(first)].set()
            for _ in range(2):
                following = await asyncio.wait_for(started.get(), 3)
                release[id(following)].set()
        async with asyncio.timeout(3):
            while True:
                async with harness.events.changed:
                    if (await harness.runs.get(run["id"]))["status"] == "completed":
                        break
                    await harness.events.changed.wait()
        assert not harness.vm.handles
    finally:
        owner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await owner


async def test_stopping_database_failure_cannot_skip_vm_cleanup(harness: Harness) -> None:
    class FailingTransition(RunService):
        failed = False

        async def transition(
            self, execution_id: str, state: ExecutionState, error: str | None = None
        ) -> None:
            if state == ExecutionState.STOPPING and not self.failed:
                self.failed = True
                raise OSError("database disk full")
            await super().transition(execution_id, state, error)

    _, run, execution = await harness.run()
    harness.supervisor.runs = FailingTransition(harness.sessions, harness.events)
    await harness.supervisor.execute(execution, asyncio.Event())
    assert harness.session.closed and not harness.vm.handles
    result = await harness.runs.get(run)
    assert result["status"] == "failed"
    assert "persistence failed" in result["executions"][0]["error"]


async def test_guest_shutdown_failure_is_failed_even_if_vm_teardown_succeeds(
    harness: Harness,
) -> None:
    class BadClose(ScriptSession):
        async def close(self) -> None:
            self.closed = True
            raise ConnectionError("guest rejected shutdown")

    session = BadClose()
    harness.supervisor.factory = ScriptFactory(session)
    _, run, execution = await harness.run()
    await harness.supervisor.execute(execution, asyncio.Event())
    assert session.closed and not harness.vm.handles
    result = await harness.runs.get(run)
    assert result["status"] == "failed"
    assert "shutdown failed" in result["executions"][0]["error"]


async def test_hard_limit_interrupts_active_work_without_finalization(harness: Harness) -> None:
    _, run_id, execution_id = await harness.run([UsageStop(window_minutes=300, used_percent=70)])
    async with harness.sessions.begin() as db:
        run = await db.get(Run, run_id)
        assert run
        run.threshold_mode = "hard"
    harness.session.windows = [[UsageWindow(limit_id="codex", window_minutes=300, used_percent=69)]]
    harness.session.wait_until_cancel = True
    work = asyncio.create_task(harness.supervisor.execute(execution_id, asyncio.Event()))
    await asyncio.wait_for(harness.session.started.wait(), 3)
    assert harness.session.observe
    await harness.session.observe(
        [UsageWindow(limit_id="codex", window_minutes=300, used_percent=71)]
    )
    await asyncio.wait_for(work, 3)
    execution = (await harness.runs.get(run_id))["executions"][0]
    assert len(harness.session.prompts) == 1
    assert execution["status"] == "stopped"
    assert execution["termination_reason"] == "usage_threshold"
    assert execution["threshold_mode"] == "hard" and execution["interrupted"]
    assert execution["report"] is None
    assert not harness.vm.handles and harness.session.closed


async def test_graceful_limit_wraps_up_once_and_retains_checkpoint(harness: Harness) -> None:
    project_id, run_id, execution_id = await harness.run(
        [UsageStop(window_minutes=300, used_percent=70)]
    )
    tasks = await harness.projects.mutate_tasks(
        project_id, [TaskMutation(title="Approved work", column="todo")]
    )
    harness.session.windows = [[UsageWindow(limit_id="codex", window_minutes=300, used_percent=69)]]

    async def reach_limit() -> None:
        assert harness.session.observe
        await harness.session.observe(
            [UsageWindow(limit_id="codex", window_minutes=300, used_percent=71)]
        )
        harness.session.action = None

    harness.session.action = reach_limit
    harness.session.reports = [
        RunReport(summary="Current unit"),
        RunReport(status="completed", summary="Workspace settled"),
    ]
    await harness.supervisor.execute(execution_id, asyncio.Event())
    execution = (await harness.runs.get(run_id))["executions"][0]
    assert len(harness.session.prompts) == 2
    assert "Stop new substantive work" in harness.session.prompts[1]
    board = json.loads(harness.session.prompts[1].split("\n", 1)[1])["kanban"]
    assert board[0]["id"] == tasks[0]["id"]
    assert isinstance(board[0]["created_at"], str)
    assert execution["status"] == "stopped"  # Model completion cannot override the usage outcome.
    assert execution["termination_reason"] == "usage_threshold"
    assert execution["report"]["summary"] == "Workspace settled"
    assert not execution["interrupted"]


async def test_kanban_timestamps_are_serializable_in_initial_and_continuation_prompts(
    harness: Harness,
) -> None:
    project_id, run_id, execution_id = await harness.run()
    tasks = await harness.projects.mutate_tasks(
        project_id, [TaskMutation(title="Approved work", column="todo")]
    )
    harness.session.reports = [
        RunReport(status="in_progress", summary="Work started"),
        RunReport(status="completed", summary="Work finished"),
    ]
    await harness.supervisor.execute(execution_id, asyncio.Event())
    assert (await harness.runs.get(run_id))["status"] == "completed"
    assert len(harness.session.prompts) == 2
    for prompt in harness.session.prompts:
        context = json.loads(prompt.split("\n", 1)[1])
        task = context["kanban"][0]
        assert task["id"] == tasks[0]["id"]
        assert task["title"] == "Approved work"
        assert isinstance(task["created_at"], str)
        assert isinstance(task["updated_at"], str)


async def test_repeated_checkpoints_stop_no_progress_loop(harness: Harness) -> None:
    _, run, execution = await harness.run()
    harness.session.reports = [RunReport(summary="Same state") for _ in range(4)]
    await harness.supervisor.execute(execution, asyncio.Event())
    result = (await harness.runs.get(run))["executions"][0]
    assert result["status"] == "stopped" and result["termination_reason"] == "no_progress"
    assert len(harness.session.prompts) == 3
    assert result["report"]["status"] == "in_progress"


async def test_cancellation_preserves_previous_valid_checkpoint(harness: Harness) -> None:
    project, run, execution = await harness.run()
    await harness.supervisor.execute(execution, asyncio.Event())
    checkpoint = (await harness.runs.get(run))["executions"][0]["report"]
    next_run = await harness.runs.create(RunTemplate(projects=[ProjectConfig(project_id=project)]))
    harness.session.started.clear()
    harness.session.wait_until_cancel = True
    cancel = asyncio.Event()
    task = asyncio.create_task(harness.supervisor.execute(next_run["executions"][0]["id"], cancel))
    await asyncio.wait_for(harness.session.started.wait(), 3)
    cancel.set()
    await asyncio.wait_for(task, 3)
    result = (await harness.runs.get(next_run["id"]))["executions"][0]
    assert result["report"] == checkpoint
    assert result["termination_reason"] == "user_cancelled"
    assert result["checkpoint_from_execution_id"] == execution
    assert "Cancelled by user" not in result["report"]["blockers"]
