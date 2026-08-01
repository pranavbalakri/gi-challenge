"""Bounded typed feedback emitted by the EnvMaker harness."""

from __future__ import annotations

import math as _math
from enum import StrEnum as _StrEnum

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

__all__ = ["SignalSeverity", "Signal"]


class SignalSeverity(_StrEnum):
    """Severity of one harness signal."""

    FAILURE = "failure"
    WARNING = "warning"
    NOTE = "note"


class Signal(_BaseModel):
    """A bounded item of structured harness feedback."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    code: str = _Field(pattern=r"^[a-z][a-z0-9_.]{2,63}$")
    severity: SignalSeverity
    message: str = _Field(min_length=1, max_length=2000)
    subject_ids: tuple[str, ...] = ()
    measurements: dict[str, float | int | str | bool] = {}
    guidance: str = _Field(default="", max_length=2000)

    @_model_validator(mode="after")
    def _validate_bounds(self) -> Signal:
        if len(self.measurements) > 32:
            raise ValueError("measurements must contain at most 32 entries")

        for key, value in self.measurements.items():
            if not key:
                raise ValueError("measurement keys must not be empty")
            if len(key) > 64:
                raise ValueError("measurement keys must be at most 64 characters")
            if isinstance(value, float) and not _math.isfinite(value):
                raise ValueError("measurement float values must be finite")
            if isinstance(value, str) and len(value) > 2000:
                raise ValueError(
                    "measurement string values must be at most 2000 characters"
                )

        if len(self.subject_ids) > 64:
            raise ValueError("subject_ids must contain at most 64 entries")
        for subject_id in self.subject_ids:
            if not subject_id:
                raise ValueError("subject_ids entries must not be empty")
            if len(subject_id) > 128:
                raise ValueError("subject_ids entries must be at most 128 characters")

        return self
