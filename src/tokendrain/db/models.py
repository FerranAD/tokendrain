from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tokendrain.domain import utcnow


def new_id() -> str:
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    next_run_feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    status: Mapped[str] = mapped_column(String(24), default="idle")
    default_model: Mapped[str] = mapped_column(String(200), default="")
    default_reasoning_effort: Mapped[str] = mapped_column(String(24), default="medium")
    environment_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    workspace_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    thread_id: Mapped[str | None] = mapped_column(String(200))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    threshold_mode: Mapped[str] = mapped_column(String(16), default="graceful")
    parallel: Mapped[bool] = mapped_column(Boolean, default=True)
    stop_conditions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    schedule_id: Mapped[str | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (Index("uq_schedule_occurrence", "schedule_id", "scheduled_for", unique=True),)


class ProjectExecution(Base):
    __tablename__ = "project_executions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="queued")
    model: Mapped[str] = mapped_column(String(200), default="")
    reasoning_effort: Mapped[str] = mapped_column(String(24), default="medium")
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    thread_id: Mapped[str | None] = mapped_column(String(200))
    termination_reason: Mapped[str | None] = mapped_column(String(40))
    termination_detail: Mapped[str | None] = mapped_column(Text)
    threshold_mode: Mapped[str | None] = mapped_column(String(16))
    interrupted: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    vm_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    __table_args__ = (
        Index(
            "uq_project_active",
            "project_id",
            unique=True,
            sqlite_where=text(
                "status IN ('queued','preparing','starting_vm','running','stopping')"
            ),
        ),
    )


class Report(Base):
    __tablename__ = "run_reports"
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("project_executions.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Agent-produced schema versioned structured report; relational ownership remains explicit.
    content: Mapped[dict[str, Any]] = mapped_column(JSON)


class UsageSnapshot(Base):
    __tablename__ = "usage_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("project_executions.id", ondelete="SET NULL"), index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    windows: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    cron: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    run_template: Mapped[dict[str, Any]] = mapped_column(JSON)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(100))
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    run_id: Mapped[str | None] = mapped_column(String(32), index=True)
    project_id: Mapped[str | None] = mapped_column(String(32))
    execution_id: Mapped[str | None] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ProjectTask(Base):
    __tablename__ = "project_tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    column: Mapped[str] = mapped_column(String(24), default="todo")
    position: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProjectSnapshot(Base):
    __tablename__ = "project_snapshots"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    environment_bytes: Mapped[int] = mapped_column(Integer, default=0)
    workspace_bytes: Mapped[int] = mapped_column(Integer, default=0)


class SecretEntry(Base):
    __tablename__ = "secret_entries"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    credential_ref: Mapped[str] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GitHubApp(Base):
    __tablename__ = "github_apps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    app_id: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(200))
    credential_ref: Mapped[str] = mapped_column(String(200))


class GitHubInstallation(Base):
    __tablename__ = "github_installations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(200))
    permissions: Mapped[dict[str, str]] = mapped_column(JSON)


class ProjectGitHub(Base):
    __tablename__ = "project_github_integrations"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    installation_id: Mapped[int] = mapped_column(Integer)
    repository_id: Mapped[int] = mapped_column(Integer)
    repository_name: Mapped[str] = mapped_column(String(300))
    permissions: Mapped[dict[str, str]] = mapped_column(JSON)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
