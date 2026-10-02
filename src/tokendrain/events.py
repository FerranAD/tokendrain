import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.db.models import Event
from tokendrain.domain import utcnow


def event_json(row: Event) -> dict[str, Any]:
    return {
        "id": row.id,
        "type": row.type,
        "timestamp": row.timestamp.isoformat() + "Z"
        if row.timestamp.tzinfo is None
        else row.timestamp.isoformat(),
        "run_id": row.run_id,
        "execution_id": row.execution_id,
        "project_id": row.project_id,
        "message": row.message,
        "data": row.data,
    }


class EventBus:
    """Persist before wakeup; SSE clients replay after disconnect using database IDs."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.changed = asyncio.Condition()

    async def publish(
        self,
        type: str,
        message: str = "",
        *,
        run_id: str | None = None,
        project_id: str | None = None,
        execution_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        async with self.sessions.begin() as db:
            db.add(
                Event(
                    type=type,
                    message=message[:100_000],
                    run_id=run_id,
                    project_id=project_id,
                    execution_id=execution_id,
                    data=data or {},
                    timestamp=utcnow(),
                )
            )
        async with self.changed:
            self.changed.notify_all()

    async def stream(self, after: int = 0) -> AsyncIterator[str]:
        while True:
            # Hold condition through read to avoid losing a notification between query and wait.
            async with self.changed:
                async with self.sessions() as db:
                    rows = list(
                        (
                            await db.scalars(
                                select(Event).where(Event.id > after).order_by(Event.id).limit(200)
                            )
                        ).all()
                    )
                if not rows:
                    try:
                        await asyncio.wait_for(self.changed.wait(), 15)
                    except TimeoutError:
                        pass
            if not rows:
                yield ": keepalive\n\n"
            for row in rows:
                after = row.id
                yield f"id: {row.id}\ndata: {json.dumps(event_json(row))}\n\n"
