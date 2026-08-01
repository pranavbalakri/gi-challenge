"""Compile an EnvironmentModel into a CandidateScene."""

from __future__ import annotations

import math as _math
import random as _random
from typing import Any as _Any

from envmaker.core.artifacts import ArtifactManifest as _ArtifactManifest
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.model import Transform3D as _Transform3D
from envmaker.core.model import Vec3 as _Vec3
from envmaker.core.scene_spec import BoxVisual as _BoxVisual
from envmaker.core.scene_spec import CameraSpec as _CameraSpec
from envmaker.core.scene_spec import CandidateScene as _CandidateScene
from envmaker.core.scene_spec import ColliderShape as _ColliderShape
from envmaker.core.scene_spec import ColliderSpec as _ColliderSpec
from envmaker.core.scene_spec import CylinderVisual as _CylinderVisual
from envmaker.core.scene_spec import GodotSceneSpec as _GodotSceneSpec
from envmaker.core.scene_spec import PlaneVisual as _PlaneVisual
from envmaker.core.scene_spec import SceneNode as _SceneNode
from envmaker.sdk.footprints import Polygon2D as _Polygon2D
from envmaker.sdk.footprints import min_area_obb as _min_area_obb
from envmaker.sdk.footprints import polygon_bounds as _polygon_bounds
from envmaker.sdk.footprints import polygon_contains as _polygon_contains
from envmaker.sdk.kits import Kit as _Kit
from envmaker.sdk.kits import KitPart as _KitPart
from envmaker.sdk.kits import get_kit as _get_kit

__all__ = ["compile_environment_model"]

_Point = tuple[float, float]


def _identity_basis() -> tuple[_Vec3, _Vec3, _Vec3]:
    return (
        _Vec3(x=1.0, y=0.0, z=0.0),
        _Vec3(x=0.0, y=1.0, z=0.0),
        _Vec3(x=0.0, y=0.0, z=1.0),
    )


def _yaw_basis(yaw: float) -> tuple[_Vec3, _Vec3, _Vec3]:
    cos_yaw = _math.cos(yaw)
    sin_yaw = _math.sin(yaw)
    return (
        _Vec3(x=cos_yaw, y=0.0, z=-sin_yaw),
        _Vec3(x=0.0, y=1.0, z=0.0),
        _Vec3(x=sin_yaw, y=0.0, z=cos_yaw),
    )


def _transform(
    origin: tuple[float, float, float],
    yaw: float | None = None,
) -> _Transform3D:
    if yaw is None:
        basis_x, basis_y, basis_z = _identity_basis()
    else:
        basis_x, basis_y, basis_z = _yaw_basis(yaw)
    return _Transform3D(
        origin=_Vec3(x=origin[0], y=origin[1], z=origin[2]),
        basis_x=basis_x,
        basis_y=basis_y,
        basis_z=basis_z,
    )


def _box_collider(size_x: float, size_y: float, size_z: float) -> _ColliderSpec:
    return _ColliderSpec(
        shape=_ColliderShape.BOX,
        dimensions={"x": size_x, "y": size_y, "z": size_z},
    )


def _cylinder_collider(radius: float, height: float) -> _ColliderSpec:
    return _ColliderSpec(
        shape=_ColliderShape.CYLINDER,
        dimensions={"radius": radius, "height": height},
    )


def _node(
    *,
    node_id: str,
    semantic_id: str,
    transform: _Transform3D,
    visual: _Any,
    collider: _ColliderSpec | None,
    navmesh_contributor: bool,
) -> _SceneNode:
    return _SceneNode(
        node_id=node_id,
        semantic_id=semantic_id,
        transform=transform,
        collider=collider,
        navmesh_contributor=navmesh_contributor,
        visual=visual,
    )


def _polygon_from_payload(payload: dict[str, _Any]) -> _Polygon2D:
    points = payload["footprint"]
    return _Polygon2D([(float(x), float(z)) for x, z in points])


def _rotate_offset(
    offset: tuple[float, float, float],
    yaw: float,
) -> tuple[float, float, float]:
    cos_yaw = _math.cos(yaw)
    sin_yaw = _math.sin(yaw)
    local_x, local_y, local_z = offset
    return (
        cos_yaw * local_x + sin_yaw * local_z,
        local_y,
        -sin_yaw * local_x + cos_yaw * local_z,
    )


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


def _part_visual(
    part: _KitPart,
    *,
    size: tuple[float, float, float] | None = None,
    radius: float | None = None,
    height: float | None = None,
) -> _Any:
    if part.shape == "box":
        assert size is not None
        return _BoxVisual(size=size, material=part.material)
    assert radius is not None and height is not None
    return _CylinderVisual(radius=radius, height=height, material=part.material)


def _dotted_id(name: str, *parts: int) -> str:
    # Godot strips '.' from tree node names; runtime resolution uses semantic_id
    # from the candidate JSON, so tree-name divergence from dotted ids is deliberate.
    return ".".join((name, *(str(part) for part in parts)))


def _compile_kit_parts(
    *,
    name: str,
    kit: _Kit,
    positions: list[tuple[float, float]],
    scale: tuple[float, float, float] | None,
    yaw: float,
    blocking: bool,
) -> list[_SceneNode]:
    nodes: list[_SceneNode] = []
    scale_x, scale_y, scale_z = scale if scale is not None else (1.0, 1.0, 1.0)
    for placement_index, (center_x, center_z) in enumerate(positions):
        for part_index, part in enumerate(kit.parts):
            if scale is None:
                if len(positions) == 1:
                    node_id = _dotted_id(name, part_index)
                else:
                    node_id = _dotted_id(name, placement_index, part_index)
                semantic_id = node_id
                world_offset = part.offset
                part_yaw = None
                if part.shape == "box":
                    assert part.size is not None
                    visual = _part_visual(part, size=part.size)
                    collider = (
                        _box_collider(part.size[0], part.size[1], part.size[2])
                        if blocking
                        else None
                    )
                else:
                    assert part.radius is not None and part.height is not None
                    visual = _part_visual(
                        part, radius=part.radius, height=part.height
                    )
                    collider = (
                        _cylinder_collider(part.radius, part.height)
                        if blocking
                        else None
                    )
            else:
                node_id = _dotted_id(name, part_index)
                semantic_id = node_id
                scaled_offset = (
                    part.offset[0] * scale_x,
                    part.offset[1] * scale_y,
                    part.offset[2] * scale_z,
                )
                world_offset = _rotate_offset(scaled_offset, yaw)
                part_yaw = yaw
                if part.shape == "box":
                    assert part.size is not None
                    size = (
                        part.size[0] * scale_x,
                        part.size[1] * scale_y,
                        part.size[2] * scale_z,
                    )
                    visual = _part_visual(part, size=size)
                    collider = (
                        _box_collider(size[0], size[1], size[2]) if blocking else None
                    )
                else:
                    assert part.radius is not None and part.height is not None
                    radius = part.radius * min(scale_x, scale_z)
                    height = part.height * scale_y
                    visual = _part_visual(part, radius=radius, height=height)
                    collider = (
                        _cylinder_collider(radius, height) if blocking else None
                    )

            origin = (
                center_x + world_offset[0],
                world_offset[1],
                center_z + world_offset[2],
            )
            nodes.append(
                _node(
                    node_id=node_id,
                    semantic_id=semantic_id,
                    transform=_transform(origin, part_yaw),
                    visual=visual,
                    collider=collider,
                    navmesh_contributor=blocking,
                )
            )
    return nodes


def _compile_ground(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    footprint = _polygon_from_payload(payload)
    min_x, min_z, max_x, max_z = _polygon_bounds(footprint)
    size_x = max_x - min_x
    size_z = max_z - min_z
    center_x = (min_x + max_x) / 2.0
    center_z = (min_z + max_z) / 2.0
    material = str(payload["material"])
    return [
        _node(
            node_id=name,
            semantic_id=name,
            transform=_transform((center_x, 0.0, center_z)),
            visual=_PlaneVisual(size_x=size_x, size_z=size_z, material=material),
            collider=_box_collider(size_x, 0.5, size_z),
            navmesh_contributor=True,
        )
    ]


def _compile_path(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    points = [(float(x), float(z)) for x, z in payload["points"]]
    width = float(payload["width"])
    material = str(payload["material"])
    nodes: list[_SceneNode] = []
    for index, ((x0, z0), (x1, z1)) in enumerate(zip(points, points[1:])):
        dx = x1 - x0
        dz = z1 - z0
        length = _math.hypot(dx, dz)
        yaw = _math.atan2(-dz, dx)
        mid_x = (x0 + x1) / 2.0
        mid_z = (z0 + z1) / 2.0
        node_id = _dotted_id(name, index)
        nodes.append(
            _node(
                node_id=node_id,
                semantic_id=node_id,
                transform=_transform((mid_x, 0.01, mid_z), yaw),
                visual=_BoxVisual(size=(length, 0.02, width), material=material),
                collider=None,
                navmesh_contributor=False,
            )
        )
    return nodes


def _compile_water(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    fit = _min_area_obb(_polygon_from_payload(payload))
    center_x, center_z = fit.center
    yaw = -fit.yaw
    return [
        _node(
            node_id=name,
            semantic_id=name,
            transform=_transform((center_x, -0.05, center_z), yaw),
            visual=_BoxVisual(
                size=(fit.size_x, 0.3, fit.size_z), material="water"
            ),
            collider=None,
            navmesh_contributor=False,
        ),
        _node(
            node_id=_dotted_id(name, 0),
            semantic_id=_dotted_id(name, 0),
            transform=_transform((center_x, 0.25, center_z), yaw),
            visual=None,
            collider=_box_collider(fit.size_x, 1.5, fit.size_z),
            navmesh_contributor=True,
        ),
    ]


def _compile_wall(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    x0, z0 = float(payload["start"][0]), float(payload["start"][1])
    x1, z1 = float(payload["end"][0]), float(payload["end"][1])
    height = float(payload["height"])
    thickness = float(payload["thickness"])
    material = str(payload["material"])
    dx = x1 - x0
    dz = z1 - z0
    length = _math.hypot(dx, dz)
    yaw = _math.atan2(-dz, dx)
    mid_x = (x0 + x1) / 2.0
    mid_z = (z0 + z1) / 2.0
    return [
        _node(
            node_id=name,
            semantic_id=name,
            transform=_transform((mid_x, height / 2.0, mid_z), yaw),
            visual=_BoxVisual(size=(length, height, thickness), material=material),
            collider=_box_collider(length, height, thickness),
            navmesh_contributor=True,
        )
    ]


def _compile_obstacle(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    fit = _min_area_obb(_polygon_from_payload(payload))
    height = float(payload["height"])
    material = str(payload["material"])
    center_x, center_z = fit.center
    yaw = -fit.yaw
    return [
        _node(
            node_id=name,
            semantic_id=name,
            transform=_transform((center_x, height / 2.0, center_z), yaw),
            visual=_BoxVisual(
                size=(fit.size_x, height, fit.size_z), material=material
            ),
            collider=_box_collider(fit.size_x, height, fit.size_z),
            navmesh_contributor=True,
        )
    ]


def _compile_structure(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    fit = _min_area_obb(_polygon_from_payload(payload))
    height = float(payload["height"])
    kit = _get_kit(str(payload["kit"]))
    center_x, center_z = fit.center
    yaw = -fit.yaw
    return _compile_kit_parts(
        name=name,
        kit=kit,
        positions=[(center_x, center_z)],
        scale=(fit.size_x, height, fit.size_z),
        yaw=yaw,
        blocking=kit.blocking,
    )


def _compile_landmark(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    position = (float(payload["position"][0]), float(payload["position"][1]))
    kit = _get_kit(str(payload["kit"]))
    return _compile_kit_parts(
        name=name,
        kit=kit,
        positions=[position],
        scale=None,
        yaw=0.0,
        blocking=False,
    )


def _extract_blockers(
    components: tuple[_Any, ...],
) -> tuple[list[_Polygon2D], list[tuple[_Point, _Point, float]], _Point | None]:
    polygons: list[_Polygon2D] = []
    walls: list[tuple[_Point, _Point, float]] = []
    spawn: _Point | None = None
    for component in components:
        discriminator = component.payload.get("component")
        if discriminator in {"water", "obstacle", "structure"}:
            polygons.append(_polygon_from_payload(component.payload))
        elif discriminator == "wall":
            start = (
                float(component.payload["start"][0]),
                float(component.payload["start"][1]),
            )
            end = (
                float(component.payload["end"][0]),
                float(component.payload["end"][1]),
            )
            thickness = float(component.payload["thickness"])
            walls.append((start, end, thickness))
        elif discriminator == "spawn":
            spawn = (
                float(component.payload["position"][0]),
                float(component.payload["position"][1]),
            )
    return polygons, walls, spawn


def _sample_scatter_points(
    *,
    seed: int,
    name: str,
    footprint: _Polygon2D,
    count: int,
    min_spacing: float,
    blocker_polygons: list[_Polygon2D],
    walls: list[tuple[_Point, _Point, float]],
    spawn: _Point | None,
) -> list[tuple[float, float]]:
    rng = _random.Random(f"{seed}:{name}")
    min_x, min_z, max_x, max_z = _polygon_bounds(footprint)
    accepted: list[tuple[float, float]] = []
    max_attempts = 200 * count
    attempts = 0
    while len(accepted) < count and attempts < max_attempts:
        attempts += 1
        x = rng.uniform(min_x, max_x)
        z = rng.uniform(min_z, max_z)
        if not _polygon_contains(footprint, x, z):
            continue
        if any(_polygon_contains(blocker, x, z) for blocker in blocker_polygons):
            continue
        if any(
            _point_in_wall_rect(x, z, start, end, thickness)
            for start, end, thickness in walls
        ):
            continue
        if spawn is not None and _math.hypot(x - spawn[0], z - spawn[1]) < 1.0:
            continue
        if any(
            _math.hypot(x - other_x, z - other_z) < min_spacing
            for other_x, other_z in accepted
        ):
            continue
        accepted.append((x, z))
    if len(accepted) < count:
        raise ValueError(
            f"scatter '{name}' placed {len(accepted)} of {count} requested"
        )
    return accepted


def _compile_scatter(
    name: str,
    payload: dict[str, _Any],
    *,
    seed: int,
    ground_name: str,
    ground_footprint: _Polygon2D,
    blocker_polygons: list[_Polygon2D],
    walls: list[tuple[_Point, _Point, float]],
    spawn: _Point | None,
) -> list[_SceneNode]:
    region = str(payload["region"])
    if region != ground_name:
        raise ValueError(f"unknown region: {region}")
    kit = _get_kit(str(payload["kit"]))
    count = int(payload["count"])
    min_spacing = float(payload["min_spacing"])
    points = _sample_scatter_points(
        seed=seed,
        name=name,
        footprint=ground_footprint,
        count=count,
        min_spacing=min_spacing,
        blocker_polygons=blocker_polygons,
        walls=walls,
        spawn=spawn,
    )
    return _compile_kit_parts(
        name=name,
        kit=kit,
        positions=points,
        scale=None,
        yaw=0.0,
        blocking=kit.blocking,
    )


def _compile_spawn(name: str, payload: dict[str, _Any]) -> list[_SceneNode]:
    x = float(payload["position"][0])
    z = float(payload["position"][1])
    return [
        _node(
            node_id=name,
            semantic_id=name,
            transform=_transform((x, 0.5, z)),
            visual=None,
            collider=None,
            navmesh_contributor=False,
        )
    ]


def compile_environment_model(model: _EnvironmentModel) -> _CandidateScene:
    """Compile a frozen environment model into an engine-facing candidate scene."""

    model = _EnvironmentModel.model_validate(model.model_dump())

    ground_footprint: _Polygon2D | None = None
    ground_name: str | None = None
    spawn_name: str | None = None
    seen_ground = False
    seen_spawn = False
    seen_camera = False
    for component in model.components:
        discriminator = component.payload.get("component")
        if discriminator == "ground":
            if seen_ground:
                raise ValueError("duplicate ground component")
            seen_ground = True
            ground_name = component.semantic_id
            ground_footprint = _polygon_from_payload(component.payload)
        elif discriminator == "spawn":
            if seen_spawn:
                raise ValueError("duplicate spawn component")
            seen_spawn = True
            spawn_name = component.semantic_id
        elif discriminator == "camera":
            if seen_camera:
                raise ValueError("duplicate camera component")
            seen_camera = True

    if ground_footprint is None or ground_name is None:
        raise ValueError("missing ground component")
    if spawn_name is None:
        raise ValueError("missing spawn component")

    blocker_polygons, walls, spawn_position = _extract_blockers(model.components)

    nodes: list[_SceneNode] = []
    camera_size: float | None = None

    for component in model.components:
        payload = component.payload
        discriminator = payload.get("component")
        name = component.semantic_id

        if discriminator == "ground":
            nodes.extend(_compile_ground(name, payload))
        elif discriminator == "path":
            nodes.extend(_compile_path(name, payload))
        elif discriminator == "water":
            nodes.extend(_compile_water(name, payload))
        elif discriminator == "wall":
            nodes.extend(_compile_wall(name, payload))
        elif discriminator == "obstacle":
            nodes.extend(_compile_obstacle(name, payload))
        elif discriminator == "structure":
            nodes.extend(_compile_structure(name, payload))
        elif discriminator == "landmark":
            nodes.extend(_compile_landmark(name, payload))
        elif discriminator == "scatter":
            nodes.extend(
                _compile_scatter(
                    name,
                    payload,
                    seed=model.seed,
                    ground_name=ground_name,
                    ground_footprint=ground_footprint,
                    blocker_polygons=blocker_polygons,
                    walls=walls,
                    spawn=spawn_position,
                )
            )
        elif discriminator == "spawn":
            nodes.extend(_compile_spawn(name, payload))
        elif discriminator == "camera":
            camera_size = float(payload["orthographic_size"])
        else:
            raise ValueError(f"unknown component discriminator: {discriminator!r}")

    if camera_size is None:
        raise ValueError("missing camera component")

    scene = _GodotSceneSpec(
        nodes=tuple(nodes),
        camera=_CameraSpec(
            follow_semantic_id=spawn_name,
            orthographic_size=camera_size,
            fade_occluders=True,
        ),
        controller_semantic_id=spawn_name,
    )
    manifest = _ArtifactManifest(root="artifacts", entries=())
    return _CandidateScene(scene=scene, manifest=manifest)
