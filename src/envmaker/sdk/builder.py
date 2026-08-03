"""Mutable authoring surface for EnvMaker environment programs."""

from __future__ import annotations

from collections.abc import Sequence as _Sequence
import math as _math
import re as _re
from typing import Any as _Any

from envmaker.core.model import ComponentKind as _ComponentKind
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.model import SemanticComponent as _SemanticComponent
from envmaker.sdk.footprints import Polygon2D as _Polygon2D
from envmaker.sdk.footprints import polygon_area as _polygon_area
from envmaker.sdk.footprints import polygon_bounds as _polygon_bounds
from envmaker.sdk.footprints import polygon_contains as _polygon_contains
from envmaker.sdk.kits import CURATED_MATERIALS as _CURATED_MATERIALS
from envmaker.sdk.kits import Kit as _Kit
from envmaker.sdk.kits import KITS as _KITS
from envmaker.sdk.kits import MATERIAL_NAME_PATTERN as _MATERIAL_NAME_PATTERN
from envmaker.sdk.kits import MAX_CUSTOM_KIT_PARTS as _MAX_CUSTOM_KIT_PARTS
from envmaker.sdk.kits import MAX_CUSTOM_KITS as _MAX_CUSTOM_KITS
from envmaker.sdk.kits import get_kit as _get_kit

__all__ = ["PALETTE_KEYS", "SDK_VERSION", "EnvironmentBuilder"]

SDK_VERSION: str = "0.1.0"

_NAME_PATTERN = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_Point = tuple[float, float]
_MAX_MAGNITUDE = 10000.0
_SPAWN_MARGIN = 0.4

_HEX_COLOR_INPUT = _re.compile(r"^#?([0-9a-fA-F]{6})$")
_MAX_CUSTOM_MATERIALS = 16
_MAX_EMISSION_STRENGTH = 4.0

PALETTE_KEYS: frozenset[str] = frozenset(
    {
        "ground",
        "path",
        "vegetation",
        "rock",
        "wood",
        "water",
        "snow",
        "structure",
        "accent",
        "sky_top",
        "sky_horizon",
        "sun",
    }
)


def _parse_color(value: object, label: str) -> str:
    """Normalize a color to canonical lowercase '#rrggbb' (no alpha)."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a '#RRGGBB' hex string")
    match = _HEX_COLOR_INPUT.fullmatch(value.strip())
    if match is None:
        raise ValueError(
            f"{label} must be a '#RRGGBB' hex string (no alpha): {value!r}"
        )
    return "#" + match.group(1).lower()


def _require_unit(value: float, label: str) -> float:
    number = _require_finite(value, label)
    if number < 0.0 or number > 1.0:
        raise ValueError(f"{label} must be between 0.0 and 1.0")
    return number


def _clamp(value: float, label: str, low: float, high: float) -> float:
    number = _require_finite(value, label)
    return min(max(number, low), high)


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


def _require_material(
    material: str,
    custom_materials: frozenset[str] | set[str] = frozenset(),
) -> str:
    if material not in _CURATED_MATERIALS and material not in custom_materials:
        raise ValueError(
            f"unknown material: {material} — use a curated material "
            f"({', '.join(sorted(_CURATED_MATERIALS))}) or declare it "
            "first with material(...)"
        )
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


def _prop_keepout_radius(kit: object, scale: float) -> float:
    """Conservative horizontal keep-out radius for a blocking prop placement."""

    reach = 0.0
    for part in kit.parts:  # type: ignore[attr-defined]
        if part.shape == "box":
            assert part.size is not None
            half_x = part.size[0] / 2.0
            half_z = part.size[2] / 2.0
        else:
            assert part.radius is not None
            half_x = half_z = float(part.radius)
        part_reach = max(
            abs(float(part.offset[0])) + half_x,
            abs(float(part.offset[2])) + half_z,
        )
        reach = max(reach, part_reach)
    return float(scale) * reach


def _normalize_yaw_degrees(yaw: float) -> float:
    normalized = _math.fmod(yaw, 360.0)
    if normalized < 0.0:
        normalized += 360.0
    return normalized


def _require_scale(scale: float, label: str = "scale") -> float:
    scale = _require_finite(scale, label)
    if scale < 0.5 or scale > 2.0:
        raise ValueError(f"{label} must be between 0.5 and 2.0")
    return scale


def _require_scale_range(
    scale_range: tuple[float, float],
) -> tuple[float, float]:
    if not isinstance(scale_range, tuple) or len(scale_range) != 2:
        raise ValueError("scale_range must be a (lo, hi) pair")
    lo = _require_finite(scale_range[0], "scale_range[0]")
    hi = _require_finite(scale_range[1], "scale_range[1]")
    if not (0.5 <= lo <= hi <= 2.0):
        raise ValueError("scale_range must satisfy 0.5 <= lo <= hi <= 2.0")
    return (lo, hi)


def _spawn_blocker_hit(
    spawn: _Point,
    components: _Sequence[_SemanticComponent],
    kit_resolver: _Any = None,
) -> str | None:
    """Return a located description of the blocker the spawn intersects.

    Freeze failures happen before any candidate exists, so this message is
    the model's ONLY signal for the repair; it must name the offender and
    its extent, not just the fact of the collision.
    """

    spawn_x, spawn_z = spawn
    for component in components:
        discriminator = component.payload.get("component")
        if discriminator in {"water", "obstacle", "structure"}:
            points = [
                (float(x), float(z))
                for x, z in component.payload["footprint"]  # type: ignore[index]
            ]
            footprint = _Polygon2D(points)
            if _polygon_contains(footprint, spawn_x, spawn_z):
                xs = [p[0] for p in points]
                zs = [p[1] for p in points]
                return (
                    f"'{component.semantic_id}' ({discriminator}, "
                    f"x {min(xs):.1f}..{max(xs):.1f}, "
                    f"z {min(zs):.1f}..{max(zs):.1f})"
                )
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
                return (
                    f"'{component.semantic_id}' (wall from "
                    f"({start[0]:.1f}, {start[1]:.1f}) to "
                    f"({end[0]:.1f}, {end[1]:.1f}), "
                    f"thickness {thickness:.1f})"
                )
        elif discriminator == "prop":
            kit_name = str(component.payload["kit"])
            resolve = kit_resolver if kit_resolver is not None else _get_kit
            kit_obj = resolve(kit_name)
            if not kit_obj.blocking:
                continue
            center = (
                float(component.payload["position"][0]),  # type: ignore[index]
                float(component.payload["position"][1]),  # type: ignore[index]
            )
            scale = float(component.payload["scale"])  # type: ignore[arg-type]
            radius = _prop_keepout_radius(kit_obj, scale)
            if _math.hypot(spawn_x - center[0], spawn_z - center[1]) <= radius:
                return (
                    f"'{component.semantic_id}' (prop, center "
                    f"({center[0]:.1f}, {center[1]:.1f}), radius {radius:.1f})"
                )
    return None


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
        self._ground_name: str | None = None
        self._has_ground = False
        self._has_spawn = False
        self._has_camera = False
        self._has_palette = False
        self._has_lighting = False
        self._spawn_position: _Point | None = None
        self._custom_materials: set[str] = set()
        self._custom_kits: dict[str, _Kit] = {}

    def _resolve_kit(self, name: str) -> _Kit:
        custom = self._custom_kits.get(name)
        if custom is not None:
            return custom
        return _get_kit(name)

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
        material = _require_material(material, self._custom_materials)
        semantic_id = self._claim_name(name)
        self._has_ground = True
        self._ground_name = semantic_id
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
        material = _require_material(material, self._custom_materials)
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
        material = _require_material(material, self._custom_materials)
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
        material = _require_material(material, self._custom_materials)
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
        kit_obj = self._resolve_kit(kit)
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
        kit_obj = self._resolve_kit(kit)
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

    def prop(
        self,
        name: str,
        *,
        kit: str,
        position: _Point,
        yaw: float = 0.0,
        scale: float = 1.0,
    ) -> EnvironmentBuilder:
        """Declare a direct landmark/vegetation kit placement with pose control."""

        self._ensure_mutable()
        position_point = _require_point(position, "position")
        yaw = _normalize_yaw_degrees(_require_finite(yaw, "yaw"))
        scale = _require_scale(scale)
        kit_obj = self._resolve_kit(kit)
        if kit_obj.category == "structure":
            raise ValueError(
                f"kit category must be landmark or vegetation (got structure); "
                f"use structure() for structure kits that need footprints"
            )
        if kit_obj.category not in {"landmark", "vegetation"}:
            raise ValueError(
                f"kit category must be landmark or vegetation: {kit}"
            )
        semantic_id = self._claim_name(name)
        kind = _ComponentKind.PROP if kit_obj.blocking else _ComponentKind.PRESENTATION
        return self._append(
            semantic_id,
            kind,
            {
                "component": "prop",
                "kit": kit,
                "position": [position_point[0], position_point[1]],
                "yaw_degrees": yaw,
                "scale": scale,
            },
        )

    def scatter(
        self,
        name: str,
        *,
        region: str,
        kit: str,
        count: int,
        min_spacing: float,
        yaw_jitter: bool = False,
        scale_range: tuple[float, float] | None = None,
    ) -> EnvironmentBuilder:
        """Declare a seeded vegetation scatter over the ground footprint.

        ``region`` must name the already-declared ground component. Scatter
        still samples over that ground footprint; declare ground before scatter.
        Optional ``yaw_jitter`` / ``scale_range`` are model-controlled only.
        """

        self._ensure_mutable()
        if region != self._ground_name:
            raise ValueError(f"unknown region: {region}")
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
        if not isinstance(yaw_jitter, bool):
            raise ValueError("yaw_jitter must be a bool")
        validated_range: tuple[float, float] | None = None
        if scale_range is not None:
            validated_range = _require_scale_range(scale_range)
        kit_obj = self._resolve_kit(kit)
        if kit_obj.category != "vegetation":
            raise ValueError(f"kit category must be vegetation: {kit}")
        semantic_id = self._claim_name(name)
        payload: dict[str, object] = {
            "component": "scatter",
            "region": region,
            "kit": kit,
            "count": count,
            "min_spacing": min_spacing,
        }
        if yaw_jitter:
            payload["yaw_jitter"] = True
        if validated_range is not None:
            payload["scale_range"] = [validated_range[0], validated_range[1]]
        return self._append(
            semantic_id,
            _ComponentKind.PROP,
            payload,
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

    def material(
        self,
        name: str,
        *,
        color: str,
        emission_color: str | None = None,
        emission_strength: float | None = None,
        roughness: float | None = None,
        metallic: float | None = None,
    ) -> EnvironmentBuilder:
        """Declare a bounded custom material (max 16 per environment).

        Colors are '#RRGGBB' hex (no alpha/transparency). Emission strength
        is limited to 0..4; roughness/metallic to 0..1. No shaders, textures,
        or arbitrary engine properties. Declare materials BEFORE using them.
        """

        self._ensure_mutable()
        if _MATERIAL_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError(f"invalid material name: {name}")
        if name in _CURATED_MATERIALS:
            raise ValueError(f"material name shadows a curated material: {name}")
        if name in self._custom_materials:
            raise ValueError(f"duplicate material: {name}")
        if len(self._custom_materials) >= _MAX_CUSTOM_MATERIALS:
            raise ValueError(
                f"at most {_MAX_CUSTOM_MATERIALS} custom materials per environment"
            )
        payload: dict[str, object] = {
            "component": "material",
            "color": _parse_color(color, "color"),
        }
        if emission_strength is not None and emission_color is None:
            raise ValueError("emission_strength requires emission_color")
        if emission_color is not None:
            payload["emission_color"] = _parse_color(
                emission_color, "emission_color"
            )
            strength = 1.0 if emission_strength is None else emission_strength
            strength = _require_finite(strength, "emission_strength")
            if strength < 0.0 or strength > _MAX_EMISSION_STRENGTH:
                raise ValueError(
                    "emission_strength must be between 0.0 and "
                    f"{_MAX_EMISSION_STRENGTH:g}"
                )
            payload["emission_strength"] = strength
        if roughness is not None:
            payload["roughness"] = _require_unit(roughness, "roughness")
        if metallic is not None:
            payload["metallic"] = _require_unit(metallic, "metallic")
        semantic_id = self._claim_name(name)
        self._custom_materials.add(name)
        return self._append(semantic_id, _ComponentKind.PRESENTATION, payload)

    def palette(self, **colors: str) -> EnvironmentBuilder:
        """Override the colors of semantic material categories (visuals only).

        Supported keys: ground, path, vegetation, rock, wood, water, snow,
        structure, accent (landmark kits), sky_top, sky_horizon, sun.
        Colliders and navigation are untouched; omitted keys keep defaults.
        """

        self._ensure_mutable()
        if self._has_palette:
            raise ValueError("palette already declared")
        if not colors:
            raise ValueError("palette requires at least one key")
        unknown = sorted(set(colors) - PALETTE_KEYS)
        if unknown:
            raise ValueError(
                f"unknown palette keys: {', '.join(unknown)} "
                f"(supported: {', '.join(sorted(PALETTE_KEYS))})"
            )
        payload: dict[str, object] = {"component": "palette"}
        for key in sorted(colors):
            payload[key] = _parse_color(colors[key], key)
        self._has_palette = True
        semantic_id = self._claim_name("palette")
        return self._append(semantic_id, _ComponentKind.PRESENTATION, payload)

    def lighting(
        self,
        *,
        ambient_color: str | None = None,
        ambient_energy: float | None = None,
        sun_color: str | None = None,
        sun_energy: float | None = None,
        sky_top: str | None = None,
        sky_horizon: str | None = None,
    ) -> EnvironmentBuilder:
        """Declare bounded environment lighting (energies clamped to 0..2)."""

        self._ensure_mutable()
        if self._has_lighting:
            raise ValueError("lighting already declared")
        payload: dict[str, object] = {"component": "lighting"}
        if ambient_color is not None:
            payload["ambient_color"] = _parse_color(ambient_color, "ambient_color")
        if ambient_energy is not None:
            payload["ambient_energy"] = _clamp(
                ambient_energy, "ambient_energy", 0.0, 2.0
            )
        if sun_color is not None:
            payload["sun_color"] = _parse_color(sun_color, "sun_color")
        if sun_energy is not None:
            payload["sun_energy"] = _clamp(sun_energy, "sun_energy", 0.0, 2.0)
        if sky_top is not None:
            payload["sky_top"] = _parse_color(sky_top, "sky_top")
        if sky_horizon is not None:
            payload["sky_horizon"] = _parse_color(sky_horizon, "sky_horizon")
        if len(payload) == 1:
            raise ValueError("lighting requires at least one setting")
        self._has_lighting = True
        semantic_id = self._claim_name("lighting")
        return self._append(semantic_id, _ComponentKind.PRESENTATION, payload)

    def custom_kit(
        self,
        name: str,
        *,
        category: str,
        blocking: bool,
        parts: _Sequence[object],
    ) -> EnvironmentBuilder:
        """Declare a small primitive assembly kit (max 12 per environment).

        ``parts`` are BoxPart/CylinderPart/ConePart/SpherePart instances
        (max 16 per kit, extents bounded to 8 m). Blocking kits receive
        conservative per-part colliders; nonblocking kits get none. Custom
        kits are used exactly like curated kits by prop/scatter/landmark/
        structure according to their category.
        """

        self._ensure_mutable()
        if len(self._custom_kits) >= _MAX_CUSTOM_KITS:
            raise ValueError(
                f"at most {_MAX_CUSTOM_KITS} custom kits per environment"
            )
        if name in _KITS:
            raise ValueError(f"kit name shadows a curated kit: {name}")
        if name in self._custom_kits:
            raise ValueError(f"duplicate kit: {name}")
        if category not in {"structure", "landmark", "vegetation"}:
            raise ValueError(
                f"category must be structure, landmark, or vegetation: {category}"
            )
        if not isinstance(blocking, bool):
            raise ValueError("blocking must be a bool")
        if not parts or len(parts) > _MAX_CUSTOM_KIT_PARTS:
            raise ValueError(
                f"parts must contain 1..{_MAX_CUSTOM_KIT_PARTS} entries"
            )
        kit_parts = []
        for index, part in enumerate(parts):
            to_kit_part = getattr(part, "to_kit_part", None)
            if to_kit_part is None:
                raise ValueError(
                    f"parts[{index}] must be a BoxPart, CylinderPart, "
                    "ConePart, or SpherePart"
                )
            kit_part = to_kit_part()
            _require_material(kit_part.material, self._custom_materials)
            kit_parts.append(kit_part)
        kit = _Kit(
            name=name,
            category=category,  # type: ignore[arg-type]
            blocking=blocking,
            parts=tuple(kit_parts),
        )
        semantic_id = self._claim_name(name)
        self._custom_kits[name] = kit
        part_payloads: list[dict[str, object]] = []
        for kit_part in kit_parts:
            entry: dict[str, object] = {
                "shape": kit_part.shape,
                "offset": [
                    kit_part.offset[0],
                    kit_part.offset[1],
                    kit_part.offset[2],
                ],
                "material": kit_part.material,
                "yaw_degrees": kit_part.yaw_degrees,
            }
            if kit_part.size is not None:
                entry["size"] = [
                    kit_part.size[0],
                    kit_part.size[1],
                    kit_part.size[2],
                ]
            if kit_part.radius is not None:
                entry["radius"] = kit_part.radius
            if kit_part.height is not None:
                entry["height"] = kit_part.height
            part_payloads.append(entry)
        return self._append(
            semantic_id,
            _ComponentKind.PRESENTATION,
            {
                "component": "custom_kit",
                "category": category,
                "blocking": blocking,
                "parts": part_payloads,
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
                ground_xs = [p[0] for p in self._ground_footprint.points]
                ground_zs = [p[1] for p in self._ground_footprint.points]
                raise ValueError(
                    "spawn must lie on the ground footprint with "
                    f"{_SPAWN_MARGIN} m clearance: "
                    f"spawn=({spawn_x:.1f}, {spawn_z:.1f}), ground x "
                    f"{min(ground_xs):.1f}..{max(ground_xs):.1f}, z "
                    f"{min(ground_zs):.1f}..{max(ground_zs):.1f}"
                )

        blocker_hit = _spawn_blocker_hit(
            self._spawn_position, self._components, self._resolve_kit
        )
        if blocker_hit is not None:
            raise ValueError(
                "spawn intersects a blocker: "
                f"spawn=({spawn_x:.1f}, {spawn_z:.1f}) is inside "
                f"{blocker_hit}; move the spawn at least "
                f"{_SPAWN_MARGIN} m clear of it"
            )

        model = _EnvironmentModel(
            name=self._name,
            style=self._style,
            seed=self._seed,
            sdk_version=SDK_VERSION,
            components=tuple(self._components),
        )
        self._frozen = True
        return model
