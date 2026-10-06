"""Global agent configuration and transactional switching."""

from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tokendrain.db.models import Automation, AutomationOccurrence, Project, Setting

AgentName = Literal["codex", "claude_code"]
AGENT_NAMES = {"codex": "Codex", "claude_code": "Claude Code"}
CLAUDE_MODELS = [
    {
        "id": name,
        "name": name.capitalize(),
        "is_default": False,
        "reasoning_efforts": ["low", "medium", "high"] if name != "haiku" else ["medium"],
    }
    for name in ("sonnet", "opus", "haiku")
]


async def agent_config(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(Setting, "agent")
    return row.value if row else {"name": "codex", "account_id": None}


async def reset_account(app: Any, account_id: str | None) -> None:
    async with app.sessions.begin() as db:
        row = await db.get(Setting, "agent")
        value = {"name": app.settings.active_agent, "account_id": account_id}
        if row:
            row.value = value
        else:
            db.add(Setting(key="agent", value=value))
        for occurrence in (
            await db.scalars(
                select(AutomationOccurrence).where(
                    AutomationOccurrence.status.in_(["pending", "ready"])
                )
            )
        ).all():
            occurrence.status = "cancelled"
        delivery = await db.get(Setting, "ntfy_delivery")
        if delivery:
            delivery.value = {**delivery.value, "delivered": {}}
    app.probe_cache = None


async def switch_agent(app: Any, name: AgentName) -> None:
    if name == app.settings.active_agent:
        return
    async with app.runs.credentials_change():
        async with app.sessions.begin() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            old = app.settings.active_agent
            for project in (await db.scalars(select(Project))).all():
                state = {
                    **project.agent_state,
                    old: {
                        "model": project.default_model,
                        "effort": project.default_reasoning_effort,
                        "thread_id": project.thread_id,
                    },
                }
                target = state.get(name, {})
                project.agent_state = state
                project.default_model = target.get("model", "")
                project.default_reasoning_effort = target.get("effort", "medium")
                project.thread_id = target.get("thread_id")
            # Window durations follow the selected account; old provider IDs do not.
            for rule in (await db.scalars(select(Automation))).all():
                rule.trigger = {**rule.trigger, "limit_id": None}
            ntfy = await db.get(Setting, "ntfy")
            if ntfy:
                ntfy.value = {
                    **ntfy.value,
                    "rules": [{**rule, "limit_id": None} for rule in ntfy.value.get("rules", [])],
                }
            row = await db.get(Setting, "agent")
            if row:
                row.value = {"name": name, "account_id": None}
            else:
                db.add(Setting(key="agent", value={"name": name, "account_id": None}))
        app.settings.active_agent = name
        if name == "claude_code":
            record = await app.claude.record()
            account = record["id"] if record else None
        else:
            accounts = await app.auth.accounts()
            account = accounts[0].id if accounts else None
        await reset_account(app, account)
        await app.events.publish("agent.changed", AGENT_NAMES[name])
