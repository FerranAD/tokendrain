from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TOKENDRAIN_")
    state_dir: Path = Path("/var/lib/tokendrain")
    master_key_file: Path | None = None
    admin_token_file: Path | None = None
    listen_address: str = "127.0.0.1"
    port: int = Field(default=8742, ge=1, le=65535)
    public_url: str = "http://127.0.0.1:8742"
    helper_socket: Path = Path("/run/tokendrain/helper.sock")
    guest_artifacts: Path | None = None
    web_dir: Path | None = None
    backend: str = "firecracker"
    default_vcpus: int = Field(default=4, ge=1, le=64)
    default_memory_mib: int = Field(default=4096, ge=256, le=1048576)
    default_disk_gib: int = Field(default=40, ge=1, le=4096)
    max_concurrency: int = Field(default=2, ge=1, le=64)
    turn_timeout_seconds: int = Field(default=7200, ge=60)
    shutdown_timeout_seconds: int = Field(default=60, ge=1)
    scheduler_interval_seconds: float = Field(default=10, ge=0.1)
    usage_poll_seconds: float = Field(default=30, ge=1)
    # This is the controller callback required by SIWC, not the UI's reverse proxy URL.
    openai_redirect_uri: str = "http://127.0.0.1:8742/auth/callback"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "tokendrain.sqlite3"
