"""Immutable evaluator-owned episode contracts."""

from __future__ import annotations

from enum import StrEnum as _StrEnum

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.model import SEMANTIC_ID_PATTERN as _SEMANTIC_ID_PATTERN

__all__ = [
    "ConnectorType",
    "TerminalReason",
    "NavigationProbe",
    "EpisodeResult",
]


class ConnectorType(_StrEnum):
    """Connector type an evaluator may permit."""

    STAIRS = "stairs"
    RAMP = "ramp"
    BRIDGE = "bridge"
    LANDING = "landing"


class TerminalReason(_StrEnum):
    """Reason an evaluator episode ended."""

    ARRIVED = "arrived"
    TIMEOUT = "timeout"
    STUCK = "stuck"
    FELL = "fell"
    ABORTED = "aborted"


class NavigationProbe(_BaseModel):
    """A bounded evaluator-owned navigation episode specification."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    target_landmark_id: str = _Field(pattern=_SEMANTIC_ID_PATTERN)
    success_radius_m: float = _Field(
        gt=0,
        allow_inf_nan=False,
        json_schema_extra={"precision_places": 3},
    )
    max_ticks: int = _Field(ge=1)
    action_repeat: int = _Field(ge=1)
    allowed_connector_types: tuple[ConnectorType, ...]
    stuck_timeout_ticks: int = _Field(ge=1)
    terminal_reasons: tuple[TerminalReason, ...]
    probe_fingerprint: str = ""

    def __init__(self, /, **data: object) -> None:
        validated = self.__pydantic_validator__.validate_python(
            data,
            self_instance=self,
        )
        if validated is not self:
            object.__setattr__(self, "__dict__", validated.__dict__)
            object.__setattr__(
                self,
                "__pydantic_fields_set__",
                validated.__pydantic_fields_set__,
            )
            object.__setattr__(
                self,
                "__pydantic_extra__",
                validated.__pydantic_extra__,
            )
            object.__setattr__(
                self,
                "__pydantic_private__",
                validated.__pydantic_private__,
            )

    @_model_validator(mode="after")
    def _validate_and_fingerprint(self) -> NavigationProbe:
        required_reasons = {TerminalReason.ARRIVED, TerminalReason.TIMEOUT}
        if not required_reasons.issubset(self.terminal_reasons):
            raise ValueError(
                "terminal_reasons must include arrived and timeout"
            )

        if len(set(self.terminal_reasons)) != len(self.terminal_reasons):
            raise ValueError("terminal_reasons must not contain duplicates")
        if len(set(self.allowed_connector_types)) != len(
            self.allowed_connector_types
        ):
            raise ValueError(
                "allowed_connector_types must not contain duplicates"
            )

        if self.stuck_timeout_ticks >= self.max_ticks:
            raise ValueError("stuck timeout must be below max_ticks")

        fingerprint = _canonical_fingerprint(
            {
                "target_landmark_id": self.target_landmark_id,
                "success_radius_m": self.success_radius_m,
                "max_ticks": self.max_ticks,
                "action_repeat": self.action_repeat,
                "allowed_connector_types": self.allowed_connector_types,
                "stuck_timeout_ticks": self.stuck_timeout_ticks,
                "terminal_reasons": self.terminal_reasons,
            }
        )
        if self.probe_fingerprint == "":
            return self.model_copy(update={"probe_fingerprint": fingerprint})
        if self.probe_fingerprint != fingerprint:
            raise ValueError("probe_fingerprint mismatch")
        return self


class EpisodeResult(_BaseModel):
    """A bounded outcome record for one navigation probe."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    probe_fingerprint: str = _Field(pattern=r"^[0-9a-f]{64}$")
    terminal_reason: TerminalReason
    ticks_used: int = _Field(ge=0)
    final_geodesic_distance_m: float = _Field(
        ge=0,
        allow_inf_nan=False,
    )
    path_length_m: float = _Field(ge=0, allow_inf_nan=False)
    collisions: int = _Field(ge=0)
    stuck_recoveries: int = _Field(ge=0)
