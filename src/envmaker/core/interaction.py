"""Immutable runtime interaction contracts."""

from __future__ import annotations

import math as _math
import re as _re
from enum import StrEnum as _StrEnum

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import ArtifactRef as _ArtifactRef
from envmaker.core.model import SEMANTIC_ID_PATTERN as _SEMANTIC_ID_PATTERN
from envmaker.core.model import Transform3D as _Transform3D
from envmaker.core.model import Vec3 as _Vec3

__all__ = [
    "ContactPoint",
    "WorldSnapshot",
    "ObservationKind",
    "ObservationPacket",
    "ControllerAction",
]


class ContactPoint(_BaseModel):
    """One semantic contact reported by the runtime."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    other_semantic_id: str = _Field(pattern=_SEMANTIC_ID_PATTERN)
    position: _Vec3
    normal: _Vec3


class WorldSnapshot(_BaseModel):
    """Versioned runtime state for one simulation tick."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    tick_id: int = _Field(ge=0)
    agent_transform: _Transform3D
    agent_velocity: _Vec3
    grounded: bool
    current_nav_region: str = _Field(default="", max_length=128)
    contacts: tuple[ContactPoint, ...] = ()
    visible_fade_groups: tuple[str, ...] = ()
    faded_groups: tuple[str, ...] = ()
    events: tuple[str, ...] = ()

    @_model_validator(mode="after")
    def _validate_bounds(self) -> WorldSnapshot:
        if len(self.contacts) > 32:
            raise ValueError("contacts must contain at most 32 entries")

        for field_name in ("visible_fade_groups", "faded_groups"):
            groups = getattr(self, field_name)
            if len(groups) > 128:
                raise ValueError(f"{field_name} must contain at most 128 entries")
            for group in groups:
                if not group:
                    raise ValueError(f"{field_name} entries must not be empty")
                if len(group) > 128:
                    raise ValueError(
                        f"{field_name} entries must be at most 128 characters"
                    )

        if len(self.events) > 64:
            raise ValueError("events must contain at most 64 entries")
        for event in self.events:
            if _re.fullmatch(r"[a-z][a-z0-9_.]{2,63}", event) is None:
                raise ValueError("event entries must match the event code pattern")

        return self


class ObservationKind(_StrEnum):
    """Kind of policy observation carried by a packet."""

    RGB_FRAME = "rgb_frame"
    LOCAL_SEMANTIC = "local_semantic"
    CONTROLLER_STATE = "controller_state"


class ObservationPacket(_BaseModel):
    """A bounded policy observation for one simulation tick."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    tick_id: int = _Field(ge=0)
    kind: ObservationKind
    frame: _ArtifactRef | None = None
    semantic: dict[str, object] = {}
    controller_state: dict[str, float] = {}

    @_model_validator(mode="after")
    def _validate_packet(self) -> ObservationPacket:
        if (self.kind is ObservationKind.RGB_FRAME) != (self.frame is not None):
            raise ValueError("frame presence must match observation kind")

        if len(self.semantic) > 128:
            raise ValueError("semantic must contain at most 128 entries")
        for key in self.semantic:
            if not key:
                raise ValueError("semantic keys must not be empty")
            if len(key) > 64:
                raise ValueError("semantic keys must be at most 64 characters")

        if len(self.controller_state) > 32:
            raise ValueError("controller_state must contain at most 32 entries")
        for value in self.controller_state.values():
            if not _math.isfinite(value):
                raise ValueError("controller_state values must be finite")

        return self


class ControllerAction(_BaseModel):
    """Camera-relative grounded planar movement for one tick."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    tick_id: int = _Field(ge=0)
    move_x: float = _Field(ge=-1.0, le=1.0, allow_inf_nan=False)
    move_z: float = _Field(ge=-1.0, le=1.0, allow_inf_nan=False)

    @_model_validator(mode="after")
    def _validate_planar_magnitude(self) -> ControllerAction:
        magnitude = _math.sqrt(self.move_x**2 + self.move_z**2)
        if magnitude > 1.000001:
            raise ValueError("planar move magnitude exceeds 1")
        return self
