"""Immutable engine-facing scene and candidate contracts."""

from __future__ import annotations

import math as _math
from enum import StrEnum as _StrEnum
from typing import Annotated as _Annotated
from typing import Literal as _Literal

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import ArtifactManifest as _ArtifactManifest
from envmaker.core.artifacts import ArtifactRef as _ArtifactRef
from envmaker.core.artifacts import ManifestLookupError as _ManifestLookupError
from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.model import SEMANTIC_ID_PATTERN as _SEMANTIC_ID_PATTERN
from envmaker.core.model import Transform3D as _Transform3D
from envmaker.core.signals import Signal as _Signal

__all__ = [
    "ISO_YAW_DEGREES",
    "ISO_PITCH_DEGREES",
    "ColliderShape",
    "ColliderSpec",
    "CameraSpec",
    "BoxVisual",
    "CylinderVisual",
    "PlaneVisual",
    "SphereVisual",
    "RibbonVisual",
    "PrimitiveVisual",
    "SceneNode",
    "GodotSceneSpec",
    "CandidateScene",
]

ISO_YAW_DEGREES: float = 45.0
ISO_PITCH_DEGREES: float = 35.264


class ColliderShape(_StrEnum):
    """Collision representation used for a scene node."""

    BOX = "box"
    CAPSULE = "capsule"
    CONVEX = "convex"
    TRIMESH = "trimesh"
    CYLINDER = "cylinder"


class ColliderSpec(_BaseModel):
    """A bounded collider shape and its positive dimensions."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: ColliderShape
    dimensions: dict[str, float] = {}

    @_model_validator(mode="after")
    def _validate_dimensions(self) -> ColliderSpec:
        if len(self.dimensions) > 16:
            raise ValueError("dimensions must contain at most 16 entries")
        for key, value in self.dimensions.items():
            if not key:
                raise ValueError("dimension keys must not be empty")
            if len(key) > 32:
                raise ValueError("dimension keys must be at most 32 characters")
            if not _math.isfinite(value) or value <= 0:
                raise ValueError("dimension values must be finite and positive")
        return self


class CameraSpec(_BaseModel):
    """Fixed-isometric camera configuration."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    follow_semantic_id: str = _Field(pattern=_SEMANTIC_ID_PATTERN)
    orthographic_size: float = _Field(
        gt=0,
        allow_inf_nan=False,
        json_schema_extra={"precision_places": 3},
    )
    fade_occluders: bool = True


class BoxVisual(_BaseModel):
    """A box primitive visual."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: _Literal["box"] = "box"
    size: tuple[float, float, float]
    material: str = "default"

    @_model_validator(mode="after")
    def _validate_size(self) -> BoxVisual:
        if any(not _math.isfinite(value) or value <= 0 for value in self.size):
            raise ValueError("size components must be finite and positive")
        return self


class CylinderVisual(_BaseModel):
    """A cylinder primitive visual.

    Optional ``top_radius`` enables cones (0) and truncated cones; omitted
    means equal to ``radius`` (a plain cylinder). Omitted None is fingerprint-
    stable via omit_when_none.
    """

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: _Literal["cylinder"] = "cylinder"
    radius: float = _Field(gt=0, allow_inf_nan=False)
    height: float = _Field(gt=0, allow_inf_nan=False)
    top_radius: float | None = _Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
        json_schema_extra={"omit_when_none": True},
    )
    material: str = "default"


class PlaneVisual(_BaseModel):
    """A plane primitive visual."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: _Literal["plane"] = "plane"
    size_x: float = _Field(gt=0, allow_inf_nan=False)
    size_z: float = _Field(gt=0, allow_inf_nan=False)
    material: str = "default"


class SphereVisual(_BaseModel):
    """A sphere primitive visual."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: _Literal["sphere"] = "sphere"
    radius: float = _Field(gt=0, allow_inf_nan=False)
    material: str = "default"


class RibbonVisual(_BaseModel):
    """A flat ribbon path visual through XZ points.

    Visual-only by doctrine: the compiler never pairs a ribbon with a collider.
    """

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: _Literal["ribbon"] = "ribbon"
    points: tuple[tuple[float, float], ...]
    width: float = _Field(gt=0, allow_inf_nan=False)
    material: str = "default"

    @_model_validator(mode="after")
    def _validate_points(self) -> RibbonVisual:
        if len(self.points) < 2:
            raise ValueError("points must contain at least 2 entries")
        for point in self.points:
            if any(not _math.isfinite(component) for component in point):
                raise ValueError("point components must be finite")
        return self


PrimitiveVisual = _Annotated[
    BoxVisual | CylinderVisual | PlaneVisual | SphereVisual | RibbonVisual,
    _Field(discriminator="shape"),
]


class SceneNode(_BaseModel):
    """One transformed semantic node in an engine-facing scene."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    node_id: str = _Field(pattern=_SEMANTIC_ID_PATTERN)
    semantic_id: str = _Field(pattern=_SEMANTIC_ID_PATTERN)
    transform: _Transform3D
    mesh: _ArtifactRef | None = None
    collider: ColliderSpec | None = None
    navmesh_contributor: bool = False
    fade_group: str = _Field(default="", max_length=128)
    visual: PrimitiveVisual | None = _Field(
        default=None,
        json_schema_extra={"omit_when_none": True},
    )


class GodotSceneSpec(_BaseModel):
    """A validated Godot scene wiring specification."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    nodes: tuple[SceneNode, ...]
    camera: CameraSpec
    controller_semantic_id: str = _Field(pattern=_SEMANTIC_ID_PATTERN)

    @_model_validator(mode="after")
    def _validate_scene_wiring(self) -> GodotSceneSpec:
        node_ids = [node.node_id for node in self.nodes]
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node_ids must be unique")

        semantic_ids = {node.semantic_id for node in self.nodes}
        if self.camera.follow_semantic_id not in semantic_ids:
            raise ValueError("camera follow target not in scene")
        if self.controller_semantic_id not in semantic_ids:
            raise ValueError("controller target not in scene")
        return self


class CandidateScene(_BaseModel):
    """An unaccepted scene candidate and its artifact inventory."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    scene: GodotSceneSpec
    manifest: _ArtifactManifest
    pre_reports: tuple[_Signal, ...] = ()
    candidate_fingerprint: str = ""

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
    def _validate_and_fingerprint(self) -> CandidateScene:
        for node in self.scene.nodes:
            if node.mesh is None:
                continue
            try:
                manifested_ref = self.manifest.get(node.mesh.path)
            except _ManifestLookupError:
                raise ValueError("unmanifested mesh artifact") from None
            if manifested_ref != node.mesh:
                raise ValueError("unmanifested mesh artifact")

        fingerprint = _canonical_fingerprint(
            {
                "scene": self.scene,
                "manifest": self.manifest,
            }
        )
        if self.candidate_fingerprint == "":
            return self.model_copy(update={"candidate_fingerprint": fingerprint})
        if self.candidate_fingerprint != fingerprint:
            raise ValueError("candidate_fingerprint mismatch")
        return self
