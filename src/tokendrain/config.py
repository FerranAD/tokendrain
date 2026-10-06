from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOKENDRAIN_")
    state_dir: Path = Path("/var/lib/tokendrain")
    master_key_file: Path | None = None
    admin_token_file: Path | None = None
    auth_mode: Literal["token", "none"] = "token"
    listen_address: str = "127.0.0.1"
    port: int = Field(default=8742, ge=1, le=65535)
    public_url: str = "http://127.0.0.1:8742"
    helper_socket: Path = Path("/run/tokendrain/helper.sock")
    guest_artifacts: Path | None = None
    web_dir: Path | None = None
    backend: Literal["firecracker", "mock"] = "firecracker"
    auth_runtime_dir: Path = Path("/run/tokendrain-auth")
    default_vcpus: int = Field(default=4, ge=1, le=32)
    default_memory_mib: int = Field(default=4096, ge=512, le=131072)
    default_disk_gib: int = Field(default=40, ge=1, le=4096)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    concurrency_limit: int = Field(default=64, ge=1, le=128)
    vcpus_limit: int = Field(default=32, ge=1, le=32)
    memory_mib_limit: int = Field(default=131072, ge=512, le=131072)
    turn_timeout_seconds: int = Field(default=7200, ge=60)
    shutdown_timeout_seconds: int = Field(default=60, ge=1)
    scheduler_interval_seconds: float = Field(default=10, ge=0.1)
    usage_poll_seconds: float = Field(default=30, ge=1)
    active_agent: Literal["codex", "claude_code"] = "codex"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "tokendrain.sqlite3"
