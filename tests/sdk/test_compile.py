"""Tests for EnvironmentModel -> CandidateScene compilation."""

from __future__ import annotations

import math

import pytest

from envmaker.core.model import ComponentKind, EnvironmentModel, SemanticComponent
from envmaker.core.scene_spec import BoxVisual, PlaneVisual
from envmaker.sdk import SDK_VERSION, EnvironmentBuilder, Polygon2D, compile_environment_model
from envmaker.sdk.footprints import polygon_contains
from envmaker.sdk.kits import get_kit


def _square(size: float = 20.0) -> Polygon2D:
    half = size / 2.0
    return Polygon2D(
        [(-half, -half), (half, -half), (half, half), (-half, half)]
    )


def _full_model(*, seed: int = 0) -> EnvironmentModel:
    """Build a model that exercises every authoring method once."""

    return (
        EnvironmentBuilder("demo", seed=seed)
        .ground("ground", footprint=_square(), material="grass")
        .path(
            "trail",
            points=[(-5.0, 0.0), (0.0, 0.0), (5.0, 0.0)],
            width=1.5,
            material="dirt",
        )
        .water(
            "pond",
            footprint=Polygon2D([(6.0, 6.0), (9.0, 6.0), (9.0, 9.0), (6.0, 9.0)]),
        )
        .wall(
            "fence",
            start=(-8.0, -4.0),
            end=(-8.0, 4.0),
            height=2.0,
            thickness=0.3,
            material="wood",
        )
        .obstacle(
            "rock",
            footprint=Polygon2D(
                [(2.0, -6.0), (4.0, -6.0), (4.0, -4.0), (2.0, -4.0)]
            ),
            height=1.5,
            material="rock",
        )
        .structure(
            "ruin",
            footprint=Polygon2D(
                [(-4.0, 2.0), (-1.0, 2.0), (-1.0, 5.0), (-4.0, 5.0)]
            ),
            height=3.0,
            kit="stone_ruin",
        )
        .landmark("marker", position=(0.0, 7.0), kit="obelisk")
        .scatter("grove", region="ground", kit="shrub", count=3, min_spacing=1.0)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
        .freeze()
    )


def _expected_node_count(model: EnvironmentModel) -> int:
    # ground(1) + path(2) + water(2: visual+blocker) + wall(1) + obstacle(1)
    # + stone_ruin(4) + obelisk(2) + shrub scatter 3*1 + spawn(1)
    return 1 + 2 + 2 + 1 + 1 + 4 + 2 + 3 + 1


def test_compile_full_round_trip_node_count() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    assert len(candidate.scene.nodes) == _expected_node_count(model)
    assert candidate.manifest.root == "artifacts"
    assert candidate.manifest.entries == ()


def test_compile_ground_flush_plane_and_navmesh() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    ground = next(node for node in candidate.scene.nodes if node.node_id == "ground")
    assert isinstance(ground.visual, PlaneVisual)
    assert ground.visual.size_x == pytest.approx(20.0)
    assert ground.visual.size_z == pytest.approx(20.0)
    assert ground.visual.material == "grass"
    assert ground.transform.origin.y == pytest.approx(0.0)
    assert ground.transform.origin.x == pytest.approx(0.0)
    assert ground.transform.origin.z == pytest.approx(0.0)
    assert ground.collider is not None
    assert ground.collider.shape.value == "box"
    assert ground.collider.dimensions == {"x": 20.0, "y": 0.5, "z": 20.0}
    assert ground.navmesh_contributor is True


def test_compile_path_has_no_collider() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    path_nodes = [
        node for node in candidate.scene.nodes if node.node_id.startswith("trail.")
    ]
    assert len(path_nodes) == 2
    for node in path_nodes:
        assert node.collider is None
        assert node.navmesh_contributor is False
        assert isinstance(node.visual, BoxVisual)
        assert node.transform.origin.y == pytest.approx(0.01)


def test_compile_blockers_have_colliders() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    by_id = {node.node_id: node for node in candidate.scene.nodes}
    for node_id in ("pond.0", "fence", "rock"):
        node = by_id[node_id]
        assert node.collider is not None
        assert node.navmesh_contributor is True
    assert by_id["pond"].collider is None
    assert by_id["pond"].navmesh_contributor is False


def test_compile_structure_blocking_landmark_not() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    ruin_nodes = [
        node for node in candidate.scene.nodes if node.node_id.startswith("ruin.")
    ]
    marker_nodes = [
        node for node in candidate.scene.nodes if node.node_id.startswith("marker.")
    ]
    assert len(ruin_nodes) == len(get_kit("stone_ruin").parts)
    assert len(marker_nodes) == len(get_kit("obelisk").parts)
    assert all(node.collider is not None and node.navmesh_contributor for node in ruin_nodes)
    assert all(
        node.collider is None and not node.navmesh_contributor for node in marker_nodes
    )


def test_compile_scatter_count_spacing_and_bounds() -> None:
    model = (
        EnvironmentBuilder("scatter-demo", seed=11)
        .ground("ground", footprint=_square(10.0), material="grass")
        .scatter("grove", region="ground", kit="shrub", count=4, min_spacing=1.5)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=12.0)
        .freeze()
    )
    candidate = compile_environment_model(model)
    placements = [
        node
        for node in candidate.scene.nodes
        if node.node_id.startswith("grove.")
    ]
    assert len(placements) == 4
    points = [
        (node.transform.origin.x, node.transform.origin.z) for node in placements
    ]
    footprint = _square(10.0)
    for x, z in points:
        assert polygon_contains(footprint, x, z)
        assert math.hypot(x, z) >= 1.0 - 1e-9
    for index, first in enumerate(points):
        for second in points[index + 1 :]:
            distance = math.hypot(first[0] - second[0], first[1] - second[1])
            assert distance >= 1.5 - 1e-9


def test_compile_yaw_basis_orthonormal() -> None:
    model = (
        EnvironmentBuilder("yaw-demo", seed=0)
        .ground("ground", footprint=_square(), material="grass")
        .obstacle(
            "diamond",
            footprint=Polygon2D([(2.0, 0.0), (4.0, 2.0), (2.0, 4.0), (0.0, 2.0)]),
            height=2.0,
            material="rock",
        )
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
        .freeze()
    )
    candidate = compile_environment_model(model)
    node = next(n for n in candidate.scene.nodes if n.node_id == "diamond")
    bx, by, bz = node.transform.basis_x, node.transform.basis_y, node.transform.basis_z
    assert math.isclose(bx.x * bx.x + bx.y * bx.y + bx.z * bx.z, 1.0, abs_tol=1e-9)
    assert math.isclose(by.x * by.x + by.y * by.y + by.z * by.z, 1.0, abs_tol=1e-9)
    assert math.isclose(bz.x * bz.x + bz.y * bz.y + bz.z * bz.z, 1.0, abs_tol=1e-9)
    assert math.isclose(bx.x * by.x + bx.y * by.y + bx.z * by.z, 0.0, abs_tol=1e-9)
    assert math.isclose(bx.x * bz.x + bx.y * bz.y + bx.z * bz.z, 0.0, abs_tol=1e-9)
    assert math.isclose(by.x * bz.x + by.y * bz.y + by.z * bz.z, 0.0, abs_tol=1e-9)
    cross_x = by.y * bz.z - by.z * bz.y
    cross_y = by.z * bz.x - by.x * bz.z
    cross_z = by.x * bz.y - by.y * bz.x
    det = bx.x * cross_x + bx.y * cross_y + bx.z * cross_z
    assert det == pytest.approx(1.0)


def test_compile_camera_follows_spawn() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    assert candidate.scene.camera.follow_semantic_id == "hero"
    assert candidate.scene.controller_semantic_id == "hero"
    assert candidate.scene.camera.orthographic_size == pytest.approx(16.0)
    assert candidate.scene.camera.fade_occluders is True
    hero = next(node for node in candidate.scene.nodes if node.node_id == "hero")
    assert hero.visual is None
    assert hero.collider is None
    assert hero.navmesh_contributor is False
    assert hero.transform.origin.y == pytest.approx(0.5)


def test_compile_determinism_and_seed_sensitivity() -> None:
    first = compile_environment_model(_full_model(seed=3))
    second = compile_environment_model(_full_model(seed=3))
    third = compile_environment_model(_full_model(seed=4))
    assert first.candidate_fingerprint == second.candidate_fingerprint
    assert first.candidate_fingerprint != third.candidate_fingerprint


def test_compile_rejects_unknown_discriminator() -> None:
    model = EnvironmentModel(
        name="alien",
        style="flat-shaded minimal",
        seed=0,
        sdk_version=SDK_VERSION,
        components=(
            SemanticComponent(
                semantic_id="ground",
                kind=ComponentKind.SURFACE,
                payload={
                    "component": "ground",
                    "footprint": [[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0]],
                    "material": "grass",
                },
            ),
            SemanticComponent(
                semantic_id="hero",
                kind=ComponentKind.DYNAMIC_ENTITY,
                payload={"component": "spawn", "position": [0.0, 0.0]},
            ),
            SemanticComponent(
                semantic_id="camera",
                kind=ComponentKind.PRESENTATION,
                payload={"component": "camera", "orthographic_size": 16.0},
            ),
            SemanticComponent(
                semantic_id="weird",
                kind=ComponentKind.PROP,
                payload={"component": "portal"},
            ),
        ),
    )
    with pytest.raises(ValueError, match="unknown"):
        compile_environment_model(model)


def test_compile_rotated_obstacle_corners_inside_local_box() -> None:
    yaw = math.radians(30.0)
    half_x, half_z = 4.0, 1.0
    center = (3.0, -2.0)
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    local_corners = [
        (-half_x, -half_z),
        (half_x, -half_z),
        (half_x, half_z),
        (-half_x, half_z),
    ]
    world_corners = [
        (
            center[0] + cos_yaw * lx - sin_yaw * lz,
            center[1] + sin_yaw * lx + cos_yaw * lz,
        )
        for lx, lz in local_corners
    ]
    model = (
        EnvironmentBuilder("rotated", seed=0)
        .ground("ground", footprint=_square(40.0), material="grass")
        .obstacle(
            "slab",
            footprint=Polygon2D(world_corners),
            height=1.0,
            material="rock",
        )
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
        .freeze()
    )
    candidate = compile_environment_model(model)
    node = next(n for n in candidate.scene.nodes if n.node_id == "slab")
    assert isinstance(node.visual, BoxVisual)
    size_x, _, size_z = node.visual.size
    origin = node.transform.origin
    bx = node.transform.basis_x
    bz = node.transform.basis_z
    for wx, wz in world_corners:
        dx = wx - origin.x
        dz = wz - origin.z
        local_x = dx * bx.x + dz * bx.z
        local_z = dx * bz.x + dz * bz.z
        assert abs(local_x) <= size_x / 2.0 + 1e-6
        assert abs(local_z) <= size_z / 2.0 + 1e-6


def test_compile_water_carve_geometry() -> None:
    model = _full_model()
    candidate = compile_environment_model(model)
    ground = next(n for n in candidate.scene.nodes if n.node_id == "ground")
    visual = next(n for n in candidate.scene.nodes if n.node_id == "pond")
    blocker = next(n for n in candidate.scene.nodes if n.node_id == "pond.0")
    assert ground.collider is not None
    ground_top = ground.transform.origin.y + ground.collider.dimensions["y"] / 2.0
    assert blocker.collider is not None
    blocker_top = blocker.transform.origin.y + blocker.collider.dimensions["y"] / 2.0
    assert blocker_top >= ground_top + 0.5
    assert visual.collider is None
    assert visual.navmesh_contributor is False
    assert isinstance(visual.visual, BoxVisual)
    assert blocker.visual is None
    assert blocker.navmesh_contributor is True


def test_compile_pine_scatter_parts_block() -> None:
    model = (
        EnvironmentBuilder("pine-demo", seed=2)
        .ground("ground", footprint=_square(40.0), material="grass")
        .scatter("grove", region="ground", kit="pine", count=2, min_spacing=4.0)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
        .freeze()
    )
    candidate = compile_environment_model(model)
    pine_nodes = [
        node for node in candidate.scene.nodes if node.node_id.startswith("grove.")
    ]
    assert len(pine_nodes) == 2 * len(get_kit("pine").parts)
    assert all(
        node.collider is not None and node.navmesh_contributor for node in pine_nodes
    )


def test_compile_scatter_shortfall_raises() -> None:
    model = (
        EnvironmentBuilder("tight", seed=0)
        .ground("ground", footprint=_square(4.0), material="grass")
        .scatter("grove", region="ground", kit="pine", count=50, min_spacing=3.0)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=12.0)
        .freeze()
    )
    with pytest.raises(ValueError, match=r"scatter 'grove' placed \d+ of 50 requested"):
        compile_environment_model(model)


def test_compile_scatter_avoids_blockers_and_spawn() -> None:
    water = Polygon2D([(-3.0, -3.0), (3.0, -3.0), (3.0, 3.0), (-3.0, 3.0)])
    model = (
        EnvironmentBuilder("avoid", seed=5)
        .ground("ground", footprint=_square(30.0), material="grass")
        .water("lake", footprint=water)
        .scatter("grove", region="ground", kit="shrub", count=8, min_spacing=1.0)
        .spawn("hero", position=(8.0, 8.0))
        .camera(orthographic_size=16.0)
        .freeze()
    )
    candidate = compile_environment_model(model)
    points = [
        (node.transform.origin.x, node.transform.origin.z)
        for node in candidate.scene.nodes
        if node.node_id.startswith("grove.")
    ]
    assert len(points) == 8
    for x, z in points:
        assert not polygon_contains(water, x, z)
        assert math.hypot(x - 8.0, z - 8.0) >= 1.0 - 1e-9


def test_compile_rejects_tampered_payload() -> None:
    model = _full_model()
    model.components[0].payload["material"] = "stone"
    with pytest.raises(ValueError, match="model_fingerprint mismatch"):
        compile_environment_model(model)


def test_compile_rejects_duplicate_singletons() -> None:
    model = EnvironmentModel(
        name="dupes",
        style="flat-shaded minimal",
        seed=0,
        sdk_version=SDK_VERSION,
        components=(
            SemanticComponent(
                semantic_id="ground",
                kind=ComponentKind.SURFACE,
                payload={
                    "component": "ground",
                    "footprint": [[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0]],
                    "material": "grass",
                },
            ),
            SemanticComponent(
                semantic_id="yard",
                kind=ComponentKind.SURFACE,
                payload={
                    "component": "ground",
                    "footprint": [[-2.0, -2.0], [2.0, -2.0], [2.0, 2.0], [-2.0, 2.0]],
                    "material": "dirt",
                },
            ),
            SemanticComponent(
                semantic_id="hero",
                kind=ComponentKind.DYNAMIC_ENTITY,
                payload={"component": "spawn", "position": [0.0, 0.0]},
            ),
            SemanticComponent(
                semantic_id="camera",
                kind=ComponentKind.PRESENTATION,
                payload={"component": "camera", "orthographic_size": 16.0},
            ),
        ),
    )
    with pytest.raises(ValueError, match="duplicate ground"):
        compile_environment_model(model)
