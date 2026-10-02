from __future__ import annotations

from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tokendrain.doctor import DoctorCheck


class VmSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    execution_id: str
    project_id: str
    vcpus: int = Field(default=4, ge=1, le=32)
    memory_mib: int = Field(default=4096, ge=512, le=131072)

    @field_validator("execution_id", "project_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return str(UUID(value))


class VmHandle(BaseModel):
    execution_id: str
    project_id: str
    vsock_path: Path
    guest_port: int = 4050


class VmBackend(Protocol):
    async def diagnostics(self) -> list[DoctorCheck]: ...
    async def start(self, spec: VmSpec) -> VmHandle: ...
    async def stop(self, handle: VmHandle) -> None: ...
    async def reconcile(self) -> list[VmHandle]: ...
