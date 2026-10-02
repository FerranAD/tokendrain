"""Durable cron scheduling; the due occurrence creates an ordinary Run transactionally."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.db.models import Schedule
from tokendrain.domain import RunTemplate, utcnow
from tokendrain.events import EventBus

log = logging.getLogger(__name__)


def next_occurrence(expression: str, timezone: str, after: datetime) -> datetime:
    # Iterate UTC minutes, matching local calendar fields. This skips nonexistent local
    # times and deliberately fires twice for a repeated DST hour (each is a unique instant).
    if after.tzinfo is None:
        after = after.replace(tzinfo=UTC)
    zone = ZoneInfo(timezone)
    # croniter's timezone-aware iterator handles ordinary cases efficiently; validate its
    # wall-clock candidate to discard phantom spring-forward times.
    candidate = croniter(expression, after.astimezone(zone)).get_next(datetime)
    for _ in range(10):
        normalized = candidate.astimezone(UTC).astimezone(zone)
        if croniter.match(expression, normalized) and normalized.astimezone(UTC) > after:
            return normalized.astimezone(UTC)
        candidate = croniter(expression, candidate).get_next(datetime)
    raise ValueError("Cannot find valid timezone-aware cron occurrence")


class RunCreator(Protocol):
    async def create_run_in(
        self,
        db: AsyncSession,
        template: RunTemplate,
        *,
        schedule_id: str | None = None,
        scheduled_for: datetime | None = None,
    ) -> str: ...


class Scheduler:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        creator: RunCreator,
        events: EventBus,
        interval: float = 10,
    ) -> None:
        self.sessions, self.creator, self.events = sessions, creator, events
        self.interval = interval

    async def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        notifications: list[tuple[str, str]] = []
        async with self.sessions.begin() as db:
            schedules = list(
                (
                    await db.scalars(
                        select(Schedule).where(
                            Schedule.enabled.is_(True), Schedule.next_run_at <= now
                        )
                    )
                ).all()
            )
            for schedule in schedules:
                # Coalesce downtime to one run; advance atomically even when overlap skips it.
                due = schedule.next_run_at
                schedule.next_run_at = next_occurrence(schedule.cron, schedule.timezone, now)
                try:
                    async with db.begin_nested():
                        run_id = await self.creator.create_run_in(
                            db,
                            RunTemplate.model_validate(schedule.run_template),
                            schedule_id=schedule.id,
                            scheduled_for=due,
                        )
                    schedule.last_run_at = now
                    notifications.append(("schedule.triggered", run_id))
                except ValueError as error:
                    notifications.append(("schedule.skipped", str(error)))
        for kind, message in notifications:
            await self.events.publish(kind, message)

    async def serve(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler.tick_failed")
            await asyncio.sleep(self.interval)
