"""Bounded visual-extension layer: palettes, custom materials, custom kits."""

from __future__ import annotations

import pytest

from envmaker.core.artifacts import canonical_json
from envmaker.sdk import (
    BoxPart,
    ConePart,
    CylinderPart,
    EnvironmentBuilder,
    Polygon2D,
    SpherePart,
    compile_environment_model,
)


def _base_builder(seed: int = 7) -> EnvironmentBuilder:
    builder = EnvironmentBuilder("visual_ext", seed=seed, style="alien")
    builder.ground(
        "terrain",
        footprint=Polygon2D([(-12, -12), (12, -12), (12, 12), (-12, 12)]),
        material="rock",
    )
    return builder


def _finish(builder: EnvironmentBuilder) -> EnvironmentBuilder:
    builder.spawn("agent", position=(-8, -8))
    builder.camera(orthographic_size=18.0)
    return builder


def test_palette_compiles_deterministically_and_rewrites_roles() -> None:
    def build() -> object:
        builder = _base_builder()
        builder.palette(
            ground="#6a4ba3",
            path="#30214F",
            vegetation="#27d9c2",
            accent="#E568FF",
            sky_top="#180e32",
            sky_horizon="#593b82",
            sun="#c5a6ff",
        )
        builder.path("lane", points=[(-8, 0), (0, 0), (6, 4)], width=1.4, material="dirt")
        builder.landmark("goal", position=(8, -8), kit="obelisk")
        builder.prop("tree", kit="pine", position=(4, 6))
        return compile_environment_model(_finish(builder).freeze())

    first = build()
    second = build()
    assert first.candidate_fingerprint == second.candidate_fingerprint
    assert canonical_json(first) == canonical_json(second)

    materials = first.scene.materials
    assert materials is not None
    # Case-insensitive input normalizes to canonical lowercase hex.
    assert materials["palette.accent"].color == "#e568ff"
    ground = next(n for n in first.scene.nodes if n.node_id == "terrain")
    assert ground.visual.material == "palette.ground"
    lane = next(n for n in first.scene.nodes if n.node_id == "lane.0")
    assert lane.visual.material == "palette.path"
    goal_parts = [n for n in first.scene.nodes if n.node_id.startswith("goal.")]
    assert {n.visual.material for n in goal_parts} == {"palette.accent"}
    # Pine canopy (grass) follows vegetation; trunk (wood) has no override here.
    tree_materials = {
        n.visual.material for n in first.scene.nodes if n.node_id.startswith("tree.")
    }
    assert "palette.vegetation" in tree_materials
    assert "wood" in tree_materials
    presentation = first.scene.presentation
    assert presentation.sky_top == "#180e32"
    assert presentation.sun_color == "#c5a6ff"


def test_no_palette_omits_extension_fields_and_keeps_fingerprint() -> None:
    builder = _finish(_base_builder())
    candidate = compile_environment_model(builder.freeze())
    assert candidate.scene.materials is None
    assert candidate.scene.presentation is None
    text = canonical_json(candidate)
    assert '"materials"' not in text
    assert '"presentation"' not in text


def test_custom_material_rejects_invalid_values() -> None:
    builder = _base_builder()
    with pytest.raises(ValueError, match="hex string"):
        builder.material("bad_color", color="magenta")
    with pytest.raises(ValueError, match="no alpha"):
        builder.material("bad_alpha", color="#a36cff80")
    with pytest.raises(ValueError, match="emission_strength"):
        builder.material(
            "too_hot", color="#a36cff", emission_color="#a36cff",
            emission_strength=9.0,
        )
    with pytest.raises(ValueError, match="requires emission_color"):
        builder.material("orphan", color="#a36cff", emission_strength=1.0)
    with pytest.raises(ValueError, match="roughness"):
        builder.material("rough", color="#a36cff", roughness=1.5)
    with pytest.raises(ValueError, match="metallic"):
        builder.material("metal", color="#a36cff", metallic=-0.1)
    with pytest.raises(ValueError, match="shadows a curated material"):
        builder.material("grass", color="#a36cff")
    builder.material("ok", color="#A36CFF")
    with pytest.raises(ValueError, match="duplicate material"):
        builder.material("ok", color="#a36cff")


def test_unknown_material_is_a_typed_failure_naming_it() -> None:
    builder = _base_builder()
    with pytest.raises(ValueError, match="unknown material: violet_crystal"):
        builder.wall(
            "rampart", start=(-4, 0), end=(4, 0), height=2.0, thickness=0.4,
            material="violet_crystal",
        )
    with pytest.raises(ValueError, match="unknown material: nope"):
        builder.custom_kit(
            "spike", category="vegetation", blocking=False,
            parts=[ConePart(offset=(0, 0.5, 0), radius=0.3, height=1.0, material="nope")],
        )


def test_custom_kit_bounds_and_shape_constraints() -> None:
    builder = _base_builder()
    part = SpherePart(offset=(0, 0.4, 0), radius=0.5, material="rock")
    with pytest.raises(ValueError, match="1..16"):
        builder.custom_kit(
            "crowded", category="vegetation", blocking=False, parts=[part] * 17
        )
    with pytest.raises(ValueError, match="shadows a curated kit"):
        builder.custom_kit(
            "pine", category="vegetation", blocking=False, parts=[part]
        )
    with pytest.raises(ValueError, match="BoxPart, CylinderPart"):
        builder.custom_kit(
            "raw", category="vegetation", blocking=False,
            parts=[{"shape": "torus"}],
        )
    with pytest.raises(ValueError, match="magnitude must be at most 8"):
        SpherePart(offset=(0, 40.0, 0), radius=0.5, material="rock")
    for index in range(12):
        builder.custom_kit(
            f"kit_{index}", category="vegetation", blocking=False, parts=[part]
        )
    with pytest.raises(ValueError, match="at most 12 custom kits"):
        builder.custom_kit(
            "kit_overflow", category="vegetation", blocking=False, parts=[part]
        )


def test_custom_kit_compiles_visuals_and_conservative_colliders() -> None:
    builder = _base_builder()
    builder.material(
        "violet_crystal", color="#a36cff", emission_color="#7b3dff",
        emission_strength=1.2, roughness=0.25, metallic=0.15,
    )
    builder.custom_kit(
        "crystal_cluster", category="vegetation", blocking=True,
        parts=[
            ConePart(offset=(0.0, 0.8, 0.0), radius=0.45, height=1.8,
                     material="violet_crystal"),
            SpherePart(offset=(0.5, 0.3, 0.2), radius=0.35,
                       material="violet_crystal"),
            BoxPart(offset=(-0.4, 0.2, 0.0), size=(0.4, 0.4, 0.4),
                    material="rock", yaw=30.0),
            CylinderPart(offset=(0.0, 0.2, -0.5), radius=0.2, height=0.4,
                         material="violet_crystal"),
        ],
    )
    builder.prop("cluster", kit="crystal_cluster", position=(4, -4))
    candidate = compile_environment_model(_finish(builder).freeze())

    parts = [n for n in candidate.scene.nodes if n.node_id.startswith("cluster.")]
    assert len(parts) == 4
    by_shape = {n.visual.shape: n for n in parts}
    # Cone compiles as a zero-top-radius cylinder visual.
    assert by_shape["cylinder"].collider.shape.value == "cylinder"
    assert by_shape["sphere"].collider.shape.value == "cylinder"
    sphere_dims = by_shape["sphere"].collider.dimensions
    assert sphere_dims["height"] == pytest.approx(2 * sphere_dims["radius"])
    assert by_shape["box"].collider.shape.value == "box"
    assert all(n.navmesh_contributor for n in parts)
    table = candidate.scene.materials
    assert table["violet_crystal"].emission_strength == pytest.approx(1.2)


def test_nonblocking_custom_kit_gets_no_colliders() -> None:
    builder = _base_builder()
    builder.custom_kit(
        "glow_moss", category="vegetation", blocking=False,
        parts=[SpherePart(offset=(0, 0.2, 0), radius=0.4, material="grass")],
    )
    builder.prop("moss", kit="glow_moss", position=(3, 3))
    candidate = compile_environment_model(_finish(builder).freeze())
    parts = [n for n in candidate.scene.nodes if n.node_id.startswith("moss.")]
    assert parts and all(n.collider is None for n in parts)
    assert all(not n.navmesh_contributor for n in parts)


def test_visual_material_changes_never_alter_colliders() -> None:
    def build(wall_material: str, with_palette: bool) -> object:
        builder = _base_builder()
        if wall_material not in {"stone"}:
            builder.material(wall_material, color="#a36cff")
        if with_palette:
            builder.palette(structure="#7a63a8", ground="#6a4ba3")
        builder.wall(
            "rampart", start=(-4, 0), end=(4, 0), height=2.0, thickness=0.4,
            material=wall_material,
        )
        return compile_environment_model(_finish(builder).freeze())

    plain = build("stone", with_palette=False)
    themed = build("stone", with_palette=True)
    custom = build("lava_glass", with_palette=False)

    def wall_collider(candidate: object) -> tuple:
        node = next(n for n in candidate.scene.nodes if n.node_id == "rampart")
        return (node.collider.shape.value, tuple(sorted(node.collider.dimensions.items())))

    assert wall_collider(plain) == wall_collider(themed) == wall_collider(custom)
    themed_wall = next(n for n in themed.scene.nodes if n.node_id == "rampart")
    assert themed_wall.visual.material == "palette.structure"


def test_lighting_clamps_energies() -> None:
    builder = _base_builder()
    builder.lighting(ambient_energy=99.0, sun_energy=-3.0, sun_color="#c5a6ff")
    candidate = compile_environment_model(_finish(builder).freeze())
    presentation = candidate.scene.presentation
    assert presentation.ambient_energy == pytest.approx(2.0)
    assert presentation.sun_energy == pytest.approx(0.0)
    assert presentation.sun_color == "#c5a6ff"
