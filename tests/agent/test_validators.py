"""Hard-stage validators for EnvMaker static and duck-typed runtime checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from envmaker.core.artifacts import ArtifactRef
from envmaker.core.definition import HardStage, seal_definition
from envmaker.core.episode import (
    ConnectorType,
    EpisodeResult,
    NavigationProbe,
    TerminalReason,
)
from envmaker.core.model import (
    ComponentKind,
    EnvironmentModel,
    SemanticComponent,
    Transform3D,
    Vec3,
)
from envmaker.core.program import EnvironmentProgram, ProviderInfo, ResourceLimits
from envmaker.core.requirements import PromptRequirementSet
from envmaker.core.scene_spec import CandidateScene, GodotSceneSpec, SceneNode
from envmaker.core.signals import SignalSeverity
from envmaker.sdk import SDK_VERSION, EnvironmentBuilder, Polygon2D, compile_environment_model
from envmaker.validation import (
    StaticValidation,
    full_bundle,
    validate_candidate,
    validate_model,
    validate_static,
)


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


def _tiny_model() -> EnvironmentModel:
    return (
        EnvironmentBuilder("tiny", seed=1)
        .ground(
            "ground",
            footprint=Polygon2D([(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]),
            material="grass",
        )
        .obstacle(
            "rock",
            footprint=Polygon2D([(1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0)]),
            height=1.0,
            material="rock",
        )
        .spawn("hero", position=(-3.0, -3.0))
        .camera(orthographic_size=12.0)
        .freeze()
    )


def _probe() -> NavigationProbe:
    return NavigationProbe(
        target_landmark_id="hero",
        success_radius_m=1.0,
        max_ticks=100,
        action_repeat=1,
        allowed_connector_types=(ConnectorType.STAIRS,),
        stuck_timeout_ticks=10,
        terminal_reasons=(TerminalReason.ARRIVED, TerminalReason.TIMEOUT),
    )


def _report_map(static: StaticValidation) -> dict[HardStage, object]:
    return {report.stage: report for report in static.reports}


def test_program_stage_fails_on_crashing_source() -> None:
    source = "raise RuntimeError('boom')\n"
    static = validate_static(source, limits=_limits())
    reports = _report_map(static)
    assert HardStage.PROGRAM in reports
    assert reports[HardStage.PROGRAM].passed is False
    codes = {signal.code for signal in reports[HardStage.PROGRAM].signals}
    assert "v1.program_failed" in codes
    failure = next(
        signal
        for signal in reports[HardStage.PROGRAM].signals
        if signal.code == "v1.program_failed"
    )
    assert failure.severity is SignalSeverity.FAILURE
    assert "exit_reason" in failure.measurements
    assert "stderr_blake2b256" in failure.measurements
    assert HardStage.SDK_MODEL not in reports
    assert static.model is None
    assert static.candidate is None


def test_sdk_model_stage_fails_on_wrong_sdk_version() -> None:
    model = EnvironmentModel(
        name="tiny",
        style="flat-shaded minimal",
        seed=1,
        sdk_version="9.9.9",
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
                semantic_id="hero",
                kind=ComponentKind.DYNAMIC_ENTITY,
                payload={"component": "spawn", "position": [0.0, 0.0]},
            ),
            SemanticComponent(
                semantic_id="camera",
                kind=ComponentKind.PRESENTATION,
                payload={"component": "camera", "orthographic_size": 12.0},
            ),
        ),
    )
    static = validate_model(model)
    reports = _report_map(static)
    assert reports[HardStage.SDK_MODEL].passed is False
    codes = {signal.code for signal in reports[HardStage.SDK_MODEL].signals}
    assert "v2.sdk_version_mismatch" in codes
    assert HardStage.SEMANTIC not in reports


def test_semantic_stage_fails_on_dangling_scatter_region() -> None:
    model = (
        EnvironmentBuilder("tiny", seed=1)
        .ground(
            "ground",
            footprint=Polygon2D([(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]),
            material="grass",
        )
        .scatter("pines", region="ground", kit="pine", count=2, min_spacing=1.0)
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=12.0)
        .freeze()
    )
    components = []
    for component in model.components:
        if component.semantic_id == "pines":
            payload = dict(component.payload)
            payload["region"] = "missing_ground"
            components.append(
                SemanticComponent(
                    semantic_id=component.semantic_id,
                    kind=component.kind,
                    payload=payload,
                )
            )
        else:
            components.append(component)
    broken = EnvironmentModel(
        name=model.name,
        style=model.style,
        seed=model.seed,
        sdk_version=model.sdk_version,
        components=tuple(components),
    )
    static = validate_model(broken)
    reports = _report_map(static)
    assert reports[HardStage.SDK_MODEL].passed is True
    assert reports[HardStage.SEMANTIC].passed is False
    failure = next(
        signal
        for signal in reports[HardStage.SEMANTIC].signals
        if signal.severity is SignalSeverity.FAILURE
    )
    assert failure.code == "v3.scatter_region"
    assert "pines" in failure.subject_ids
    assert HardStage.ASSET not in reports


def test_asset_stage_fails_on_non_finite_origin() -> None:
    model = _tiny_model()
    candidate = compile_environment_model(model)
    nodes = []
    for node in candidate.scene.nodes:
        if node.semantic_id == "rock":
            bad_origin = Vec3.model_construct(x=float("nan"), y=0.5, z=2.0)
            bad_transform = Transform3D.model_construct(
                origin=bad_origin,
                basis_x=node.transform.basis_x,
                basis_y=node.transform.basis_y,
                basis_z=node.transform.basis_z,
            )
            nodes.append(
                SceneNode.model_construct(
                    node_id=node.node_id,
                    semantic_id=node.semantic_id,
                    transform=bad_transform,
                    mesh=node.mesh,
                    collider=node.collider,
                    navmesh_contributor=node.navmesh_contributor,
                    fade_group=node.fade_group,
                    visual=node.visual,
                )
            )
        else:
            nodes.append(node)
    broken_scene = GodotSceneSpec.model_construct(
        nodes=tuple(nodes),
        camera=candidate.scene.camera,
        controller_semantic_id=candidate.scene.controller_semantic_id,
    )
    broken = CandidateScene.model_construct(
        scene=broken_scene,
        manifest=candidate.manifest,
        pre_reports=candidate.pre_reports,
        candidate_fingerprint=candidate.candidate_fingerprint,
    )
    static = validate_model(model, candidate=broken)
    reports = _report_map(static)
    assert reports[HardStage.SDK_MODEL].passed is True
    assert reports[HardStage.SEMANTIC].passed is True
    assert reports[HardStage.ASSET].passed is False
    failure = next(
        signal
        for signal in reports[HardStage.ASSET].signals
        if signal.code == "v4.non_finite_transform"
    )
    assert "rock" in failure.subject_ids
    assert HardStage.SCENE not in reports


def test_scene_stage_fails_when_spawn_on_ground_edge() -> None:
    model = _tiny_model()
    candidate = compile_environment_model(model)
    nodes = []
    for node in candidate.scene.nodes:
        if node.semantic_id == "hero":
            # Ground is [-5, 5] on X/Z; edge spawn must fail after agent-radius inset.
            nodes.append(
                node.model_copy(
                    update={
                        "transform": node.transform.model_copy(
                            update={"origin": Vec3(x=5.0, y=0.5, z=0.0)}
                        )
                    }
                )
            )
        else:
            nodes.append(node)
    mutated = CandidateScene(
        scene=GodotSceneSpec(
            nodes=tuple(nodes),
            camera=candidate.scene.camera,
            controller_semantic_id=candidate.scene.controller_semantic_id,
        ),
        manifest=candidate.manifest,
    )
    static = validate_model(model, candidate=mutated)
    reports = _report_map(static)
    assert reports[HardStage.ASSET].passed is True
    assert reports[HardStage.SCENE].passed is False
    assert any(
        signal.code == "v5.spawn_outside_ground"
        for signal in reports[HardStage.SCENE].signals
    )


def test_scene_stage_fails_when_spawn_intersects_blocker() -> None:
    model = _tiny_model()
    candidate = compile_environment_model(model)
    nodes = []
    for node in candidate.scene.nodes:
        if node.semantic_id == "hero":
            nodes.append(
                node.model_copy(
                    update={
                        "transform": node.transform.model_copy(
                            update={"origin": Vec3(x=2.0, y=0.5, z=2.0)}
                        )
                    }
                )
            )
        else:
            nodes.append(node)
    mutated = CandidateScene(
        scene=GodotSceneSpec(
            nodes=tuple(nodes),
            camera=candidate.scene.camera,
            controller_semantic_id=candidate.scene.controller_semantic_id,
        ),
        manifest=candidate.manifest,
    )
    static = validate_model(model, candidate=mutated)
    reports = _report_map(static)
    assert reports[HardStage.SDK_MODEL].passed is True
    assert reports[HardStage.SEMANTIC].passed is True
    assert reports[HardStage.ASSET].passed is True
    assert reports[HardStage.SCENE].passed is False
    failure = next(
        signal
        for signal in reports[HardStage.SCENE].signals
        if signal.code == "v5.spawn_intersects_blocker"
    )
    assert "hero" in failure.subject_ids
    assert "rock" in failure.subject_ids


def test_demo_fixture_passes_static_stages() -> None:
    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert len(static.reports) == 5
    assert all(report.passed for report in static.reports)
    assert static.model is not None
    assert static.candidate is not None
    stages = [report.stage for report in static.reports]
    assert stages == [
        HardStage.PROGRAM,
        HardStage.SDK_MODEL,
        HardStage.SEMANTIC,
        HardStage.ASSET,
        HardStage.SCENE,
    ]


class _HappyDriver:
    def load_candidate(self, candidate: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True)

    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        return None

    def connected_navigable_fraction(self) -> float:
        return 0.8

    def navigate(self, probe: NavigationProbe) -> EpisodeResult:
        return EpisodeResult(
            probe_fingerprint=probe.probe_fingerprint,
            terminal_reason=TerminalReason.ARRIVED,
            ticks_used=12,
            final_geodesic_distance_m=0.1,
            path_length_m=8.0,
            collisions=0,
            stuck_recoveries=0,
        )

    def render(self, view: str) -> ArtifactRef:
        digest = "a" * 64
        return ArtifactRef(
            path=f"renders/{view}.png",
            media_type="image/png",
            byte_count=128,
            blake2b256=digest,
            sha256=digest,
            producer="stub",
            toolchain_version="test",
        )


class _LoadFailDriver(_HappyDriver):
    def load_candidate(self, candidate: object) -> SimpleNamespace:
        raise RuntimeError("load failed")


class _NavFailDriver(_HappyDriver):
    def connected_navigable_fraction(self) -> float:
        return 0.3


class _ControllerFailDriver(_HappyDriver):
    def navigate(self, probe: NavigationProbe) -> EpisodeResult:
        return EpisodeResult(
            probe_fingerprint=probe.probe_fingerprint,
            terminal_reason=TerminalReason.TIMEOUT,
            ticks_used=100,
            final_geodesic_distance_m=4.0,
            path_length_m=1.0,
            collisions=0,
            stuck_recoveries=0,
        )


class _RenderFailDriver(_HappyDriver):
    def render(self, view: str) -> object:
        return SimpleNamespace(path="renders/x.png", byte_count=0, blake2b256="")


def test_runtime_stages_pass_with_happy_stub() -> None:
    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert static.model is not None and static.candidate is not None
    reports = validate_candidate(
        static.model,
        static.candidate,
        _HappyDriver(),
        probe=_probe(),
    )
    assert len(reports) == 4
    assert all(report.passed for report in reports)
    bundle = full_bundle(static, reports)
    assert bundle.all_passed()
    requirements = PromptRequirementSet(prompt="demo village", requirements=())
    program = EnvironmentProgram(
        source=_DEMO_SOURCE,
        sdk_version=SDK_VERSION,
        prompt_fingerprint=requirements.prompt_fingerprint,
        provider=ProviderInfo(
            provider="fixture",
            model_name="demo",
            prompt_version="v1",
        ),
    )
    definition = seal_definition(
        static.candidate,
        bundle,
        requirements=requirements,
        program=program,
        model=static.model,
        navmesh_fingerprint="b" * 64,
    )
    assert definition.validation.all_passed()


def test_materialization_fails_when_load_raises() -> None:
    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert static.model is not None and static.candidate is not None
    reports = validate_candidate(
        static.model,
        static.candidate,
        _LoadFailDriver(),
        probe=_probe(),
    )
    assert reports[0].stage is HardStage.MATERIALIZATION
    assert reports[0].passed is False
    assert any(signal.code == "v.materialization_failed" for signal in reports[0].signals)


def test_navigation_fails_when_fraction_too_low() -> None:
    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert static.model is not None and static.candidate is not None
    reports = validate_candidate(
        static.model,
        static.candidate,
        _NavFailDriver(),
        probe=_probe(),
        min_walkable_fraction=0.5,
    )
    by_stage = {report.stage: report for report in reports}
    assert by_stage[HardStage.MATERIALIZATION].passed is True
    assert by_stage[HardStage.NAVIGATION].passed is False
    failure = next(
        signal
        for signal in by_stage[HardStage.NAVIGATION].signals
        if signal.code == "v6.navigation_fraction"
    )
    assert failure.measurements["connected_fraction"] == pytest.approx(0.3)


def test_controller_fails_when_not_arrived() -> None:
    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert static.model is not None and static.candidate is not None
    reports = validate_candidate(
        static.model,
        static.candidate,
        _ControllerFailDriver(),
        probe=_probe(),
    )
    by_stage = {report.stage: report for report in reports}
    assert by_stage[HardStage.CONTROLLER].passed is False
    assert any(
        signal.code == "v7.controller_not_arrived"
        for signal in by_stage[HardStage.CONTROLLER].signals
    )


def test_camera_fails_when_render_empty() -> None:
    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert static.model is not None and static.candidate is not None
    reports = validate_candidate(
        static.model,
        static.candidate,
        _RenderFailDriver(),
        probe=_probe(),
    )
    by_stage = {report.stage: report for report in reports}
    assert by_stage[HardStage.CAMERA].passed is False
    assert any(
        signal.code == "v.camera_render_invalid"
        for signal in by_stage[HardStage.CAMERA].signals
    )


def test_orthonormal_helper_rejects_skew_basis() -> None:
    # Sanity: asset stage catches non-orthonormal bases when candidate is injected.
    model = _tiny_model()
    candidate = compile_environment_model(model)
    node = candidate.scene.nodes[0]
    skewed = Transform3D(
        origin=node.transform.origin,
        basis_x=Vec3(x=1.0, y=0.0, z=0.0),
        basis_y=Vec3(x=0.5, y=1.0, z=0.0),
        basis_z=Vec3(x=0.0, y=0.0, z=1.0),
    )
    nodes = [
        (
            SceneNode(
                node_id=n.node_id,
                semantic_id=n.semantic_id,
                transform=skewed if n is node else n.transform,
                mesh=n.mesh,
                collider=n.collider,
                navmesh_contributor=n.navmesh_contributor,
                fade_group=n.fade_group,
                visual=n.visual,
            )
            if n is node
            else n
        )
        for n in candidate.scene.nodes
    ]
    broken = CandidateScene(
        scene=GodotSceneSpec(
            nodes=tuple(nodes),
            camera=candidate.scene.camera,
            controller_semantic_id=candidate.scene.controller_semantic_id,
        ),
        manifest=candidate.manifest,
    )
    static = validate_model(model, candidate=broken)
    assert static.reports[-1].stage is HardStage.ASSET
    assert static.reports[-1].passed is False
    assert any(
        signal.code == "v4.basis_not_orthonormal"
        for signal in static.reports[-1].signals
    )


def test_cold_import_order_independence() -> None:
    import subprocess
    import sys

    for module in (
        "envmaker.validation",
        "envmaker.agent.tools",
        "envmaker.agent",
        "envmaker.runlog",
    ):
        proc = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"cold import of {module} failed: {proc.stderr}"


def test_controller_trivial_route_fails() -> None:
    class _TrivialDriver(_HappyDriver):
        def navigate(self, probe: NavigationProbe) -> EpisodeResult:
            return EpisodeResult(
                probe_fingerprint=probe.probe_fingerprint,
                terminal_reason=TerminalReason.ARRIVED,
                ticks_used=1,
                final_geodesic_distance_m=0.0,
                path_length_m=0.0,
                collisions=0,
                stuck_recoveries=0,
            )

    static = validate_static(_DEMO_SOURCE, limits=_limits())
    assert static.model is not None and static.candidate is not None
    reports = validate_candidate(
        static.model,
        static.candidate,
        _TrivialDriver(),
        probe=_probe(),
    )
    controller = [r for r in reports if r.stage.value == "controller"]
    assert controller and not controller[0].passed
    assert controller[0].signals[0].code == "v7.trivial_route"


def test_runtime_driver_resolves_relative_run_dir(tmp_path, monkeypatch) -> None:
    from envmaker.runtime import RuntimeDriver

    monkeypatch.chdir(tmp_path)
    driver = RuntimeDriver(run_dir=Path("rel/runs/x"), session_id="run-x", windowed=False)
    assert driver._run_dir.is_absolute()
    assert str(driver._run_dir).startswith(str(tmp_path.resolve()))
