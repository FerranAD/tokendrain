"""Durable usage-triggered runs and explicit, reset-bound website approvals."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.db.models import Automation, AutomationOccurrence, Project
from tokendrain.domain import Boundary, DeadlineStop, RunTemplate, UsageStop, UsageWindow, utcnow
from tokendrain.events import EventBus
from tokendrain.notifications import NtfyService, UsageTrigger
from tokendrain.services import RunService, columns

log = logging.getLogger(__name__)
INTERVAL_SECONDS = 15 * 60
UNLAUNCHED = ("ready", "pending")


class AutomationInput(Boundary):
    name: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    trigger: UsageTrigger = Field(default_factory=lambda: UsageTrigger(min_remaining_percent=1))
    mode: Literal["automatic", "approval"] = "approval"
    run_template: RunTemplate


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def occurrence_json(row: AutomationOccurrence, now: datetime | None = None) -> dict[str, Any]:
    result = columns(row)
    if row.status in UNLAUNCHED and aware(row.resets_at) <= (now or utcnow()):
        result["status"] = "expired"
    return result


class AutomationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runs: RunService,
        events: EventBus,
        notifications: NtfyService,
        read_usage: Callable[[], Awaitable[list[UsageWindow]]],
        refresh_usage: Callable[[], Awaitable[list[UsageWindow]]],
    ) -> None:
        self.sessions, self.runs, self.events = sessions, runs, events
        self.notifications = notifications
        self.read_usage, self.refresh_usage = read_usage, refresh_usage
        self.lock = asyncio.Lock()

    async def list_automations(self) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            return [
                columns(row)
                for row in (
                    await db.scalars(select(Automation).order_by(Automation.created_at.desc()))
                ).all()
            ]

    async def get(self, automation_id: str) -> dict[str, Any]:
        async with self.sessions() as db:
            row = await db.get(Automation, automation_id)
            if row is None:
                raise LookupError("Automation not found")
            return columns(row)

    async def save(
        self, value: AutomationInput | dict[str, Any], automation_id: str | None = None
    ) -> dict[str, Any]:
        async with self.lock:
            async with self.sessions.begin() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                if automation_id:
                    row = await db.get(Automation, automation_id)
                    if row is None:
                        raise LookupError("Automation not found")
                    merged = {key: getattr(row, key) for key in AutomationInput.model_fields}
                    merged.update(value if isinstance(value, dict) else value.model_dump())
                    body = AutomationInput.model_validate(merged)
                else:
                    body = AutomationInput.model_validate(value)
                    row = Automation()
                if body.mode == "approval" and body.enabled:
                    if not (await self.notifications.config()).topic:
                        raise ValueError(
                            "Configure an ntfy topic in Settings before enabling approvals"
                        )
                if body.enabled:
                    for project in body.run_template.projects:
                        if not await db.get(Project, project.project_id):
                            raise ValueError(f"Project {project.project_id} no longer exists")
                changed = automation_id is not None and any(
                    getattr(row, key) != val for key, val in body.model_dump(mode="json").items()
                )
                for key, val in body.model_dump(mode="json").items():
                    setattr(row, key, val)
                row.last_error = None
                db.add(row)
                if changed:
                    await self._cancel_in(db, automation_id)
                await db.flush()
                result = columns(row)
            await self.events.publish(
                "automation.updated" if automation_id else "automation.created"
            )
            return result

    async def _cancel_in(self, db: AsyncSession, automation_id: str | None) -> None:
        for occurrence in (
            await db.scalars(
                select(AutomationOccurrence).where(
                    AutomationOccurrence.automation_id == automation_id,
                    AutomationOccurrence.status.in_(UNLAUNCHED),
                )
            )
        ).all():
            occurrence.status = "cancelled"

    async def delete(self, automation_id: str) -> None:
        async with self.lock:
            async with self.sessions.begin() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                row = await db.get(Automation, automation_id)
                if row is None:
                    raise LookupError("Automation not found")
                await self._cancel_in(db, automation_id)
                await db.delete(row)
            await self.events.publish("automation.deleted")

    async def occurrences(self, automation_id: str | None = None) -> list[dict[str, Any]]:
        async with self.sessions() as db:
            query = select(AutomationOccurrence).order_by(AutomationOccurrence.created_at.desc())
            if automation_id:
                query = query.where(AutomationOccurrence.automation_id == automation_id)
            # Pending approvals must remain visible even with a long history.
            rows = (
                await db.scalars(query.where(AutomationOccurrence.status.in_(UNLAUNCHED)))
            ).all()
            history = (
                await db.scalars(
                    query.where(AutomationOccurrence.status.not_in(UNLAUNCHED)).limit(200)
                )
            ).all()
            return [occurrence_json(row) for row in [*rows, *history]]

    async def occurrence(self, occurrence_id: str) -> dict[str, Any]:
        async with self.sessions() as db:
            row = await db.get(AutomationOccurrence, occurrence_id)
            if row is None:
                raise LookupError("Automation occurrence not found")
            result = occurrence_json(row)
        result["current_usage"] = [
            w.model_dump(mode="json") for w in await self.runs.latest_usage()
        ]
        return result

    @staticmethod
    def _matching(
        row: Automation, occurrence: AutomationOccurrence, windows: list[UsageWindow], now: datetime
    ) -> bool:
        trigger = UsageTrigger.model_validate(row.trigger)
        return row.enabled and any(
            w.limit_id == occurrence.limit_id
            and w.window_minutes == occurrence.window_minutes
            and w.resets_at is not None
            and aware(w.resets_at) == aware(occurrence.resets_at)
            and trigger.matches(w, now)
            for w in windows
        )

    async def _launch_in(
        self, db: AsyncSession, occurrence: AutomationOccurrence, now: datetime
    ) -> str | None:
        if aware(occurrence.resets_at) <= max(now, utcnow()):
            occurrence.status = "expired"
            return None
        template = RunTemplate.model_validate(occurrence.run_template)
        # Bound the concrete occurrence, never the reusable definition.
        for condition in template.stop_conditions:
            if (
                isinstance(condition, UsageStop)
                and condition.window_minutes == occurrence.window_minutes
                and not condition.limit_id
            ):
                condition.limit_id = occurrence.limit_id
        template.stop_conditions.append(DeadlineStop(at=aware(occurrence.resets_at)))
        try:
            async with db.begin_nested():
                run_id = await self.runs.create_run_in(db, template)
        except ValueError as error:
            occurrence.last_error = str(error)
            for project in template.projects:
                if not await db.get(Project, project.project_id):
                    occurrence.status = "configuration_error"
                    break
            return None
        occurrence.run_id, occurrence.status, occurrence.last_error = run_id, "launched", None
        return run_id

    async def authorize(self, occurrence_id: str) -> dict[str, Any]:
        async with self.lock:
            # Idempotent approval must not depend on an available provider after launch.
            existing = await self.occurrence(occurrence_id)
            if existing["status"] == "launched":
                return existing
            if existing["status"] != "pending":
                raise ValueError("This approval is no longer pending")
            windows = await self.refresh_usage()
            error: str | None = None
            async with self.sessions.begin() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                occurrence = await db.get(AutomationOccurrence, occurrence_id)
                assert occurrence is not None
                row = (
                    await db.get(Automation, occurrence.automation_id)
                    if occurrence.automation_id
                    else None
                )
                now = utcnow()
                if occurrence.status == "launched":
                    return occurrence_json(occurrence)
                if aware(occurrence.resets_at) <= now:
                    occurrence.status = "expired"
                    error = "This approval expired at the usage reset"
                elif occurrence.status != "pending" or row is None or not row.enabled:
                    error = "This approval is no longer pending"
                elif not self._matching(row, occurrence, windows, now):
                    error = "No fresh matching usage observation; the automation must still match"
                else:
                    await self._launch_in(db, occurrence, now)
                    error = occurrence.last_error
                result = occurrence_json(occurrence)
            await self.events.publish(
                "automation.authorized" if result["status"] == "launched" else "automation.updated",
                run_id=result["run_id"],
            )
            if error:
                raise ValueError(error)
            return result

    async def dismiss(self, occurrence_id: str) -> dict[str, Any]:
        async with self.lock:
            async with self.sessions.begin() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                row = await db.get(AutomationOccurrence, occurrence_id)
                if row is None:
                    raise LookupError("Automation occurrence not found")
                if occurrence_json(row)["status"] != "pending":
                    raise ValueError("This approval is no longer pending")
                row.status = "dismissed"
                result = occurrence_json(row)
            await self.events.publish("automation.dismissed")
            return result

    async def tick(self, now: datetime | None = None) -> None:
        async with self.lock:
            # Avoid probing accounts when no automation is enabled.
            if not any(row["enabled"] for row in await self.list_automations()):
                return
            usage_error: str | None = None
            try:
                windows = await self.read_usage()
                if not any(
                    -30 <= ((now or utcnow()) - w.observed_at).total_seconds() <= 300
                    for w in windows
                    if w.observed_at.tzinfo is not None
                ):
                    usage_error = "No fresh usage observation. Check the Codex connection."
            except (ValueError, OSError, TimeoutError, httpx.HTTPError) as error:
                log.warning(
                    "automations.usage_unavailable", extra={"error_type": type(error).__name__}
                )
                windows = []
                usage_error = (
                    "Usage unavailable. Check the Codex connection; the next check will retry."
                )
            notify: set[str] = set()
            async with self.sessions.begin() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                now = now or utcnow()
                for previous in (
                    await db.scalars(
                        select(AutomationOccurrence).where(
                            AutomationOccurrence.status.in_(UNLAUNCHED)
                        )
                    )
                ).all():
                    if aware(previous.resets_at) <= now:
                        previous.status = "expired"
                for row in (
                    await db.scalars(select(Automation).where(Automation.enabled.is_(True)))
                ).all():
                    row.last_checked_at, row.last_error = now, usage_error
                    trigger = UsageTrigger.model_validate(row.trigger)
                    for window in windows:
                        if not trigger.matches(window, now):
                            continue
                        assert window.resets_at is not None and window.window_minutes is not None
                        occurrence = await db.scalar(
                            select(AutomationOccurrence).where(
                                AutomationOccurrence.automation_id == row.id,
                                AutomationOccurrence.limit_id == window.limit_id,
                                AutomationOccurrence.window_minutes == window.window_minutes,
                                AutomationOccurrence.resets_at == aware(window.resets_at),
                            )
                        )
                        if occurrence is None:
                            occurrence = AutomationOccurrence(
                                automation_id=row.id,
                                automation_name=row.name,
                                limit_id=window.limit_id,
                                window_minutes=window.window_minutes,
                                resets_at=aware(window.resets_at),
                                matched_window=window.model_dump(mode="json"),
                                run_template=row.run_template,
                                mode=row.mode,
                                status="pending" if row.mode == "approval" else "ready",
                            )
                            db.add(occurrence)
                            await db.flush()
                        if occurrence.status == "ready":
                            await self._launch_in(db, occurrence, now)
                        elif occurrence.status == "pending" and occurrence.notified_at is None:
                            notify.add(occurrence.id)
            # A pending request is durable before an external notification can succeed.
            for occurrence_id in notify:
                async with self.sessions() as db:
                    occurrence = await db.get(AutomationOccurrence, occurrence_id)
                    assert occurrence is not None
                    if occurrence_json(occurrence)["status"] != "pending":
                        continue
                try:
                    await self.notifications.approval(
                        occurrence.id,
                        occurrence.automation_name,
                        f"{max(0, 100 - float(occurrence.matched_window['used_percent'])):.0f}% "
                        f"allowance remains. Reset: {aware(occurrence.resets_at).isoformat()}. "
                        "Open Tokendrain to review and authorize the run before reset.",
                    )
                    delivery_error = None
                except (ValueError, OSError, TimeoutError):
                    delivery_error = (
                        "ntfy delivery failed. Check notification settings; "
                        "the next check will retry."
                    )
                async with self.sessions.begin() as db:
                    saved = await db.get(AutomationOccurrence, occurrence_id)
                    assert saved is not None
                    saved.delivery_error = delivery_error
                    if delivery_error is None:
                        saved.notified_at = utcnow()
            await self.events.publish("automation.checked")

    async def serve(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("automations.worker_failed")
            await asyncio.sleep(INTERVAL_SECONDS)
