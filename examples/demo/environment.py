"""Checked-in village-green fixture for the EnvMaker authoring worker."""

from envmaker.sdk import EnvironmentBuilder, Polygon2D


def build_environment():
    builder = EnvironmentBuilder("village-green", seed=7)
    builder.ground(
        "green",
        footprint=Polygon2D([(-20.0, -20.0), (20.0, -20.0), (20.0, 20.0), (-20.0, 20.0)]),
        material="grass",
    )
    builder.water(
        "moat_north",
        footprint=Polygon2D([(-2.0, 2.0), (0.0, 2.0), (0.0, 20.0), (-2.0, 20.0)]),
    )
    builder.water(
        "moat_south",
        footprint=Polygon2D([(-2.0, -20.0), (0.0, -20.0), (0.0, -2.0), (-2.0, -2.0)]),
    )
    builder.path(
        "lane",
        points=[(-14.0, 0.0), (-1.0, 0.0), (6.0, 6.0), (13.0, 13.0)],
        width=1.2,
        material="dirt",
    )
    builder.wall(
        "square_west",
        start=(6.0, 2.0),
        end=(6.0, 10.0),
        height=1.6,
        thickness=0.4,
        material="stone",
    )
    builder.wall(
        "square_north",
        start=(6.0, 10.0),
        end=(14.0, 10.0),
        height=1.6,
        thickness=0.4,
        material="stone",
    )
    builder.obstacle(
        "boulder_a",
        footprint=Polygon2D([(2.6, -6.2), (5.0, -6.2), (5.0, -3.8), (2.6, -3.8)]),
        height=1.2,
        material="rock",
    )
    builder.obstacle(
        "boulder_b",
        footprint=Polygon2D([(-8.0, 6.0), (-5.8, 6.0), (-5.8, 8.2), (-8.0, 8.2)]),
        height=1.0,
        material="rock",
    )
    builder.structure(
        "ruin",
        footprint=Polygon2D([(9.0, 3.0), (13.0, 3.0), (13.0, 7.0), (9.0, 7.0)]),
        height=2.2,
        kit="stone_ruin",
    )
    builder.landmark("obelisk_goal", position=(16.0, 16.0), kit="obelisk")
    builder.scatter(
        "pines",
        region="green",
        kit="pine",
        count=5,
        min_spacing=3.0,
    )
    builder.spawn("wanderer", position=(-14.0, -14.0))
    builder.camera(orthographic_size=30.0)
    return builder.freeze()


environment = build_environment()
