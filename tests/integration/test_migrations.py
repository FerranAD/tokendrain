from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text

from tokendrain.db.engine import migrate, open_database
from tokendrain.db.models import Base


async def test_real_alembic_schema_and_idempotency(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    await migrate(path)
    await migrate(path)
    engine, _ = open_database(path)
    try:
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0002"
            )
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert {"projects", "runs", "project_executions", "secret_entries", "schedules"} <= set(
                tables
            )
            differences = await connection.run_sync(
                lambda conn: compare_metadata(MigrationContext.configure(conn), Base.metadata)
            )
            assert differences == []
            assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert await connection.scalar(text("PRAGMA journal_mode")) == "wal"
    finally:
        await engine.dispose()
