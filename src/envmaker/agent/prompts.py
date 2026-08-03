"""System and user prompts for the EnvMaker authoring agent.

The prompt is assembled from shared fragments so the two authoring modes
stay consistent without contradicting each other:

- ``SYSTEM_PROMPT`` (API loop): shared contract + tool protocol. Frozen —
  the evaluation in evals/mvp-report.md was run against this exact text.
- ``AGENT_CONTRACT`` (agent-driven ``author`` mode): shared contract only.
  No FIRST-MOVE rule, no fenced-reply requirement, no tool list — the
  enclosing coding agent edits environment.py with its own file tools.
"""

from __future__ import annotations

__all__ = [
    "AGENT_CONTRACT",
    "PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "build_user_prompt",
]

PROMPT_VERSION = "2"

_PREAMBLE = """\
You author a playable 3D-lite environment via the EnvMaker SDK. You receive typed \
validation failures (stable codes, measurements, guidance) and repair by patching \
your program until all hard stages pass.

"""

_SDK_CONTRACT = """\
PROGRAM CONTRACT (exact):
- Define `def build_environment() -> EnvironmentModel` and end with \
`environment = build_environment()`.
- Imports allowed ONLY from `envmaker.sdk` (EnvironmentBuilder, Polygon2D, \
BoxPart, CylinderPart, ConePart, SpherePart) and `import math`.
- No filesystem, networking, exec/eval/open, or Godot APIs.

SDK QUICKSTART (EnvironmentBuilder methods):
- EnvironmentBuilder(name, *, style="...", seed=0) — start a mutable builder.
- ground(name, *, footprint, material) — unique walkable plane. Footprint MUST be \
an axis-aligned rectangle (exactly 4 points). Exactly one ground.
- path(name, *, points, width, material) — visual-only ribbon (no collider).
- water(name, *, footprint) — non-walkable water (blocks navigation).
- wall(name, *, start, end, height, thickness, material) — blocking segment.
- obstacle(name, *, footprint, height, material) — blocking extruded prop.
- structure(name, *, footprint, height, kit) — blocking curated structure kit.
- landmark(name, *, position, kit) — non-blocking landmark kit at a point \
(optional: without one, validation probes the farthest open ground point).
- prop(name, *, kit, position, yaw=0.0, scale=1.0) — direct landmark/vegetation \
placement (yaw in degrees, scale 0.5–2.0). Structure kits need structure().
- scatter(name, *, region, kit, count, min_spacing, yaw_jitter=False, \
scale_range=None) — seeded vegetation. `region` MUST equal the ground name; \
count ∈ [1, 512]. Opt-in yaw_jitter / scale_range=(lo,hi) with 0.5≤lo≤hi≤2.0.
- spawn(name, *, position) — unique agent spawn. Must lie strictly inside ground \
with 0.4 m margin on all sides and outside all blockers. Exactly one spawn.
- camera(*, orthographic_size) — unique isometric camera (4.0–100.0). Exactly one.
- freeze() -> EnvironmentModel — validate and freeze.

Binding rules: coordinates |v| ≤ 10000; materials from curated list \
{default, grass, dirt, stone, rock, wood, water, snow} or your declared \
custom materials. Kits (name/category/blocking): \
stone_ruin/structure/true, timber_hut/structure/true, watchtower/structure/true, \
obelisk/landmark/false, banner/landmark/false, pine/vegetation/true (conical tree), \
oak/vegetation/true (round canopy tree), boulder/vegetation/true (rock cluster), \
shrub/vegetation/false.

VISUAL THEMES (visuals only — colliders and navigation never change):
- material(name, *, color="#RRGGBB", emission_color=None, \
emission_strength≤4, roughness 0..1, metallic 0..1) — up to 16 custom \
materials (no shaders, textures, or alpha). Declare BEFORE first use.
- palette(ground=, path=, vegetation=, rock=, wood=, snow=, water=, \
structure=, accent=, sky_top=, sky_horizon=, sun=) — '#RRGGBB' recolors per \
category: accent recolors landmark kits, structure recolors stone; omitted \
keys keep defaults.
- lighting(ambient_color=, ambient_energy 0..2, sun_color=, sun_energy 0..2, \
sky_top=, sky_horizon=) — bounded scene lighting.
- custom_kit(name, *, category, blocking, parts=[...]) — up to 12 kits of \
1–16 primitive parts: BoxPart(offset,size,material,yaw), \
CylinderPart(offset,radius,height,material,yaw), ConePart(same), \
SpherePart(offset,radius,material); extents ≤ 8 m. Blocking kits get \
conservative colliders. Use via prop/scatter/landmark per category.
Express themes (alien, volcanic, bioluminescent, surreal) through palette, \
materials, and custom kits FIRST — not through extra object density.

COMPOSITION (model places; harness measures only):
Vary spacing and sizes on purpose (`prop(scale=...)`, `scatter(scale_range=...)`). \
Group with intent rather than even salting. Keep the spawn→landmark approach open. \
"""

_API_WORKFLOW = """\
After your FIRST clean compile, call audit_render once and check the \
screenshots against the user's request (are things inside/outside/open/closed \
where asked?). Apply AT MOST one round of fixes, recompile, then IMMEDIATELY \
call simulate_navigation — do not keep polishing. Prefer SEARCH/REPLACE \
patches (diff line numbers are error-prone). Judge composition (clusters, voids, sightlines, scale variety, \
palette) from the images and aesthetics numbers; patch only for clear improvements \
(budget 2 audits). The loop auto-seals after an all-pass simulate — audit first.

TOOLS (call one per turn after an initial full program):
- read_program — read current source.
- patch_program(patch) — unified diff OR search/replace with delimiters \
<<<<<<< SEARCH / ======= / >>>>>>> REPLACE (search must occur exactly once).
- compile_environment — worker + static stages program→scene (V1–V5).
- probe_environment(query) — read-only: "component <id>" | "bounds" | "blockers" | \
"spawn" | "aesthetics" | "route x1 z1 x2 z2".
- render_environment(view) — "isometric" or "topdown" artifact refs.
- audit_render — bounded isometric+topdown JPEG feedback + aesthetics (budget 2).
- simulate_navigation — live stages materialization→camera (connectivity + traverse).

"""

_AGENT_WORKFLOW_NOTE = """\
After each `author step`, open the render PNGs it prints and judge the \
composition (clusters, voids, sightlines, scale variety, palette) against \
the user's request before editing further.

"""

_WORKED_EXAMPLE = """\
WORKED EXAMPLE (minimal valid program):
```python
from envmaker.sdk import EnvironmentBuilder, Polygon2D

def build_environment():
    b = EnvironmentBuilder("meadow", seed=3, style="pasture")
    b.ground("field", footprint=Polygon2D([(-12,-12),(12,-12),(12,12),(-12,12)]), material="grass")
    b.path("lane", points=[(-8,0),(0,0),(6,4)], width=1.2, material="dirt")
    b.wall("hedge", start=(-2,4), end=(4,4), height=1.4, thickness=0.4, material="wood")
    b.obstacle("rock", footprint=Polygon2D([(3,-5),(5,-5),(5,-3),(3,-3)]), height=1.0, material="rock")
    b.structure("hut", footprint=Polygon2D([(6,6),(10,6),(10,10),(6,10)]), height=2.4, kit="timber_hut")
    b.landmark("obelisk_goal", position=(8,-8), kit="obelisk")
    b.scatter("trees", region="field", kit="pine", count=3, min_spacing=3.0)
    b.spawn("hero", position=(-8,-8))
    b.camera(orthographic_size=18.0)
    return b.freeze()

environment = build_environment()
```

REPAIR GUIDANCE:
- The ground IS the world: everything, including the spawn, must sit inside \
it with 0.4 m margin. If a prompt says spawn "outside" a structure, keep the \
spawn on the ground but outside that structure's walls — or declare a bigger \
ground. Spawn errors name the offending blocker and its bounds; use them.
- Read each signal's code, measurements, and guidance; patch minimally.
"""

_API_REPAIR = """\
- Prefer search/replace for single-line fixes; recompile before simulating.
"""

_REPAIR_TAIL = """\
- Spawn-in-blocker fails at freeze (program stage); disconnected clear ground fails \
clear-ground fraction (v6.clear_ground_fraction). Open corridors, then re-simulate.
"""

_FIRST_MOVE = """\

FIRST MOVE (mandatory): your first reply must be the COMPLETE program in one \
```python fenced block. There is no write tool: a fenced code reply is the ONLY \
way to create or fully rewrite the program. Never call tools before the program \
exists; after that, call exactly one tool per reply and start with \
compile_environment.
"""

SYSTEM_PROMPT = (
    _PREAMBLE
    + _SDK_CONTRACT
    + _API_WORKFLOW
    + _WORKED_EXAMPLE
    + _API_REPAIR
    + _REPAIR_TAIL
    + _FIRST_MOVE
)

AGENT_CONTRACT = (
    _PREAMBLE
    + _SDK_CONTRACT
    + _AGENT_WORKFLOW_NOTE
    + _WORKED_EXAMPLE
    + _REPAIR_TAIL
)

assert len(SYSTEM_PROMPT) <= 8000


def build_user_prompt(prompt: str, seed: int) -> str:
    """Build the user turn that carries the authoring prompt and seed binding."""

    return (
        f"{prompt}\n\nuse seed {seed} in EnvironmentBuilder(...).\n\n"
        "Reply now with the complete program in a single ```python fenced block."
    )
