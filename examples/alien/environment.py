"""Alien acceptance fixture: violet basin, bioluminescent flora, crystal beacon.

Exercises the full bounded visual-extension layer: custom emissive
materials, a category palette, scene lighting, and custom primitive kits —
while navigation still runs over plain conservative colliders.
"""

from envmaker.sdk import (
    BoxPart,
    ConePart,
    CylinderPart,
    EnvironmentBuilder,
    Polygon2D,
    SpherePart,
)


def build_environment():
    b = EnvironmentBuilder("alien_basin", seed=11, style="alien bioluminescent")

    b.material(
        "violet_crystal",
        color="#a36cff",
        emission_color="#7b3dff",
        emission_strength=1.4,
        roughness=0.25,
        metallic=0.15,
    )
    b.material(
        "biolume_teal",
        color="#27d9c2",
        emission_color="#1cd6c8",
        emission_strength=1.0,
        roughness=0.35,
    )
    b.material(
        "magenta_bloom",
        color="#e568ff",
        emission_color="#d23dff",
        emission_strength=1.6,
        roughness=0.3,
    )

    b.palette(
        ground="#4a3572",
        path="#1d1433",
        vegetation="#27d9c2",
        rock="#9672c7",
        structure="#7a63a8",
        accent="#e568ff",
        sky_top="#12081f",
        sky_horizon="#593b82",
        sun="#c5a6ff",
    )
    b.lighting(ambient_color="#443b62", ambient_energy=1.0, sun_energy=0.4)

    b.ground(
        "basin",
        footprint=Polygon2D([(-14, -14), (14, -14), (14, 14), (-14, 14)]),
        material="rock",
    )
    b.path(
        "glow_trail",
        points=[(-10, -10), (-4, -6), (2, 0), (8, 6), (10, 10)],
        width=1.4,
        material="dirt",
    )

    b.custom_kit(
        "crystal_cluster",
        category="vegetation",
        blocking=True,
        parts=[
            ConePart(offset=(0.0, 0.9, 0.0), radius=0.5, height=2.0,
                     material="violet_crystal"),
            ConePart(offset=(0.55, 0.55, 0.2), radius=0.3, height=1.2,
                     material="violet_crystal", yaw=18.0),
            ConePart(offset=(-0.4, 0.45, -0.3), radius=0.25, height=0.9,
                     material="violet_crystal", yaw=250.0),
            SpherePart(offset=(0.1, 0.15, 0.4), radius=0.3, material="rock"),
        ],
    )
    b.custom_kit(
        "glow_tuft",
        category="vegetation",
        blocking=False,
        parts=[
            SpherePart(offset=(0.0, 0.25, 0.0), radius=0.35,
                       material="biolume_teal"),
            SpherePart(offset=(0.4, 0.15, 0.2), radius=0.22,
                       material="biolume_teal"),
            SpherePart(offset=(-0.3, 0.18, -0.25), radius=0.25,
                       material="biolume_teal"),
        ],
    )
    b.custom_kit(
        "beacon",
        category="landmark",
        blocking=False,
        parts=[
            CylinderPart(offset=(0.0, 1.4, 0.0), radius=0.18, height=2.8,
                         material="violet_crystal"),
            SpherePart(offset=(0.0, 3.1, 0.0), radius=0.55,
                       material="magenta_bloom"),
            BoxPart(offset=(0.0, 0.15, 0.0), size=(1.1, 0.3, 1.1),
                    material="rock", yaw=45.0),
        ],
    )

    # A loose crystal field the glow trail bends around.
    b.prop("cluster_gate_w", kit="crystal_cluster", position=(-2.5, -2.0),
           yaw=20.0, scale=1.4)
    b.prop("cluster_gate_e", kit="crystal_cluster", position=(3.5, 2.5),
           yaw=210.0, scale=1.2)
    b.prop("cluster_north", kit="crystal_cluster", position=(-6.0, 6.0),
           yaw=120.0, scale=1.0)
    b.prop("boulder_south", kit="boulder", position=(6.0, -6.0), yaw=75.0,
           scale=1.3)

    b.scatter(
        "tufts",
        region="basin",
        kit="glow_tuft",
        count=10,
        min_spacing=3.0,
        yaw_jitter=True,
        scale_range=(0.7, 1.4),
    )

    b.landmark("beacon_spire", position=(10.0, 10.0), kit="beacon")
    b.spawn("wanderer", position=(-10.0, -10.0))
    b.camera(orthographic_size=20.0)
    return b.freeze()


environment = build_environment()
