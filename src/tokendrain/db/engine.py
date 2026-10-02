import asyncio
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def migration_config(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}".replace("%", "%%"))
    return config


async def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(command.upgrade, migration_config(path), "head")


def open_database(path: Path) -> tuple[AsyncEngine, async_sessionmaker[Any]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def pragmas(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)
