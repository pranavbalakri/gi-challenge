"""Immutable semantic environment model contracts."""

from __future__ import annotations

from enum import StrEnum as _StrEnum

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint

__all__ = [
    "SEMANTIC_ID_PATTERN",
    "Vec3",
    "Transform3D",
    "ComponentKind",
    "SemanticComponent",
    "EnvironmentModel",
]

SEMANTIC_ID_PATTERN: str = r"^[a-z0-9][a-z0-9_.-]{0,127}$"


class Vec3(_BaseModel):
    """An immutable three-dimensional vector measured in metres."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    x: float = _Field(
        allow_inf_nan=False,
        json_schema_extra={"precision_places": 6},
    )
    y: float = _Field(
        allow_inf_nan=False,
        json_schema_extra={"precision_places": 6},
    )
    z: float = _Field(
        allow_inf_nan=False,
        json_schema_extra={"precision_places": 6},
    )


class Transform3D(_BaseModel):
    """An immutable origin and row-oriented 3x3 basis."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    origin: Vec3
    basis_x: Vec3
    basis_y: Vec3
    basis_z: Vec3


class ComponentKind(_StrEnum):
    """Semantic role of an environment component."""

    SURFACE = "surface"
    CONNECTOR = "connector"
    STRUCTURE = "structure"
    PROP = "prop"
    DYNAMIC_ENTITY = "dynamic_entity"
    PRESENTATION = "presentation"


class SemanticComponent(_BaseModel):
    """One identified component in an environment's semantic graph."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    semantic_id: str = _Field(pattern=SEMANTIC_ID_PATTERN)
    kind: ComponentKind
    payload: dict[str, object] = {}


class EnvironmentModel(_BaseModel):
    """A canonical immutable semantic environment graph."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    name: str = _Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    style: str = _Field(min_length=1, max_length=64)
    seed: int = _Field(ge=0)
    sdk_version: str = _Field(min_length=1, max_length=64)
    components: tuple[SemanticComponent, ...]
    model_fingerprint: str = ""

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
    def _validate_and_fingerprint(self) -> EnvironmentModel:
        semantic_ids = [component.semantic_id for component in self.components]
        if len(set(semantic_ids)) != len(semantic_ids):
            raise ValueError("component semantic_ids must be unique")

        fingerprint = _canonical_fingerprint(
            {
                "name": self.name,
                "style": self.style,
                "seed": self.seed,
                "sdk_version": self.sdk_version,
                "components": self.components,
            }
        )
        if self.model_fingerprint == "":
            return self.model_copy(update={"model_fingerprint": fingerprint})
        if self.model_fingerprint != fingerprint:
            raise ValueError("model_fingerprint mismatch")
        return self
