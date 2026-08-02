"""Agent tool surface coverage for EnvMaker authoring tools."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from envmaker.agent.tools import AuditResult, ToolContext, ToolSurface, _AUDIT_B64_CAP
from envmaker.core.artifacts import ArtifactManifest, ArtifactRef
from envmaker.core.definition import HardStage
from envmaker.core.episode import (
    ConnectorType,
    EpisodeResult,
    NavigationProbe,
    TerminalReason,
)
from envmaker.core.model import EnvironmentModel, Transform3D, Vec3
from envmaker.core.program import ResourceLimits
from envmaker.core.scene_spec import (
    BoxVisual,
    CameraSpec,
    CandidateScene,
    ColliderShape,
    ColliderSpec,
    GodotSceneSpec,
    PlaneVisual,
    SceneNode,
)
from envmaker.runlog import RunLog
from envmaker.sdk import EnvironmentBuilder, Polygon2D
from envmaker.validation import StaticValidation


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_SOURCE = (_REPO_ROOT / "examples" / "demo" / "environment.py").read_text(
    encoding="utf-8"
)


def _limits() -> ResourceLimits:
    return ResourceLimits(
        cpu_seconds=5.0,
        memory_mb=256,
        output_bytes=65536,
        wall_seconds=10.0,
    )


def _probe() -> NavigationProbe:
    return NavigationProbe(
        target_landmark_id="wanderer",
        success_radius_m=1.5,
        max_ticks=200,
        action_repeat=1,
        allowed_connector_types=(ConnectorType.STAIRS,),
        stuck_timeout_ticks=20,
        terminal_reasons=(TerminalReason.ARRIVED, TerminalReason.TIMEOUT),
    )


def _surface(
    tmp_path: Path,
    *,
    source: str = _DEMO_SOURCE,
    driver: object | None = None,
    probe: NavigationProbe | None = None,
) -> ToolSurface:
    run_dir = tmp_path / "run"
    context = ToolContext(
        source=source,
        limits=_limits(),
        run_dir=run_dir,
        runlog=RunLog(run_dir / "trace.jsonl"),
        driver=driver,
        probe=probe,
    )
    return ToolSurface(context)


class _HappyDriver:
    def load_candidate(self, candidate: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True)

    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        return None

    def connected_clear_ground_fraction(self) -> float:
        return 0.9

    def navigate(self, probe: NavigationProbe) -> EpisodeResult:
        return EpisodeResult(
            probe_fingerprint=probe.probe_fingerprint,
            terminal_reason=TerminalReason.ARRIVED,
            ticks_used=20,
            final_geodesic_distance_m=0.2,
            path_length_m=12.0,
            collisions=0,
            stuck_recoveries=0,
        )

    def render(self, view: str) -> ArtifactRef:
        digest = "c" * 64
        return ArtifactRef(
            path=f"renders/{view}.png",
            media_type="image/png",
            byte_count=256,
            blake2b256=digest,
            sha256=digest,
            producer="stub",
            toolchain_version="test",
        )


def _write_tiny_png(path: Path, *, color: tuple[int, int, int] = (40, 120, 80)) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, format="PNG")
    return path.stat().st_size


class _PngAuditDriver:
    """Stub driver that writes real small PNGs for audit_render."""

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self.loaded = False

    def load_candidate(self, candidate: object) -> SimpleNamespace:
        del candidate
        self.loaded = True
        return SimpleNamespace(ok=True)

    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        return None

    def connected_clear_ground_fraction(self) -> float:
        return 0.9

    def navigate(self, probe: NavigationProbe) -> EpisodeResult:
        return EpisodeResult(
            probe_fingerprint=probe.probe_fingerprint,
            terminal_reason=TerminalReason.ARRIVED,
            ticks_used=20,
            final_geodesic_distance_m=0.2,
            path_length_m=12.0,
            collisions=0,
            stuck_recoveries=0,
        )

    def render(self, view: str) -> ArtifactRef:
        rel = f"renders/{view}.png"
        abs_path = self._run_dir / rel
        byte_count = _write_tiny_png(
            abs_path,
            color=(80, 40, 120) if view == "isometric" else (40, 120, 80),
        )
        digest = hashlib.sha256(abs_path.read_bytes()).hexdigest()
        return ArtifactRef(
            path=rel,
            media_type="image/png",
            byte_count=byte_count,
            blake2b256=digest,
            sha256=digest,
            producer="stub",
            toolchain_version="test",
        )


def test_read_program_returns_source(tmp_path: Path) -> None:
    tools = _surface(tmp_path, source="print('hi')\n")
    assert tools.read_program() == "print('hi')\n"


def test_read_program_rejects_oversize_source(tmp_path: Path) -> None:
    tools = _surface(tmp_path, source="x" * (64 * 1024 + 1))
    try:
        tools.read_program()
        raised = False
    except ValueError as exc:
        raised = True
        assert "64" in str(exc).lower() or "cap" in str(exc)
    assert raised is True


def test_patch_search_replace_and_unified_diff(tmp_path: Path) -> None:
    source = "alpha\nbeta\ngamma\n"
    tools = _surface(tmp_path, source=source)
    replaced = tools.patch_program(
        "<<<<<<< SEARCH\nbeta\n=======\nBETA\n>>>>>>> REPLACE"
    )
    assert replaced.ok is True
    assert "BETA" in tools.context.source
    assert replaced.new_source_fingerprint

    diff = (
        "--- a/environment.py\n"
        "+++ b/environment.py\n"
        "@@ -1,3 +1,3 @@\n"
        " alpha\n"
        "-BETA\n"
        "+beta\n"
        " gamma\n"
    )
    restored = tools.patch_program(diff)
    assert restored.ok is True
    assert tools.context.source == source


def test_patch_rejects_mismatch_and_caps(tmp_path: Path) -> None:
    tools = _surface(tmp_path, source="one\ntwo\n")
    mismatch = tools.patch_program(
        "<<<<<<< SEARCH\nmissing\n=======\nnew\n>>>>>>> REPLACE"
    )
    assert mismatch.ok is False
    assert tools.context.source == "one\ntwo\n"

    oversize_patch = tools.patch_program("x" * (16 * 1024 + 1))
    assert oversize_patch.ok is False

    tools.context.source = "y" * (64 * 1024 - 10)
    huge = tools.patch_program(
        "<<<<<<< SEARCH\n"
        + "y" * 5
        + "\n=======\n"
        + "z" * 100
        + "\n>>>>>>> REPLACE"
    )
    assert huge.ok is False


def test_compile_demo_passes_five_stages(tmp_path: Path) -> None:
    tools = _surface(tmp_path)
    result = tools.compile_environment()
    assert result.ok is True
    assert result.stage_outcomes[HardStage.PROGRAM.value] is True
    assert result.stage_outcomes[HardStage.SCENE.value] is True
    assert len(result.signals) <= 32
    assert result.model_fingerprint
    assert result.candidate_fingerprint


def test_probe_queries_and_errors(tmp_path: Path) -> None:
    tools = _surface(tmp_path)
    before = tools.probe_environment("bounds")
    assert before.ok is False
    assert "compile" in before.reason.lower()

    assert tools.compile_environment().ok is True
    component = tools.probe_environment("component green")
    assert component.ok is True
    assert component.data["payload"]["component"] == "ground"

    bounds = tools.probe_environment("bounds")
    assert bounds.ok is True
    assert "node_count" in bounds.data

    blockers = tools.probe_environment("blockers")
    assert blockers.ok is True
    assert isinstance(blockers.data["blockers"], list)

    spawn = tools.probe_environment("spawn")
    assert spawn.ok is True
    assert "position" in spawn.data

    route = tools.probe_environment("route -14 -14 16 16")
    assert route.ok is True
    assert "distance" in route.data

    aesthetics = tools.probe_environment("aesthetics")
    assert aesthetics.ok is True
    assert "cluster_score" in aesthetics.data
    assert "instance_count" in aesthetics.data

    unknown = tools.probe_environment("wat")
    assert unknown.ok is False
    assert "component" in unknown.reason
    assert "aesthetics" in unknown.reason


def test_render_and_simulate_with_stub_driver(tmp_path: Path) -> None:
    tools = _surface(tmp_path, driver=_HappyDriver(), probe=_probe())
    assert tools.compile_environment().ok is True
    rendered = tools.render_environment("isometric")
    assert rendered.ok is True
    assert rendered.artifact_path
    assert rendered.blake2b256
    assert rendered.byte_count > 0

    nav = tools.simulate_navigation()
    assert nav.ok is True
    assert nav.stage_outcomes[HardStage.MATERIALIZATION.value] is True
    assert nav.terminal_reason == TerminalReason.ARRIVED.value


def test_render_and_simulate_without_driver(tmp_path: Path) -> None:
    tools = _surface(tmp_path)
    assert tools.compile_environment().ok is True
    rendered = tools.render_environment("topdown")
    assert rendered.ok is False
    assert "driver unavailable" in rendered.reason.lower()

    nav = tools.simulate_navigation()
    assert nav.ok is False
    assert "driver unavailable" in nav.reason.lower()


def test_audit_render_happy_path(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    driver = _PngAuditDriver(run_dir)
    tools = _surface(tmp_path, driver=driver, probe=_probe())
    assert tools.compile_environment().ok is True
    result = tools.audit_render()
    assert isinstance(result, AuditResult)
    assert result.ok is True
    assert driver.loaded is True
    assert len(result.refs) == 2
    assert len(result.images_b64) == 2
    assert all(len(item.encode("utf-8")) <= _AUDIT_B64_CAP for item in result.images_b64)
    for key in (
        "instance_count",
        "cluster_score",
        "coverage_fraction",
        "sightline_clear",
        "guidance",
    ):
        assert key in result.aesthetics


def test_audit_render_budget_exhausted(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    tools = _surface(tmp_path, driver=_PngAuditDriver(run_dir), probe=_probe())
    assert tools.compile_environment().ok is True
    assert tools.audit_render().ok is True
    assert tools.audit_render().ok is True
    third = tools.audit_render()
    assert third.ok is False
    assert "audit budget exhausted" in third.reason


def test_audit_render_without_driver(tmp_path: Path) -> None:
    tools = _surface(tmp_path)
    assert tools.compile_environment().ok is True
    result = tools.audit_render()
    assert result.ok is False
    assert "driver unavailable" in result.reason.lower()


def test_every_tool_call_is_logged_with_redaction(tmp_path: Path) -> None:
    tools = _surface(tmp_path, source="token = 'sk-ABCDEFGH1234'\nprint(1)\n")
    tools.read_program()
    tools.patch_program(
        "<<<<<<< SEARCH\nprint(1)\n=======\nprint(2)\n>>>>>>> REPLACE"
    )
    tools.context.source = _DEMO_SOURCE
    tools.compile_environment()
    tools.probe_environment("bounds")
    tools.render_environment("isometric")
    tools.simulate_navigation()

    kinds = [event["kind"] for event in tools.context.runlog.events()]
    assert "tool.read_program" in kinds
    assert "tool.patch_program" in kinds
    assert "tool.compile_environment" in kinds
    assert "tool.probe_environment" in kinds
    assert "tool.render_environment" in kinds
    assert "tool.simulate_navigation" in kinds

    blob = str(tools.context.runlog.events())
    assert "sk-ABCDEFGH1234" not in blob
    assert "[redacted]" in blob


class _LowFractionDriver(_HappyDriver):
    def connected_clear_ground_fraction(self) -> float:
        return 0.31


class _NavReadyFailDriver(_HappyDriver):
    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        raise RuntimeError("navigation bake failed")


class _LongExceptionDriver(_HappyDriver):
    def load_candidate(self, candidate: object) -> SimpleNamespace:
        raise RuntimeError("x" * 5000)


def test_simulate_navigation_distinguishes_fraction_from_bake_failure(
    tmp_path: Path,
) -> None:
    low = _surface(tmp_path / "low", driver=_LowFractionDriver(), probe=_probe())
    assert low.compile_environment().ok is True
    low_result = low.simulate_navigation()

    bake = _surface(tmp_path / "bake", driver=_NavReadyFailDriver(), probe=_probe())
    assert bake.compile_environment().ok is True
    bake_result = bake.simulate_navigation()

    assert low_result.ok is False
    assert bake_result.ok is False
    low_codes = {signal.code for signal in low_result.signals}
    bake_codes = {signal.code for signal in bake_result.signals}
    assert low_codes != bake_codes
    assert low_result.signals != bake_result.signals
    assert "v6.clear_ground_fraction" in low_codes
    assert "v6.clear_ground_fraction" not in bake_codes
    assert "v6.navigation_not_ready" in bake_codes
    fraction_signal = next(
        signal
        for signal in low_result.signals
        if signal.code == "v6.clear_ground_fraction"
    )
    assert fraction_signal.measurements["clear_ground_fraction"] == pytest.approx(0.31)
    assert low.context.runtime_reports
    assert bake.context.runtime_reports


def test_simulate_navigation_contains_long_exception_text(tmp_path: Path) -> None:
    tools = _surface(tmp_path, driver=_LongExceptionDriver(), probe=_probe())
    assert tools.compile_environment().ok is True
    result = tools.simulate_navigation()
    assert result.ok is False
    assert result.signals
    for signal in result.signals:
        assert len(signal.message) <= 2000


def test_zero_context_insertion_hunk_rejected(tmp_path: Path) -> None:
    source = "alpha\nbeta\n"
    tools = _surface(tmp_path, source=source)
    patch = (
        "--- a/environment.py\n"
        "+++ b/environment.py\n"
        "@@ -1,0 +1,1 @@\n"
        "+inserted\n"
    )
    result = tools.patch_program(patch)
    assert result.ok is False
    assert "context" in result.reason.lower()
    assert tools.context.source == source


def test_probe_payload_does_not_alias_model(tmp_path: Path) -> None:
    tools = _surface(tmp_path)
    assert tools.compile_environment().ok is True
    assert tools.context.static is not None
    assert tools.context.static.model is not None
    before = tools.context.static.model.model_fingerprint
    probed = tools.probe_environment("component green")
    assert probed.ok is True
    payload = probed.data["payload"]
    assert isinstance(payload, dict)
    payload["component"] = "mutated"
    if "footprint" in payload:
        payload["footprint"][0][0] = 999.0
    model = tools.context.static.model
    green = next(c for c in model.components if c.semantic_id == "green")
    assert green.payload["component"] == "ground"
    assert model.model_fingerprint == before
    # Fingerprint still verifies on round-trip.
    EnvironmentModel.model_validate(model.model_dump())


def test_rotated_blocker_route_ignores_aabb_corner_only(tmp_path: Path) -> None:
    yaw = math.pi / 4.0
    cos_yaw = math.cos(yaw)
    sin_yaw = math.sin(yaw)
    ground = SceneNode(
        node_id="ground",
        semantic_id="ground",
        transform=Transform3D(
            origin=Vec3(x=0.0, y=0.0, z=0.0),
            basis_x=Vec3(x=1.0, y=0.0, z=0.0),
            basis_y=Vec3(x=0.0, y=1.0, z=0.0),
            basis_z=Vec3(x=0.0, y=0.0, z=1.0),
        ),
        visual=PlaneVisual(size_x=20.0, size_z=20.0, material="grass"),
        collider=ColliderSpec(
            shape=ColliderShape.BOX,
            dimensions={"x": 20.0, "y": 0.5, "z": 20.0},
        ),
        navmesh_contributor=True,
    )
    blocker = SceneNode(
        node_id="diamond",
        semantic_id="diamond",
        transform=Transform3D(
            origin=Vec3(x=0.0, y=0.5, z=0.0),
            basis_x=Vec3(x=cos_yaw, y=0.0, z=-sin_yaw),
            basis_y=Vec3(x=0.0, y=1.0, z=0.0),
            basis_z=Vec3(x=sin_yaw, y=0.0, z=cos_yaw),
        ),
        visual=BoxVisual(size=(2.0, 1.0, 2.0), material="stone"),
        collider=ColliderSpec(
            shape=ColliderShape.BOX,
            dimensions={"x": 2.0, "y": 1.0, "z": 2.0},
        ),
        navmesh_contributor=True,
    )
    spawn = SceneNode(
        node_id="hero",
        semantic_id="hero",
        transform=Transform3D(
            origin=Vec3(x=-6.0, y=0.5, z=-6.0),
            basis_x=Vec3(x=1.0, y=0.0, z=0.0),
            basis_y=Vec3(x=0.0, y=1.0, z=0.0),
            basis_z=Vec3(x=0.0, y=0.0, z=1.0),
        ),
    )
    candidate = CandidateScene(
        scene=GodotSceneSpec(
            nodes=(ground, blocker, spawn),
            camera=CameraSpec(follow_semantic_id="hero", orthographic_size=12.0),
            controller_semantic_id="hero",
        ),
        manifest=ArtifactManifest(root="artifacts", entries=()),
    )
    model = (
        EnvironmentBuilder("route", seed=1)
        .ground(
            "ground",
            footprint=Polygon2D([(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]),
            material="grass",
        )
        .spawn("hero", position=(-6.0, -6.0))
        .camera(orthographic_size=12.0)
        .freeze()
    )
    tools = _surface(tmp_path)
    tools.context.static = StaticValidation(
        model=model,
        candidate=candidate,
        reports=(),
    )
    # World AABB corner of the 45° 2x2 box is near (√2, √2)≈(1.41, 1.41).
    # Segment stays inside that AABB corner band but outside the true OBB.
    route = tools.probe_environment("route 1.3 1.3 1.4 1.35")
    assert route.ok is True
    assert "diamond" not in route.data["blockers_intersected"]


def _aesthetics_source(*, clumpy: bool, blocked: bool) -> str:
    props = []
    if clumpy:
        spots = [
            (0.0, 0.0),
            (0.4, 0.0),
            (0.0, 0.4),
            (0.4, 0.4),
            (0.2, 0.2),
            (0.1, 0.3),
            (8.0, 8.0),
            (-8.0, -8.0),
        ]
    else:
        spots = [
            (-9.0, -9.0),
            (-9.0, 0.0),
            (-9.0, 9.0),
            (0.0, -9.0),
            (0.0, 9.0),
            (9.0, -9.0),
            (9.0, 0.0),
            (9.0, 9.0),
        ]
    for index, (x, z) in enumerate(spots):
        props.append(
            f'    b.prop("bush_{index}", kit="shrub", position=({x}, {z}))\n'
        )
    wall = ""
    if blocked:
        wall = (
            '    b.wall("gate", start=(-1.0, 4.0), end=(1.0, 4.0), '
            "height=2.0, thickness=0.5, material=\"stone\")\n"
        )
    return (
        "from envmaker.sdk import EnvironmentBuilder, Polygon2D\n\n"
        "def build_environment():\n"
        '    b = EnvironmentBuilder("aesthetics", seed=3)\n'
        '    b.ground("field", footprint=Polygon2D('
        "[(-12,-12),(12,-12),(12,12),(-12,12)]), material=\"grass\")\n"
        f"{wall}"
        '    b.landmark("goal", position=(0.0, 9.0), kit="obelisk")\n'
        + "".join(props)
        + '    b.spawn("hero", position=(0.0, -9.0))\n'
        "    b.camera(orthographic_size=20.0)\n"
        "    return b.freeze()\n\n"
        "environment = build_environment()\n"
    )


def test_aesthetics_cluster_and_sightline(tmp_path: Path) -> None:
    clumpy = _surface(tmp_path, source=_aesthetics_source(clumpy=True, blocked=False))
    assert clumpy.compile_environment().ok is True
    clumpy_probe = clumpy.probe_environment("aesthetics")
    assert clumpy_probe.ok is True
    assert clumpy_probe.data["instance_count"] == 8
    assert float(clumpy_probe.data["cluster_score"]) > 0.6

    even = _surface(tmp_path, source=_aesthetics_source(clumpy=False, blocked=False))
    assert even.compile_environment().ok is True
    even_probe = even.probe_environment("aesthetics")
    assert even_probe.ok is True
    assert float(even_probe.data["cluster_score"]) < 0.35
    assert even_probe.data["sightline_clear"] is True
    assert even_probe.data["sightline_blocker"] is None

    blocked = _surface(tmp_path, source=_aesthetics_source(clumpy=False, blocked=True))
    assert blocked.compile_environment().ok is True
    blocked_probe = blocked.probe_environment("aesthetics")
    assert blocked_probe.ok is True
    assert blocked_probe.data["sightline_clear"] is False
    assert blocked_probe.data["sightline_blocker"]
    assert any("sightline" in tip for tip in blocked_probe.data["guidance"])


def test_aesthetics_requires_compile(tmp_path: Path) -> None:
    tools = _surface(tmp_path)
    before = tools.probe_environment("aesthetics")
    assert before.ok is False
    assert "compile" in before.reason.lower()


def test_audit_resolves_runtime_subdir_and_waits_for_nav(tmp_path):
    from PIL import Image

    from envmaker.agent.tools import ToolContext, ToolSurface
    from envmaker.core.program import ResourceLimits
    from envmaker.runlog import RunLog
    from envmaker.validation import validate_static

    demo_source = (
        Path(__file__).resolve().parents[2] / "examples/demo/environment.py"
    ).read_text()
    context = ToolContext(
        source=demo_source,
        limits=ResourceLimits(
            cpu_seconds=10, memory_mb=512, output_bytes=262144, wall_seconds=20
        ),
        run_dir=tmp_path,
        runlog=RunLog(tmp_path / "runlog.jsonl"),
    )
    surface = ToolSurface(context)
    assert surface.compile_environment().ok

    artifacts_dir = tmp_path / "runtime" / "artifacts"
    artifacts_dir.mkdir(parents=True)
    calls: list[str] = []

    class _Driver:
        def load_candidate(self, candidate):
            calls.append("load")
            return None

        def wait_navigation_ready(self, timeout):
            calls.append("wait")
            return True

        def render(self, view):
            name = f"render-{view}.png"
            Image.new("RGB", (64, 64), (90, 140, 80)).save(
                artifacts_dir / name
            )
            data = (artifacts_dir / name).read_bytes()
            import hashlib

            return type(
                "Ref",
                (),
                {
                    "path": f"artifacts/{name}",
                    "blake2b256": hashlib.blake2b(
                        data, digest_size=32
                    ).hexdigest(),
                    "byte_count": len(data),
                },
            )()

    context.driver = _Driver()
    result = surface.audit_render()
    assert result.ok, result.reason
    assert calls[:2] == ["load", "wait"], "must settle nav before rendering"
    assert len(result.images_b64) == 2
