"""Explicit dependency container and ownership of long-lived application resources."""

import asyncio
import fcntl
import logging
import time
from dataclasses import dataclass
from typing import BinaryIO

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tokendrain.auth.openai import OpenAIAuthManager
from tokendrain.config import Settings
from tokendrain.credentials.store import EncryptedFileCredentialStore, load_master_key
from tokendrain.db.engine import migrate, open_database
from tokendrain.db.models import Setting
from tokendrain.events import EventBus
from tokendrain.github.provider import GitHubProvider
from tokendrain.orchestration.driver import RealSessionFactory, SessionFactory
from tokendrain.orchestration.mock import MockSessionFactory, MockStorage, MockVmBackend
from tokendrain.orchestration.supervisor import Supervisor
from tokendrain.scheduler.service import Scheduler
from tokendrain.security import SessionTokens, protected_file
from tokendrain.services import ProjectService, RunService
from tokendrain.storage import FileProjectStorage
from tokendrain.storage.files import ProjectStorage
from tokendrain.vm import FirecrackerBackend, VmBackend

log = logging.getLogger(__name__)


@dataclass
class Overrides:
    storage: ProjectStorage | None = None
    vm: VmBackend | None = None
    factory: SessionFactory | None = None
    http: httpx.AsyncClient | None = None
    start_workers: bool = True


@dataclass
class Application:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    http: httpx.AsyncClient
    credentials: EncryptedFileCredentialStore
    auth: OpenAIAuthManager
    github: GitHubProvider
    storage: ProjectStorage
    projects: ProjectService
    runs: RunService
    events: EventBus
    scheduler: Scheduler
    supervisor: Supervisor
    tokens: SessionTokens
    lock: BinaryIO
    started_at: float
    task: asyncio.Task[None] | None = None
    probe_lock: asyncio.Lock | None = None
    probe_cache: dict[str, object] | None = None

    @classmethod
    async def open(cls, settings: Settings, overrides: Overrides | None = None) -> "Application":
        overrides = overrides or Overrides()
        settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = (settings.state_dir / "daemon.lock").open("a+b")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("Another tokendraind owns this state directory") from None
        engine: AsyncEngine | None = None
        http: httpx.AsyncClient | None = None
        try:
            await migrate(settings.database_path)
            engine, sessions = open_database(settings.database_path)
            async with sessions.begin() as db:
                # The process lock proves no former credential mutation can still finish.
                await db.execute(delete(Setting).where(Setting.key == "provider_change"))
            if settings.master_key_file is None:
                if settings.backend != "mock":
                    raise ValueError("TOKENDRAIN_MASTER_KEY_FILE is required for the real backend")
                settings.master_key_file = settings.state_dir / "master.key"
                await asyncio.to_thread(protected_file, settings.master_key_file, raw=True)
            key = await asyncio.to_thread(load_master_key, settings.master_key_file)
            credentials = EncryptedFileCredentialStore(settings.state_dir / "credentials", key)
            if settings.auth_mode == "token":
                if settings.admin_token_file is None:
                    raise ValueError(
                        "Token auth requires TOKENDRAIN_ADMIN_TOKEN_FILE; "
                        "set TOKENDRAIN_AUTH_MODE=none to disable login"
                    )
                token = (await asyncio.to_thread(settings.admin_token_file.read_text)).strip()
                tokens = SessionTokens(token)
            else:
                tokens = SessionTokens(None)
            http = overrides.http or httpx.AsyncClient(timeout=30, follow_redirects=False)
            auth = OpenAIAuthManager(credentials, http, runtime_dir=settings.auth_runtime_dir)
            github = GitHubProvider(credentials, http)
            async with sessions() as db:
                saved = await db.get(Setting, "platform")
                if saved:
                    settings.max_concurrency = int(saved.value["concurrency"])
                    defaults = saved.value["vm_defaults"]
                    settings.default_vcpus = defaults["vcpus"]
                    settings.default_memory_mib = defaults["memory_mib"]
                    settings.default_disk_gib = defaults["disk_gib"]
            settings.max_concurrency = min(settings.max_concurrency, settings.concurrency_limit)
            settings.default_vcpus = min(settings.default_vcpus, settings.vcpus_limit)
            settings.default_memory_mib = min(
                settings.default_memory_mib, settings.memory_mib_limit
            )
            storage: ProjectStorage = overrides.storage or (
                MockStorage(settings.state_dir, settings.default_disk_gib)
                if settings.backend == "mock"
                else FileProjectStorage(settings.state_dir, settings.default_disk_gib)
            )
            vm = overrides.vm or (
                MockVmBackend()
                if settings.backend == "mock"
                else FirecrackerBackend(settings.helper_socket)
            )
            factory = overrides.factory or (
                MockSessionFactory()
                if settings.backend == "mock"
                else RealSessionFactory(auth, settings.turn_timeout_seconds)
            )
            events = EventBus(sessions)
            runs, projects = RunService(sessions, events), ProjectService(sessions, storage, events)
            scheduler = Scheduler(sessions, runs, events, settings.scheduler_interval_seconds)
            supervisor = Supervisor(
                settings,
                sessions,
                runs,
                projects,
                events,
                storage,
                vm,
                factory,
                credentials,
                github,
            )
            app = cls(
                settings,
                engine,
                sessions,
                http,
                credentials,
                auth,
                github,
                storage,
                projects,
                runs,
                events,
                scheduler,
                supervisor,
                tokens,
                lock,
                time.monotonic(),
                probe_lock=asyncio.Lock(),
            )
            if overrides.start_workers:
                app.task = asyncio.create_task(app.serve(), name="tokendrain-services")
                app.task.add_done_callback(app.service_finished)
            else:
                await supervisor.reconcile()
            return app
        except BaseException:
            if http and overrides.http is None:
                await http.aclose()
            if engine:
                await engine.dispose()
            lock.close()
            raise

    @staticmethod
    def service_finished(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("daemon.background_failed", extra={"error_type": type(error).__name__})

    async def serve(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self.supervisor.serve(), name="run-supervisor")
            group.create_task(self.scheduler.serve(), name="scheduler")
            group.create_task(self.housekeeping(), name="event-retention")

    async def housekeeping(self) -> None:
        from datetime import timedelta

        from tokendrain.domain import utcnow

        while True:
            await self.runs.prune_events(utcnow() - timedelta(days=30))
            await asyncio.sleep(3600)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await asyncio.wait_for(self.task, self.settings.shutdown_timeout_seconds)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                log.error("daemon.shutdown_timeout_resources_reserved_for_recovery")
            except Exception:
                log.exception("daemon.service_failed")
        await self.http.aclose()
        await self.engine.dispose()
        self.lock.close()
