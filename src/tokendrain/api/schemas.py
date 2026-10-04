from typing import Any, Literal

from pydantic import Field, SecretStr

from tokendrain.domain import Boundary, ProjectCreate
from tokendrain.github.provider import IntegrationInput


class ProjectCreateInput(ProjectCreate):
    github: IntegrationInput | None = None


class LoginInput(Boundary):
    token: SecretStr


class SnapshotInput(Boundary):
    name: str = Field(default="Manual snapshot", min_length=1, max_length=200)


class RestoreInput(Boundary):
    scope: Literal["workspace", "environment", "all"]


class ResizeInput(Boundary):
    scope: Literal["workspace", "environment"]
    size_gib: int = Field(ge=1, le=4096)


class SecretInput(Boundary):
    value: SecretStr | None = None
    description: str = Field(min_length=1, max_length=10000)


class SecretImport(Boundary):
    dotenv: SecretStr
    descriptions: dict[str, str]


class AuthImport(Boundary):
    auth_json: dict[str, Any] = Field(repr=False)


class VmDefaults(Boundary):
    vcpus: int = Field(ge=1, le=32)
    memory_mib: int = Field(ge=512, le=131072)
    disk_gib: int = Field(ge=1, le=4096)


class PlatformInput(Boundary):
    concurrency: int = Field(ge=1, le=64)
    vm_defaults: VmDefaults
