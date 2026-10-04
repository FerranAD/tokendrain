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


def activity(message: str) -> tuple[str, str, dict[str, Any]]:
    """Normalize completed protocol items while preserving their full redacted payload."""
    try:
        raw = json.loads(message)
    except (ValueError, TypeError):
        return "execution.activity", message, {}
    if not isinstance(raw, dict):
        return "execution.activity", message, {"raw": raw}
    kind = raw.get("type")
    data: dict[str, Any] = {"raw": raw}
    if kind == "error":
        error = raw.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        return "execution.error", str(raw.get("message") or detail or "Codex error"), data
    if kind == "userMessage":
        return "execution.debug", "Internal orchestration prompt", data
    if kind == "agentMessage":
        text = str(raw.get("text", ""))
        if raw.get("phase") in (None, "final_answer"):
            from tokendrain.orchestration.driver import parse_report

            try:
                report = parse_report(text)
            except ValueError:
                if raw.get("phase") is None:
                    return "agent.progress", text, data
                return "execution.error", "Agent returned an invalid checkpoint", data
            data["report"] = report.model_dump(mode="json")
            return "agent.checkpoint", report.summary, data
        return "agent.progress", text, data
    if kind == "commandExecution":
        for key in ("command", "cwd", "status", "exitCode", "durationMs", "aggregatedOutput"):
            data[key] = raw.get(key)
        return "command", str(raw.get("command", "Command")), data
    if kind == "fileChange":
        data["changes"] = raw.get("changes", [])
        return "files.changed", "Files changed", data
    return "execution.debug", str(kind or "Protocol event"), data


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
        if type == "execution.log":
            type, message, data = activity(message)
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
                        continue
                    except TimeoutError:
                        pass
            if not rows:
                yield ": keepalive\n\n"
            for row in rows:
                after = row.id
                yield f"id: {row.id}\ndata: {json.dumps(event_json(row))}\n\n"
