"""Mutable authoring surface for EnvMaker environment programs."""

from __future__ import annotations

from collections.abc import Sequence as _Sequence
import math as _math
import re as _re

from envmaker.core.model import ComponentKind as _ComponentKind
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.model import SemanticComponent as _SemanticComponent
from envmaker.sdk.footprints import Polygon2D as _Polygon2D
from envmaker.sdk.footprints import polygon_area as _polygon_area
from envmaker.sdk.footprints import polygon_bounds as _polygon_bounds
from envmaker.sdk.footprints import polygon_contains as _polygon_contains
from envmaker.sdk.kits import CURATED_MATERIALS as _CURATED_MATERIALS
from envmaker.sdk.kits import get_kit as _get_kit

__all__ = ["SDK_VERSION", "EnvironmentBuilder"]

SDK_VERSION: str = "0.1.0"

_NAME_PATTERN = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_Point = tuple[float, float]
_MAX_MAGNITUDE = 10000.0
_SPAWN_MARGIN = 0.4


def _require_finite(value: float, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{label} must be finite")
    number = float(value)
    if not _math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if abs(number) > _MAX_MAGNITUDE:
        raise ValueError(f"{label} magnitude must be at most {_MAX_MAGNITUDE:g}")
    return number


def _require_point(point: _Point, label: str) -> _Point:
    if not isinstance(point, tuple) or len(point) != 2:
        raise ValueError(f"{label} must be a pair of finite numbers")
    return (
        _require_finite(point[0], f"{label}[0]"),
        _require_finite(point[1], f"{label}[1]"),
    )


def _require_material(material: str) -> str:
    if material not in _CURATED_MATERIALS:
        raise ValueError(f"unknown material: {material}")
    return material


def _require_name(name: str) -> str:
    if _NAME_PATTERN.fullmatch(name) is None:
        raise ValueError(f"invalid name: {name}")
    return name


def _require_footprint(footprint: object, label: str = "footprint") -> _Polygon2D:
    if not isinstance(footprint, _Polygon2D):
        raise ValueError(f"{label} must be a Polygon2D")
    for index, (x, z) in enumerate(footprint.points):
        _require_finite(x, f"{label}[{index}][0]")
        _require_finite(z, f"{label}[{index}][1]")
    return footprint


def _footprint_payload(footprint: _Polygon2D) -> list[list[float]]:
    return [[float(x), float(z)] for x, z in footprint.points]


def _point_in_wall_rect(
    x: float,
    z: float,
    start: _Point,
    end: _Point,
    thickness: float,
) -> bool:
    x0, z0 = start
    x1, z1 = end
    dx = x1 - x0
    dz = z1 - z0
    length = _math.hypot(dx, dz)
    if length == 0.0:
        return False
    along_x = dx / length
    along_z = dz / length
    across_x = -along_z
    across_z = along_x
    rel_x = x - x0
    rel_z = z - z0
    along = rel_x * along_x + rel_z * along_z
    across = rel_x * across_x + rel_z * across_z
    return 0.0 <= along <= length and abs(across) <= thickness / 2.0


def _spawn_hits_blocker(
    spawn: _Point,
    components: _Sequence[_SemanticComponent],
) -> bool:
    spawn_x, spawn_z = spawn
    for component in components:
        discriminator = component.payload.get("component")
        if discriminator in {"water", "obstacle", "structure"}:
            footprint = _Polygon2D(
                [
                    (float(x), float(z))
                    for x, z in component.payload["footprint"]  # type: ignore[index]
                ]
            )
            if _polygon_contains(footprint, spawn_x, spawn_z):
                return True
        elif discriminator == "wall":
            start = (
                float(component.payload["start"][0]),  # type: ignore[index]
                float(component.payload["start"][1]),  # type: ignore[index]
            )
            end = (
                float(component.payload["end"][0]),  # type: ignore[index]
                float(component.payload["end"][1]),  # type: ignore[index]
            )
            thickness = float(component.payload["thickness"])  # type: ignore[arg-type]
            if _point_in_wall_rect(spawn_x, spawn_z, start, end, thickness):
                return True
    return False


class EnvironmentBuilder:
    """Mutable authoring surface that freezes into an EnvironmentModel."""

    def __init__(
        self,
        name: str,
        *,
        style: str = "flat-shaded minimal",
        seed: int = 0,
    ) -> None:
        if _NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid name: {name}")
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if not isinstance(style, str) or not style or len(style) > 64:
            raise ValueError("style must be a non-empty string up to 64 characters")

        self._name = name
        self._style = style
        self._seed = seed
        self._frozen = False
        self._names: set[str] = set()
        self._components: list[_SemanticComponent] = []
        self._ground_footprint: _Polygon2D | None = None
        self._has_ground = False
        self._has_spawn = False
        self._has_camera = False
        self._spawn_position: _Point | None = None

    def _ensure_mutable(self) -> None:
        if self._frozen:
            raise ValueError("builder is frozen")

    def _claim_name(self, name: str) -> str:
        validated = _require_name(name)
        if validated in self._names or validated == "camera":
            raise ValueError(f"duplicate name: {validated}")
        self._names.add(validated)
        return validated

    def _append(
        self,
        semantic_id: str,
        kind: _ComponentKind,
        payload: dict[str, object],
    ) -> EnvironmentBuilder:
        self._components.append(
            _SemanticComponent(
                semantic_id=semantic_id,
                kind=kind,
                payload=payload,
            )
        )
        return self

    def ground(
        self,
        name: str,
        *,
        footprint: _Polygon2D,
        material: str,
    ) -> EnvironmentBuilder:
        """Declare the unique ground surface.

        The footprint must be an axis-aligned rectangle (exactly four vertices
        whose area equals the axis-aligned bounding-box area). Non-rectangular
        grounds are rejected because the compiler materializes the AABB as the
        walkable plane.
        """

        self._ensure_mutable()
        if self._has_ground:
            raise ValueError("ground already declared")
        footprint = _require_footprint(footprint)
        if len(footprint.points) != 4:
            raise ValueError("ground footprint must be an axis-aligned rectangle")
        min_x, min_z, max_x, max_z = _polygon_bounds(footprint)
        bounds_area = (max_x - min_x) * (max_z - min_z)
        if abs(_polygon_area(footprint) - bounds_area) >= 1e-9:
            raise ValueError("ground footprint must be an axis-aligned rectangle")
        material = _require_material(material)
        semantic_id = self._claim_name(name)
        self._has_ground = True
        self._ground_footprint = footprint
        return self._append(
            semantic_id,
            _ComponentKind.SURFACE,
            {
                "component": "ground",
                "footprint": _footprint_payload(footprint),
                "material": material,
            },
        )

    def path(
        self,
        name: str,
        *,
        points: _Sequence[_Point],
        width: float,
        material: str,
    ) -> EnvironmentBuilder:
        """Declare a visual-only path ribbon."""

        self._ensure_mutable()
        material = _require_material(material)
        width = _require_finite(width, "width")
        if width <= 0.0:
            raise ValueError("width must be positive")
        if len(points) < 2:
            raise ValueError("path requires at least 2 points")
        validated_points: list[_Point] = []
        for index, point in enumerate(points):
            validated = _require_point(point, f"points[{index}]")
            if validated_points and validated == validated_points[-1]:
                raise ValueError("path consecutive points must be distinct")
            validated_points.append(validated)
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.PRESENTATION,
            {
                "component": "path",
                "points": [[x, z] for x, z in validated_points],
                "width": width,
                "material": material,
            },
        )

    def water(self, name: str, *, footprint: _Polygon2D) -> EnvironmentBuilder:
        """Declare a non-walkable water region."""

        self._ensure_mutable()
        footprint = _require_footprint(footprint)
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.PROP,
            {
                "component": "water",
                "footprint": _footprint_payload(footprint),
                "material": "water",
            },
        )

    def wall(
        self,
        name: str,
        *,
        start: _Point,
        end: _Point,
        height: float,
        thickness: float,
        material: str,
    ) -> EnvironmentBuilder:
        """Declare a blocking wall segment."""

        self._ensure_mutable()
        material = _require_material(material)
        start_point = _require_point(start, "start")
        end_point = _require_point(end, "end")
        if start_point == end_point:
            raise ValueError("wall start and end must be distinct")
        height = _require_finite(height, "height")
        thickness = _require_finite(thickness, "thickness")
        if height <= 0.0:
            raise ValueError("height must be positive")
        if thickness <= 0.0:
            raise ValueError("thickness must be positive")
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.PROP,
            {
                "component": "wall",
                "start": [start_point[0], start_point[1]],
                "end": [end_point[0], end_point[1]],
                "height": height,
                "thickness": thickness,
                "material": material,
            },
        )

    def obstacle(
        self,
        name: str,
        *,
        footprint: _Polygon2D,
        height: float,
        material: str,
    ) -> EnvironmentBuilder:
        """Declare a blocking extruded obstacle."""

        self._ensure_mutable()
        footprint = _require_footprint(footprint)
        material = _require_material(material)
        height = _require_finite(height, "height")
        if height <= 0.0:
            raise ValueError("height must be positive")
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.PROP,
            {
                "component": "obstacle",
                "footprint": _footprint_payload(footprint),
                "height": height,
                "material": material,
            },
        )

    def structure(
        self,
        name: str,
        *,
        footprint: _Polygon2D,
        height: float,
        kit: str,
    ) -> EnvironmentBuilder:
        """Declare a structure assembled from a curated kit."""

        self._ensure_mutable()
        footprint = _require_footprint(footprint)
        height = _require_finite(height, "height")
        if height <= 0.0:
            raise ValueError("height must be positive")
        kit_obj = _get_kit(kit)
        if kit_obj.category != "structure":
            raise ValueError(f"kit category must be structure: {kit}")
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.STRUCTURE,
            {
                "component": "structure",
                "footprint": _footprint_payload(footprint),
                "height": height,
                "kit": kit,
            },
        )

    def landmark(
        self,
        name: str,
        *,
        position: _Point,
        kit: str,
    ) -> EnvironmentBuilder:
        """Declare a non-blocking landmark kit placement."""

        self._ensure_mutable()
        position_point = _require_point(position, "position")
        kit_obj = _get_kit(kit)
        if kit_obj.category != "landmark":
            raise ValueError(f"kit category must be landmark: {kit}")
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.PRESENTATION,
            {
                "component": "landmark",
                "position": [position_point[0], position_point[1]],
                "kit": kit,
            },
        )

    def scatter(
        self,
        name: str,
        *,
        kit: str,
        count: int,
        min_spacing: float,
    ) -> EnvironmentBuilder:
        """Declare a seeded vegetation scatter over the ground footprint."""

        self._ensure_mutable()
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or count > 512
        ):
            raise ValueError("count must be an integer between 1 and 512")
        min_spacing = _require_finite(min_spacing, "min_spacing")
        if min_spacing <= 0.0:
            raise ValueError("min_spacing must be positive")
        kit_obj = _get_kit(kit)
        if kit_obj.category != "vegetation":
            raise ValueError(f"kit category must be vegetation: {kit}")
        semantic_id = self._claim_name(name)
        return self._append(
            semantic_id,
            _ComponentKind.PROP,
            {
                "component": "scatter",
                "kit": kit,
                "count": count,
                "min_spacing": min_spacing,
            },
        )

    def spawn(self, name: str, *, position: _Point) -> EnvironmentBuilder:
        """Declare the unique agent spawn point."""

        self._ensure_mutable()
        if self._has_spawn:
            raise ValueError("spawn already declared")
        position_point = _require_point(position, "position")
        semantic_id = self._claim_name(name)
        self._has_spawn = True
        self._spawn_position = position_point
        return self._append(
            semantic_id,
            _ComponentKind.DYNAMIC_ENTITY,
            {
                "component": "spawn",
                "position": [position_point[0], position_point[1]],
            },
        )

    def camera(self, *, orthographic_size: float) -> EnvironmentBuilder:
        """Declare the unique isometric camera configuration."""

        self._ensure_mutable()
        if self._has_camera:
            raise ValueError("camera already declared")
        orthographic_size = _require_finite(orthographic_size, "orthographic_size")
        if orthographic_size < 4.0 or orthographic_size > 100.0:
            raise ValueError("orthographic_size must be between 4.0 and 100.0")
        self._has_camera = True
        return self._append(
            "camera",
            _ComponentKind.PRESENTATION,
            {
                "component": "camera",
                "orthographic_size": orthographic_size,
            },
        )

    def freeze(self) -> _EnvironmentModel:
        """Validate and return an immutable EnvironmentModel."""

        self._ensure_mutable()
        missing: list[str] = []
        if not self._has_ground:
            missing.append("ground")
        if not self._has_spawn:
            missing.append("spawn")
        if not self._has_camera:
            missing.append("camera")
        if missing:
            raise ValueError(f"missing required declarations: {', '.join(missing)}")

        assert self._ground_footprint is not None
        assert self._spawn_position is not None
        spawn_x, spawn_z = self._spawn_position
        for dx, dz in (
            (_SPAWN_MARGIN, _SPAWN_MARGIN),
            (_SPAWN_MARGIN, -_SPAWN_MARGIN),
            (-_SPAWN_MARGIN, _SPAWN_MARGIN),
            (-_SPAWN_MARGIN, -_SPAWN_MARGIN),
        ):
            if not _polygon_contains(
                self._ground_footprint, spawn_x + dx, spawn_z + dz
            ):
                raise ValueError("spawn must lie on the ground footprint")

        if _spawn_hits_blocker(self._spawn_position, self._components):
            raise ValueError("spawn intersects a blocker")

        model = _EnvironmentModel(
            name=self._name,
            style=self._style,
            seed=self._seed,
            sdk_version=SDK_VERSION,
            components=tuple(self._components),
        )
        self._frozen = True
        return model
