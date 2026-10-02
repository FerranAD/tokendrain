"""Validated domain boundaries. Database and provider wire formats stay at the edges."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class ExecutionState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    STARTING_VM = "starting_vm"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset(
    {
        ExecutionState.COMPLETED,
        ExecutionState.BLOCKED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
    }
)
TRANSITIONS = {
    ExecutionState.QUEUED: {
        ExecutionState.PREPARING,
        ExecutionState.CANCELLED,
        ExecutionState.FAILED,
    },
    ExecutionState.PREPARING: {ExecutionState.STARTING_VM, ExecutionState.STOPPING},
    ExecutionState.STARTING_VM: {ExecutionState.RUNNING, ExecutionState.STOPPING},
    ExecutionState.RUNNING: {ExecutionState.STOPPING},
    ExecutionState.STOPPING: set(TERMINAL),
}


def validate_transition(old: ExecutionState, new: ExecutionState) -> None:
    if new not in TRANSITIONS.get(old, set()):
        raise ValueError(f"Invalid execution transition: {old} -> {new}")


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class UsageWindow(BaseModel):
    limit_id: str
    name: str | None = None
    used_percent: float = Field(ge=0, allow_inf_nan=False)
    window_minutes: int | None = Field(default=None, gt=0)
    resets_at: datetime | None = None
    observed_at: datetime = Field(default_factory=utcnow)
    metadata: dict[str, object] = Field(default_factory=dict)


class UsageStop(Boundary):
    kind: Literal["usage"] = "usage"
    window_minutes: int | None = Field(default=None, gt=0)
    used_percent: float = Field(ge=0, le=100)
    limit_id: str | None = None

    @model_validator(mode="after")
    def selector(self) -> "UsageStop":
        if self.window_minutes is None and not self.limit_id:
            raise ValueError("Specify window_minutes or limit_id")
        return self


class ElapsedStop(Boundary):
    kind: Literal["elapsed"] = "elapsed"
    seconds: int = Field(gt=0, le=60 * 60 * 24 * 31)


class ProviderStop(Boundary):
    kind: Literal["provider_limit"] = "provider_limit"


class CompletedStop(Boundary):
    kind: Literal["project_completed"] = "project_completed"


StopCondition = Annotated[
    UsageStop | ElapsedStop | ProviderStop | CompletedStop, Field(discriminator="kind")
]
Reasoning = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


class ProjectConfig(Boundary):
    project_id: str
    model: str = ""
    reasoning_effort: Reasoning = "medium"


def default_stop_conditions() -> list[StopCondition]:
    return [ProviderStop(), CompletedStop()]


class RunTemplate(Boundary):
    projects: list[ProjectConfig] = Field(min_length=1, max_length=64)
    stop_conditions: list[StopCondition] = Field(default_factory=default_stop_conditions)
    parallel: bool = True

    @field_validator("projects")
    @classmethod
    def unique_projects(cls, value: list[ProjectConfig]) -> list[ProjectConfig]:
        if len({p.project_id for p in value}) != len(value):
            raise ValueError("A project can appear only once in a Run")
        return value


class ProjectCreate(Boundary):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=100_000)
    default_model: str = Field(default="", max_length=200)
    default_reasoning_effort: Reasoning = "medium"


class ProjectPatch(Boundary):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=100_000)
    task_log: str | None = Field(default=None, max_length=500_000)
    next_run_feedback: str | None = Field(default=None, max_length=100_000)
    default_model: str | None = Field(default=None, max_length=200)
    default_reasoning_effort: Reasoning | None = None


class ScheduleInput(Boundary):
    name: str = Field(min_length=1, max_length=200)
    cron: str
    timezone: str = "UTC"
    enabled: bool = True
    run_template: RunTemplate

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as error:
            raise ValueError("Unknown IANA timezone") from error
        return value

    @field_validator("cron")
    @classmethod
    def valid_cron(cls, value: str) -> str:
        from croniter import croniter

        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("Use a valid five-field cron expression")
        return value


class Changes(Boundary):
    files_changed: int = Field(default=0, ge=0)
    insertions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    commits: list[str] = Field(default_factory=list)


class ReportUsage(Boundary):
    start: list[UsageWindow] = Field(default_factory=list)
    end: list[UsageWindow] = Field(default_factory=list)


class RunReport(Boundary):
    status: Literal["in_progress", "completed", "blocked", "failed", "cancelled"] = "in_progress"
    summary: str
    completed: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    changes: Changes = Field(default_factory=Changes)
    suggested_next_action: str = ""
    task_log: str = ""
    usage: ReportUsage = Field(default_factory=ReportUsage)


def stop_reason(
    conditions: list[StopCondition],
    usage: list[UsageWindow],
    elapsed: float,
    *,
    provider_limited: bool = False,
    project_completed: bool = False,
) -> str | None:
    # Provider refusal and completed projects are always safe natural boundaries.
    if provider_limited:
        return "Provider usage limit prevents further work"
    if project_completed:
        return "Project completed"
    for condition in conditions:
        if isinstance(condition, ElapsedStop) and elapsed >= condition.seconds:
            return f"Runtime reached {condition.seconds} seconds"
        if isinstance(condition, UsageStop):
            for window in usage:
                if condition.limit_id and window.limit_id != condition.limit_id:
                    continue
                if condition.window_minutes and window.window_minutes != condition.window_minutes:
                    continue
                if window.used_percent >= condition.used_percent:
                    return f"{window.name or window.limit_id} reached {window.used_percent:g}%"
    return None
