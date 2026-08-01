"""Generated environment programs and fault-contained worker records."""

from __future__ import annotations

from enum import StrEnum as _StrEnum
from typing import Any as _Any

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint

__all__ = [
    "ProviderInfo",
    "EnvironmentProgram",
    "WorkerExitReason",
    "ResourceLimits",
    "WorkerExecution",
]


class ProviderInfo(_BaseModel):
    """Provider metadata for one generated program."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    provider: str = _Field(min_length=1)
    model_name: str = _Field(min_length=1)
    prompt_version: str = _Field(min_length=1)


class EnvironmentProgram(_BaseModel):
    """Canonical generated Python source and its provenance."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    source: str = _Field(min_length=1)
    sdk_version: str = _Field(min_length=1)
    prompt_fingerprint: str = _Field(pattern=r"^[0-9a-f]{64}$")
    provider: ProviderInfo
    source_fingerprint: str = _Field(default="", pattern=r"^[0-9a-f]{64}$")

    @_model_validator(mode="before")
    @classmethod
    def _fill_or_verify_fingerprint(cls, data: _Any) -> _Any:
        if (
            not isinstance(data, dict)
            or "source" not in data
            or "sdk_version" not in data
        ):
            return data

        values = dict(data)
        canonical = _canonical_fingerprint(
            {
                "source": values["source"],
                "sdk_version": values["sdk_version"],
            }
        )
        supplied = values.get("source_fingerprint", "")
        if supplied == "":
            values["source_fingerprint"] = canonical
        elif supplied != canonical:
            raise ValueError("source_fingerprint mismatch")
        return values


class WorkerExitReason(_StrEnum):
    """Why a worker execution stopped."""

    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CRASH = "crash"
    RESOURCE_LIMIT = "resource_limit"
    REJECTED_IMPORTS = "rejected_imports"


class ResourceLimits(_BaseModel):
    """Resource bounds applied to one worker execution."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    cpu_seconds: float = _Field(gt=0, allow_inf_nan=False)
    memory_mb: int = _Field(ge=64)
    output_bytes: int = _Field(ge=1024)
    wall_seconds: float = _Field(gt=0, allow_inf_nan=False)


class WorkerExecution(_BaseModel):
    """Outcome record for one fault-contained worker execution."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    program_fingerprint: str = _Field(pattern=r"^[0-9a-f]{64}$")
    limits: ResourceLimits
    exit_reason: WorkerExitReason
    duration_seconds: float = _Field(ge=0, allow_inf_nan=False)
    stdout_blake2b256: str = _Field(pattern=r"^[0-9a-f]{64}$")
    stderr_blake2b256: str = _Field(pattern=r"^[0-9a-f]{64}$")
    quarantined: bool

    @_model_validator(mode="after")
    def _validate_quarantine(self) -> WorkerExecution:
        expected = self.exit_reason != WorkerExitReason.COMPLETED
        if self.quarantined != expected:
            raise ValueError("quarantine invariant violated")
        return self
