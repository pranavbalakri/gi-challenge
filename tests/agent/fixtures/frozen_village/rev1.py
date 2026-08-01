from envmaker.sdk import EnvironmentBuilder, Polygon2D


def build_environment():
    builder = EnvironmentBuilder("frozen-village", seed=7, style="frozen village")
    builder.ground(
        "tundra",
        footprint=Polygon2D([(-20.0, -20.0), (20.0, -20.0), (20.0, 20.0), (-20.0, 20.0)]),
        material="snow",
    )
    builder.path(
        "trail",
        points=[(-16.0, -16.0), (-4.0, 0.0), (10.0, 6.0)],
        width=1.2,
        material="dirt",
    )
    builder.wall(
        "ice_wall_north",
        start=(-2.0, 1.2),
        end=(-2.0, 20.0),
        height=2.0,
        thickness=0.5,
        material="stone",
    )
    builder.wall(
        "ice_wall_south",
        start=(-2.0, -20.0),
        end=(-2.0, -1.2),
        height=2.0,
        thickness=0.5,
        material="stone",
    )
    builder.obstacle(
        "gate_ice",
        footprint=Polygon2D([(-2.9, -1.2), (-1.1, -1.2), (-1.1, 1.2), (-2.9, 1.2)]),
        height=1.2,
        material="rock",
    )
    builder.obstacle(
        "frozen_boulder",
        footprint=Polygon2D([(-12.0, -12.0), (-10.0, -12.0), (-10.0, -10.0), (-12.0, -10.0)]),
        height=1.4,
        material="rock",
    )
    builder.structure(
        "watchtower_keep",
        footprint=Polygon2D([(8.0, 8.0), (12.0, 8.0), (12.0, 12.0), (8.0, 12.0)]),
        height=3.0,
        kit="watchtower",
    )
    builder.landmark("banner_goal", position=(10.0, 6.0), kit="banner")
    builder.wall(
        "square_west",
        start=(6.0, 4.0),
        end=(6.0, 12.0),
        height=1.6,
        thickness=0.4,
        material="stone",
    )
    builder.wall(
        "square_north",
        start=(6.0, 12.0),
        end=(14.0, 12.0),
        height=1.6,
        thickness=0.4,
        material="stone",
    )
    builder.scatter(
        "pines",
        region="tundra",
        kit="pine",
        count=4,
        min_spacing=3.0,
    )
    builder.spawn("wanderer", position=(-11.0, -11.0))
    builder.camera(orthographic_size=24.0)
    return builder.freeze()


environment = build_environment()
