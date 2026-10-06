from typing import Any

from pydantic import Field, SecretStr

from tokendrain.agents import AgentName
from tokendrain.domain import Boundary, ProjectCreate
from tokendrain.github.provider import RepositoryInput


class ProjectCreateInput(ProjectCreate):
    github: RepositoryInput | None = None


class LoginInput(Boundary):
    token: SecretStr


class AgentInput(Boundary):
    name: AgentName


class ClaudeLoginCode(Boundary):
    code: SecretStr = Field(min_length=1, max_length=4096)


class ResizeInput(Boundary):
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
