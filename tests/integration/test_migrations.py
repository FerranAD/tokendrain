from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from asgi_lifespan import LifespanManager
from sqlalchemy import inspect, text

from tokendrain.api.app import create_app
from tokendrain.application import Overrides
from tokendrain.config import Settings
from tokendrain.db.engine import migrate, migration_config, open_database
from tokendrain.db.models import Automation, AutomationOccurrence, Base


async def test_real_alembic_schema_and_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    await migrate(path)
    await migrate(path)
    engine, _ = open_database(path)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"
            )
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert {
                "projects",
                "runs",
                "automations",
                "automation_occurrences",
                "project_executions",
                "secret_entries",
                "schedules",
            } <= set(tables)
            differences = await connection.run_sync(
                lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata)
            )
            assert differences == []
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("already_present", [False, True])
async def test_automation_tables_upgrade_existing_installation(
    tmp_path: Path, already_present: bool
) -> None:
    settings = Settings(
        state_dir=tmp_path, backend="mock", auth_mode="none", public_url="http://testserver"
    )
    path = settings.database_path
    command.upgrade(migration_config(path), "0003")
    engine, sessions = open_database(path)
    try:
        if already_present:
            async with engine.begin() as connection:
                await connection.run_sync(Automation.__table__.create)
                await connection.run_sync(AutomationOccurrence.__table__.create)
            async with sessions.begin() as db:
                db.add(
                    Automation(
                        id="existing",
                        name="Keep this automation",
                        trigger={},
                        mode="approval",
                        run_template={},
                    )
                )
        await migrate(path)
        await migrate(path)
        web = create_app(settings, Overrides(start_workers=False))
        async with (
            LifespanManager(web),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=web), base_url="http://testserver"
            ) as client,
        ):
            response = await client.get("/api/v1/automations")
            assert response.status_code == 200
            rules = response.json()
            assert [rule["name"] for rule in rules] == (
                ["Keep this automation"] if already_present else []
            )
            response = await client.get("/api/v1/automation-occurrences")
            assert response.status_code == 200
            assert response.json() == []
    finally:
        await engine.dispose()
