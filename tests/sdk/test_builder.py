import math

import pytest
from pydantic import ValidationError

from envmaker.core.model import ComponentKind
from envmaker.sdk.footprints import (
    Polygon2D,
    convex_hull,
    min_area_obb,
    polygon_area,
    polygon_bounds,
    polygon_centroid,
    polygon_contains,
)
from envmaker.sdk.kits import CURATED_MATERIALS, Kit, KitPart, KITS, get_kit


def test_polygon_validation_rejections() -> None:
    with pytest.raises(ValidationError, match="at least 3 points"):
        Polygon2D([(0.0, 0.0), (1.0, 0.0)])
    with pytest.raises(ValidationError, match="must be finite"):
        Polygon2D([(0.0, 0.0), (float("inf"), 0.0), (1.0, 1.0)])
    with pytest.raises(ValidationError, match="must be finite"):
        Polygon2D([(0.0, 0.0), (float("nan"), 0.0), (1.0, 1.0)])
    with pytest.raises(ValidationError, match="must not repeat points"):
        Polygon2D([(0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    with pytest.raises(ValidationError, match="simple polygon"):
        Polygon2D([(0.0, 0.0), (2.0, 2.0), (2.0, 0.0), (0.0, 2.0)])
    with pytest.raises(ValidationError, match="area must be positive"):
        Polygon2D([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)])


def test_polygon_positional_and_frozen() -> None:
    square = Polygon2D([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)])
    assert square.points == ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0))
    with pytest.raises(ValidationError):
        square.points = ()
    keyword = Polygon2D(points=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)])
    assert len(keyword.points) == 3


def test_polygon_measurements() -> None:
    square = Polygon2D([(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)])
    assert abs(polygon_area(square) - 16.0) < 1e-9
    assert polygon_centroid(square) == pytest.approx((2.0, 2.0))
    assert polygon_bounds(square) == pytest.approx((0.0, 0.0, 4.0, 4.0))
    assert polygon_contains(square, 2.0, 2.0)
    assert not polygon_contains(square, 5.0, 5.0)
    assert not polygon_contains(square, -0.5, 2.0)


def test_convex_hull_drops_interior_points() -> None:
    hull = convex_hull(
        [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0), (2.0, 2.0), (1.0, 2.0)]
    )
    assert set(hull) == {(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)}
    assert len(hull) == 4


def test_min_area_obb_axis_aligned() -> None:
    rectangle = Polygon2D([(1.0, 1.0), (5.0, 1.0), (5.0, 3.0), (1.0, 3.0)])
    fit = min_area_obb(rectangle)
    assert fit.center == pytest.approx((3.0, 2.0))
    assert fit.size_x * fit.size_z == pytest.approx(8.0)
    assert {round(fit.size_x, 6), round(fit.size_z, 6)} == {2.0, 4.0}
    assert min(
        abs(fit.yaw), abs(abs(fit.yaw) - math.pi / 2)
    ) == pytest.approx(0.0, abs=1e-9)


def test_min_area_obb_rotated_square() -> None:
    diamond = Polygon2D([(2.0, 0.0), (4.0, 2.0), (2.0, 4.0), (0.0, 2.0)])
    fit = min_area_obb(diamond)
    assert fit.center == pytest.approx((2.0, 2.0))
    assert fit.size_x * fit.size_z == pytest.approx(8.0)
    assert fit.size_x == pytest.approx(fit.size_z)
    assert abs(fit.yaw) == pytest.approx(math.pi / 4, abs=1e-9)


def test_kit_catalog_integrity() -> None:
    expected = {
        "stone_ruin": ("structure", True),
        "timber_hut": ("structure", True),
        "watchtower": ("structure", True),
        "obelisk": ("landmark", False),
        "banner": ("landmark", False),
        "pine": ("vegetation", True),
        "oak": ("vegetation", True),
        "boulder": ("vegetation", True),
        "shrub": ("vegetation", False),
    }
    assert set(KITS) == set(expected)
    for name, (category, blocking) in expected.items():
        kit = get_kit(name)
        assert kit.name == name
        assert kit.category == category
        assert kit.blocking is blocking
        assert len(kit.parts) >= 1
        for part in kit.parts:
            assert part.material in CURATED_MATERIALS
    pine = get_kit("pine")
    assert [part.shape for part in pine.parts] == ["cylinder", "cone"]
    oak = get_kit("oak")
    assert oak.parts[0].shape == "cylinder"
    assert all(part.shape == "sphere" for part in oak.parts[1:])
    boulder = get_kit("boulder")
    assert [part.shape for part in boulder.parts] == ["sphere", "sphere"]
    assert boulder.parts[0].offset[1] < boulder.parts[0].radius
    with pytest.raises(ValueError, match="unknown kit: castle"):
        get_kit("castle")


def test_kit_part_shape_exclusivity() -> None:
    with pytest.raises(ValidationError, match="box parts take size only"):
        KitPart(shape="box", offset=(0.0, 0.0, 0.0), radius=1.0, height=1.0,
                material="stone")
    with pytest.raises(
        ValidationError, match="cylinder parts take radius and height only"
    ):
        KitPart(shape="cylinder", offset=(0.0, 0.0, 0.0),
                size=(1.0, 1.0, 1.0), material="stone")
    with pytest.raises(ValidationError, match="sphere parts take radius only"):
        KitPart(
            shape="sphere",
            offset=(0.0, 0.0, 0.0),
            radius=1.0,
            height=1.0,
            material="rock",
        )
    with pytest.raises(
        ValidationError, match="cone parts take radius and height only"
    ):
        KitPart(
            shape="cone",
            offset=(0.0, 0.0, 0.0),
            size=(1.0, 1.0, 1.0),
            material="grass",
        )
    ok_sphere = KitPart(
        shape="sphere", offset=(0.0, 0.5, 0.0), radius=0.8, material="rock"
    )
    assert ok_sphere.radius == pytest.approx(0.8)
    ok_cone = KitPart(
        shape="cone",
        offset=(0.0, 1.0, 0.0),
        radius=0.5,
        height=1.2,
        material="grass",
    )
    assert ok_cone.height == pytest.approx(1.2)
    with pytest.raises(
        ValidationError, match="dimensions must be finite and positive"
    ):
        KitPart(shape="box", offset=(0.0, 0.0, 0.0), size=(0.0, 1.0, 1.0),
                material="stone")
    with pytest.raises(ValidationError, match="unknown material: chrome"):
        KitPart(shape="box", offset=(0.0, 0.0, 0.0), size=(1.0, 1.0, 1.0),
                material="chrome")


def test_kit_blocking_is_explicit() -> None:
    with pytest.raises(ValidationError):
        Kit(name="ghost", category="landmark",
            parts=(KitPart(shape="box", offset=(0.0, 0.0, 0.0),
                           size=(1.0, 1.0, 1.0), material="stone"),))


def test_structure_kits_stay_in_unit_space() -> None:
    # Unit-space rule applies only to structure kits (vegetation/landmark are
    # authored in world scale).
    for name in ("stone_ruin", "timber_hut", "watchtower"):
        for part in get_kit(name).parts:
            if part.shape == "box":
                extent_x, extent_y, extent_z = part.size
            elif part.shape == "sphere":
                extent_x = extent_y = extent_z = 2.0 * part.radius
            else:
                extent_x = extent_z = 2.0 * part.radius
                extent_y = part.height
            assert abs(part.offset[0]) + extent_x / 2.0 <= 0.5 + 1e-9, name
            assert abs(part.offset[2]) + extent_z / 2.0 <= 0.5 + 1e-9, name
            assert part.offset[1] - extent_y / 2.0 >= -1e-9, name
            assert part.offset[1] + extent_y / 2.0 <= 1.0 + 1e-9, name


def _square(size: float = 20.0) -> Polygon2D:
    half = size / 2.0
    return Polygon2D(
        [(-half, -half), (half, -half), (half, half), (-half, half)]
    )


def _full_builder(*, seed: int = 0) -> "EnvironmentBuilder":
    from envmaker.sdk.builder import EnvironmentBuilder

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
            footprint=Polygon2D([(2.0, -6.0), (4.0, -6.0), (4.0, -4.0), (2.0, -4.0)]),
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
    )


def test_builder_happy_path_component_order() -> None:
    from envmaker.sdk.builder import SDK_VERSION

    model = _full_builder().freeze()
    assert model.name == "demo"
    assert model.style == "flat-shaded minimal"
    assert model.seed == 0
    assert model.sdk_version == SDK_VERSION == "0.1.0"
    assert len(model.components) == 10
    expected = [
        ("ground", ComponentKind.SURFACE, "ground"),
        ("trail", ComponentKind.PRESENTATION, "path"),
        ("pond", ComponentKind.PROP, "water"),
        ("fence", ComponentKind.PROP, "wall"),
        ("rock", ComponentKind.PROP, "obstacle"),
        ("ruin", ComponentKind.STRUCTURE, "structure"),
        ("marker", ComponentKind.PRESENTATION, "landmark"),
        ("grove", ComponentKind.PROP, "scatter"),
        ("hero", ComponentKind.DYNAMIC_ENTITY, "spawn"),
        ("camera", ComponentKind.PRESENTATION, "camera"),
    ]
    for component, (semantic_id, kind, discriminator) in zip(
        model.components, expected, strict=True
    ):
        assert component.semantic_id == semantic_id
        assert component.kind == kind
        assert component.payload["component"] == discriminator


def test_builder_rejects_duplicate_names() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    builder = EnvironmentBuilder("demo").ground(
        "ground", footprint=_square(), material="grass"
    )
    with pytest.raises(ValueError, match="duplicate name"):
        builder.path(
            "ground",
            points=[(0.0, 0.0), (1.0, 0.0)],
            width=1.0,
            material="dirt",
        )


def test_builder_rejects_second_ground_spawn_camera() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    builder = (
        EnvironmentBuilder("demo")
        .ground("ground", footprint=_square(), material="grass")
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
    )
    with pytest.raises(ValueError, match="ground already declared"):
        builder.ground("yard", footprint=_square(10.0), material="dirt")
    with pytest.raises(ValueError, match="spawn already declared"):
        builder.spawn("extra", position=(1.0, 1.0))
    with pytest.raises(ValueError, match="camera already declared"):
        builder.camera(orthographic_size=20.0)


def test_builder_rejects_unknown_material() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    with pytest.raises(ValueError, match="unknown material"):
        EnvironmentBuilder("demo").ground(
            "ground", footprint=_square(), material="chrome"
        )


def test_builder_rejects_unknown_and_mismatched_kits() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    builder = EnvironmentBuilder("demo").ground(
        "ground", footprint=_square(), material="grass"
    )
    with pytest.raises(ValueError, match="unknown kit"):
        builder.structure(
            "ruin",
            footprint=Polygon2D([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]),
            height=2.0,
            kit="castle",
        )
    with pytest.raises(ValueError, match="structure"):
        builder.structure(
            "ruin",
            footprint=Polygon2D([(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)]),
            height=2.0,
            kit="pine",
        )


def test_builder_freeze_requires_spawn_on_ground_and_camera() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    builder = (
        EnvironmentBuilder("demo")
        .ground("ground", footprint=_square(), material="grass")
        .spawn("hero", position=(100.0, 100.0))
        .camera(orthographic_size=16.0)
    )
    with pytest.raises(ValueError, match="spawn must lie on the ground footprint"):
        builder.freeze()

    incomplete = (
        EnvironmentBuilder("demo")
        .ground("ground", footprint=_square(), material="grass")
        .spawn("hero", position=(0.0, 0.0))
    )
    with pytest.raises(ValueError, match="camera"):
        incomplete.freeze()


def test_builder_rejects_mutation_after_freeze() -> None:
    builder = _full_builder()
    builder.freeze()
    with pytest.raises(ValueError, match="builder is frozen"):
        builder.wall(
            "wall2",
            start=(0.0, 0.0),
            end=(1.0, 0.0),
            height=1.0,
            thickness=0.2,
            material="stone",
        )
    with pytest.raises(ValueError, match="builder is frozen"):
        builder.freeze()


def test_builder_fingerprint_determinism() -> None:
    first = _full_builder(seed=7).freeze()
    second = _full_builder(seed=7).freeze()
    third = _full_builder(seed=8).freeze()
    assert first.model_fingerprint == second.model_fingerprint
    assert first.model_fingerprint != third.model_fingerprint


def test_sdk_public_surface() -> None:
    from envmaker import sdk
    from envmaker.sdk import (
        CURATED_MATERIALS as materials,
        EnvironmentBuilder as builder_cls,
        KITS as kits,
        Polygon2D as polygon_cls,
        SDK_VERSION as version,
        compile_environment_model as compile_fn,
        get_kit as kit_fn,
    )

    assert list(sdk.__all__) == [
        "SDK_VERSION",
        "EnvironmentBuilder",
        "Polygon2D",
        "compile_environment_model",
        "get_kit",
        "KITS",
        "CURATED_MATERIALS",
    ]
    assert version == "0.1.0"
    assert callable(builder_cls)
    assert callable(compile_fn)
    assert callable(kit_fn)
    assert "grass" in materials
    assert "pine" in kits
    assert polygon_cls is Polygon2D


def test_builder_rejects_boundary_spawn_accepts_interior() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    boundary = (
        EnvironmentBuilder("edge")
        .ground("ground", footprint=_square(10.0), material="grass")
        .spawn("hero", position=(4.8, 0.0))
        .camera(orthographic_size=12.0)
    )
    with pytest.raises(ValueError, match="spawn must lie on the ground footprint"):
        boundary.freeze()

    interior = (
        EnvironmentBuilder("center")
        .ground("ground", footprint=_square(10.0), material="grass")
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=12.0)
    )
    model = interior.freeze()
    assert model.components[-2].payload["position"] == [0.0, 0.0]


def test_builder_rejects_non_rectangle_ground() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    diamond = Polygon2D([(0.0, -2.0), (2.0, 0.0), (0.0, 2.0), (-2.0, 0.0)])
    with pytest.raises(
        ValueError, match="ground footprint must be an axis-aligned rectangle"
    ):
        EnvironmentBuilder("skew").ground(
            "ground", footprint=diamond, material="grass"
        )


def test_builder_rejects_spawn_inside_blocker() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    builder = (
        EnvironmentBuilder("blocked")
        .ground("ground", footprint=_square(20.0), material="grass")
        .water(
            "pond",
            footprint=Polygon2D([(-2.0, -2.0), (2.0, -2.0), (2.0, 2.0), (-2.0, 2.0)]),
        )
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
    )
    with pytest.raises(ValueError, match="spawn intersects a blocker"):
        builder.freeze()


def test_builder_rejects_oversized_coordinates() -> None:
    from envmaker.sdk.builder import EnvironmentBuilder

    with pytest.raises(ValueError, match="magnitude"):
        EnvironmentBuilder("huge").wall(
            "wall",
            start=(0.0, 0.0),
            end=(1e308, 0.0),
            height=1.0,
            thickness=0.2,
            material="stone",
        )


def test_spawn_blocker_error_is_located() -> None:
    from envmaker.sdk import EnvironmentBuilder

    builder = EnvironmentBuilder("located", seed=1)
    builder.ground(
        "g",
        footprint=Polygon2D([(-10, -10), (10, -10), (10, 10), (-10, 10)]),
        material="grass",
    )
    builder.water(
        "pond_east", footprint=Polygon2D([(2, -3), (8, -3), (8, 3), (2, 3)])
    )
    builder.spawn("agent", position=(5.0, 0.0))
    builder.camera(orthographic_size=15)
    with pytest.raises(ValueError) as excinfo:
        builder.freeze()
    message = str(excinfo.value)
    assert message.startswith("spawn intersects a blocker")
    assert "pond_east" in message
    assert "x 2.0..8.0" in message and "z -3.0..3.0" in message
    assert "spawn=(5.0, 0.0)" in message


def test_spawn_off_ground_error_is_located() -> None:
    from envmaker.sdk import EnvironmentBuilder

    builder = EnvironmentBuilder("located2", seed=1)
    builder.ground(
        "g",
        footprint=Polygon2D([(-10, -10), (10, -10), (10, 10), (-10, 10)]),
        material="grass",
    )
    builder.spawn("agent", position=(9.9, 0.0))
    builder.camera(orthographic_size=15)
    with pytest.raises(ValueError) as excinfo:
        builder.freeze()
    message = str(excinfo.value)
    assert message.startswith("spawn must lie on the ground footprint")
    assert "spawn=(9.9, 0.0)" in message
    assert "ground x -10.0..10.0" in message


def test_builder_prop_category_and_yaw_and_spawn_keepout() -> None:
    from envmaker.core.model import ComponentKind
    from envmaker.sdk import EnvironmentBuilder

    builder = EnvironmentBuilder("props", seed=2).ground(
        "ground", footprint=_square(40.0), material="grass"
    )
    with pytest.raises(ValueError, match="use structure\\(\\)"):
        builder.prop("hut", kit="timber_hut", position=(4.0, 4.0))

    model = (
        EnvironmentBuilder("yaw", seed=2)
        .ground("ground", footprint=_square(40.0), material="grass")
        .prop("flag", kit="banner", position=(6.0, 6.0), yaw=-45.0, scale=1.25)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
        .freeze()
    )
    prop = model.components[1]
    assert prop.kind is ComponentKind.PRESENTATION
    assert prop.payload["component"] == "prop"
    assert prop.payload["yaw_degrees"] == pytest.approx(315.0)
    assert prop.payload["scale"] == pytest.approx(1.25)

    pine = (
        EnvironmentBuilder("blocked-prop", seed=2)
        .ground("ground", footprint=_square(40.0), material="grass")
        .prop("tree", kit="pine", position=(0.0, 0.0), scale=1.0)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=16.0)
    )
    with pytest.raises(ValueError) as excinfo:
        pine.freeze()
    message = str(excinfo.value)
    assert "tree" in message
    assert "radius" in message
    assert "prop" in message


def test_builder_scatter_scale_range_validation() -> None:
    from envmaker.sdk import EnvironmentBuilder

    builder = (
        EnvironmentBuilder("scatter-opt", seed=1)
        .ground("ground", footprint=_square(20.0), material="grass")
    )
    with pytest.raises(ValueError, match="scale_range"):
        builder.scatter(
            "grove",
            region="ground",
            kit="shrub",
            count=2,
            min_spacing=1.0,
            scale_range=(0.4, 1.0),
        )
    model = (
        EnvironmentBuilder("scatter-ok", seed=1)
        .ground("ground", footprint=_square(20.0), material="grass")
        .scatter(
            "grove",
            region="ground",
            kit="pine",
            count=2,
            min_spacing=2.0,
            yaw_jitter=True,
            scale_range=(0.5, 2.0),
        )
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=14.0)
        .freeze()
    )
    payload = model.components[1].payload
    assert payload["yaw_jitter"] is True
    assert payload["scale_range"] == [0.5, 2.0]
