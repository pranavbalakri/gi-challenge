import pytest
from pydantic import ValidationError

from envmaker.core.artifacts import canonical_fingerprint
from envmaker.core.program import (
    EnvironmentProgram,
    ProviderInfo,
    ResourceLimits,
    WorkerExecution,
    WorkerExitReason,
)
from envmaker.core.requirements import (
    PromptRequirementSet,
    Requirement,
    RequirementKind,
)
from envmaker.core.signals import Signal, SignalSeverity


def _requirement(
    *,
    req_id: str = "snow",
    source_span: tuple[int, int] = (0, 4),
) -> Requirement:
    return Requirement(
        req_id=req_id,
        kind=RequirementKind.CONTENT,
        text="snow",
        source_span=source_span,
    )


def _provider() -> ProviderInfo:
    return ProviderInfo(
        provider="fake",
        model_name="test-model",
        prompt_version="v1",
    )


def _limits() -> ResourceLimits:
    return ResourceLimits(
        cpu_seconds=5.0,
        memory_mb=512,
        output_bytes=1_000_000,
        wall_seconds=30.0,
    )


def _execution(
    exit_reason: WorkerExitReason,
    quarantined: bool,
) -> WorkerExecution:
    return WorkerExecution(
        program_fingerprint="a" * 64,
        limits=_limits(),
        exit_reason=exit_reason,
        duration_seconds=1.25,
        stdout_blake2b256="b" * 64,
        stderr_blake2b256="c" * 64,
        quarantined=quarantined,
    )


def test_signal_valid_and_frozen() -> None:
    signal = Signal(
        code="geometry.non_manifold",
        severity=SignalSeverity.WARNING,
        message="Mesh contains a non-manifold edge.",
        subject_ids=("mesh-1",),
        measurements={"edge_count": 2, "ratio": 0.5, "phase": "compile", "fatal": False},
        guidance="Repair the affected edge.",
    )

    assert signal.code == "geometry.non_manifold"
    with pytest.raises(ValidationError, match="frozen"):
        signal.message = "changed"


def test_signal_code_format() -> None:
    for code in ("BadCode", "x", ""):
        with pytest.raises(ValidationError):
            Signal(code=code, severity=SignalSeverity.FAILURE, message="invalid code")

    assert (
        Signal(
            code="geometry.non_manifold",
            severity=SignalSeverity.FAILURE,
            message="invalid geometry",
        ).code
        == "geometry.non_manifold"
    )


def test_signal_measurements_bounded_and_finite() -> None:
    with pytest.raises(ValidationError):
        Signal(
            code="probe.measurements",
            severity=SignalSeverity.WARNING,
            message="too many measurements",
            measurements={f"m{index}": index for index in range(33)},
        )

    with pytest.raises(ValidationError):
        Signal(
            code="probe.measurements",
            severity=SignalSeverity.WARNING,
            message="non-finite measurement",
            measurements={"m": float("nan")},
        )

    signal = Signal(
        code="probe.measurements",
        severity=SignalSeverity.NOTE,
        message="bounded measurements",
        measurements={f"m{index}": index for index in range(32)},
    )
    assert len(signal.measurements) == 32


def test_requirement_span_validity() -> None:
    with pytest.raises(ValidationError):
        _requirement(source_span=(-1, 4))
    with pytest.raises(ValidationError):
        _requirement(source_span=(4, 4))

    assert _requirement(source_span=(0, 4)).source_span == (0, 4)


def test_requirement_set_unique_ids() -> None:
    requirements = (
        _requirement(req_id="duplicate", source_span=(0, 4)),
        _requirement(req_id="duplicate", source_span=(5, 9)),
    )

    with pytest.raises(ValidationError):
        PromptRequirementSet(prompt="snow town", requirements=requirements)


def test_requirement_set_span_bounds() -> None:
    requirement = _requirement(source_span=(0, 5))

    with pytest.raises(ValidationError):
        PromptRequirementSet(prompt="snow", requirements=(requirement,))


def test_requirement_set_fingerprint_computed_and_verified() -> None:
    prompt = "a snowy town"
    requirement_set = PromptRequirementSet(prompt=prompt, requirements=())
    expected = canonical_fingerprint({"prompt": prompt})

    assert requirement_set.prompt_fingerprint == expected
    assert len(requirement_set.prompt_fingerprint) == 64
    assert set(requirement_set.prompt_fingerprint) <= set("0123456789abcdef")

    with pytest.raises(ValidationError, match="prompt_fingerprint mismatch"):
        PromptRequirementSet(
            prompt=prompt,
            requirements=(),
            prompt_fingerprint="0" * 64,
        )


def test_program_fingerprint_computed_and_verified() -> None:
    source = "from envmaker import sdk\n"
    sdk_version = "1.0"
    program = EnvironmentProgram(
        source=source,
        sdk_version=sdk_version,
        prompt_fingerprint="a" * 64,
        provider=_provider(),
    )
    expected = canonical_fingerprint({"source": source, "sdk_version": sdk_version})

    assert program.source_fingerprint == expected
    assert len(program.source_fingerprint) == 64
    assert set(program.source_fingerprint) <= set("0123456789abcdef")

    with pytest.raises(ValidationError, match="source_fingerprint mismatch"):
        EnvironmentProgram(
            source=source,
            sdk_version=sdk_version,
            prompt_fingerprint="a" * 64,
            provider=_provider(),
            source_fingerprint="0" * 64,
        )


def test_program_requires_nonempty_source() -> None:
    with pytest.raises(ValidationError):
        EnvironmentProgram(
            source="",
            sdk_version="1.0",
            prompt_fingerprint="a" * 64,
            provider=_provider(),
        )

    with pytest.raises(ValidationError):
        EnvironmentProgram(
            source="pass\n",
            sdk_version="",
            prompt_fingerprint="a" * 64,
            provider=_provider(),
        )


def test_worker_execution_quarantine_invariant() -> None:
    with pytest.raises(ValidationError, match="quarantine invariant violated"):
        _execution(WorkerExitReason.COMPLETED, quarantined=True)
    with pytest.raises(ValidationError, match="quarantine invariant violated"):
        _execution(WorkerExitReason.TIMEOUT, quarantined=False)

    assert _execution(WorkerExitReason.TIMEOUT, quarantined=True).quarantined is True
    assert _execution(WorkerExitReason.COMPLETED, quarantined=False).quarantined is False


def test_worker_limits_positive_finite() -> None:
    with pytest.raises(ValidationError):
        ResourceLimits(
            cpu_seconds=0.0,
            memory_mb=512,
            output_bytes=1_000_000,
            wall_seconds=30.0,
        )
    with pytest.raises(ValidationError):
        ResourceLimits(
            cpu_seconds=5.0,
            memory_mb=32,
            output_bytes=1_000_000,
            wall_seconds=30.0,
        )
    with pytest.raises(ValidationError):
        ResourceLimits(
            cpu_seconds=5.0,
            memory_mb=512,
            output_bytes=512,
            wall_seconds=30.0,
        )
    with pytest.raises(ValidationError):
        ResourceLimits(
            cpu_seconds=5.0,
            memory_mb=512,
            output_bytes=1_000_000,
            wall_seconds=float("nan"),
        )

    limits = ResourceLimits(
        cpu_seconds=5.0,
        memory_mb=512,
        output_bytes=1_000_000,
        wall_seconds=30.0,
    )
    assert limits.cpu_seconds == 5.0


def test_all_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Signal(
            code="signal.extra",
            severity=SignalSeverity.NOTE,
            message="extra field",
            unknown=True,
        )
    with pytest.raises(ValidationError):
        Requirement(
            req_id="extra",
            kind=RequirementKind.CONTENT,
            text="extra field",
            source_span=(0, 5),
            unknown=True,
        )
    with pytest.raises(ValidationError):
        EnvironmentProgram(
            source="pass\n",
            sdk_version="1.0",
            prompt_fingerprint="a" * 64,
            provider=_provider(),
            unknown=True,
        )


from envmaker.core.artifacts import ArtifactManifest, ArtifactRef
from envmaker.core.model import (
    ComponentKind,
    EnvironmentModel,
    SemanticComponent,
    Transform3D,
    Vec3,
)
from envmaker.core.scene_spec import (
    CameraSpec,
    CandidateScene,
    GodotSceneSpec,
    SceneNode,
)


def _identity_transform() -> Transform3D:
    return Transform3D(
        origin=Vec3(x=0.0, y=0.0, z=0.0),
        basis_x=Vec3(x=1.0, y=0.0, z=0.0),
        basis_y=Vec3(x=0.0, y=1.0, z=0.0),
        basis_z=Vec3(x=0.0, y=0.0, z=1.0),
    )


def _scene_node(
    *,
    node_id: str = "agent-node",
    semantic_id: str = "agent",
    mesh: ArtifactRef | None = None,
) -> SceneNode:
    return SceneNode(
        node_id=node_id,
        semantic_id=semantic_id,
        transform=_identity_transform(),
        mesh=mesh,
    )


def _artifact_ref(
    *,
    path: str = "meshes/agent.glb",
    digest: str = "a" * 64,
) -> ArtifactRef:
    return ArtifactRef(
        path=path,
        media_type="model/gltf-binary",
        byte_count=128,
        blake2b256=digest,
        sha256="c" * 64,
        producer="test",
        toolchain_version="1.0",
    )


def _scene(*, mesh: ArtifactRef | None = None) -> GodotSceneSpec:
    return GodotSceneSpec(
        nodes=(_scene_node(mesh=mesh),),
        camera=CameraSpec(follow_semantic_id="agent", orthographic_size=12.0),
        controller_semantic_id="agent",
    )


def test_vec3_finite_and_frozen() -> None:
    with pytest.raises(ValidationError):
        Vec3(x=float("nan"), y=0.0, z=0.0)
    with pytest.raises(ValidationError):
        Vec3(x=0.0, y=float("inf"), z=0.0)

    vector = Vec3(x=1.0, y=2.0, z=3.0)
    with pytest.raises(ValidationError, match="frozen"):
        vector.x = 4.0


def test_transform_finite() -> None:
    with pytest.raises(ValidationError):
        Transform3D(
            origin={"x": 0.0, "y": 0.0, "z": 0.0},
            basis_x={"x": float("inf"), "y": 0.0, "z": 0.0},
            basis_y={"x": 0.0, "y": 1.0, "z": 0.0},
            basis_z={"x": 0.0, "y": 0.0, "z": 1.0},
        )

    assert _identity_transform().basis_z.z == 1.0


def test_environment_model_unique_semantic_ids() -> None:
    components = (
        SemanticComponent(semantic_id="plaza", kind=ComponentKind.SURFACE),
        SemanticComponent(semantic_id="plaza", kind=ComponentKind.STRUCTURE),
    )

    with pytest.raises(ValidationError):
        EnvironmentModel(
            name="duplicate-town",
            style="pixel_isometric",
            seed=7,
            sdk_version="0.1.0",
            components=components,
        )


def test_environment_model_fingerprint_computed_and_verified() -> None:
    component = SemanticComponent(
        semantic_id="lower_plaza",
        kind=ComponentKind.SURFACE,
        payload={"elevation": 0.0},
    )
    values = {
        "name": "spine-town",
        "style": "pixel_isometric",
        "seed": 7,
        "sdk_version": "0.1.0",
        "components": (component,),
    }

    first = EnvironmentModel(**values)
    second = EnvironmentModel(**values)

    assert len(first.model_fingerprint) == 64
    assert set(first.model_fingerprint) <= set("0123456789abcdef")
    assert first.model_fingerprint == second.model_fingerprint

    with pytest.raises(ValidationError, match="model_fingerprint mismatch"):
        EnvironmentModel(**values, model_fingerprint="0" * 64)


def test_scene_nodes_unique_and_camera_targets_exist() -> None:
    duplicate_nodes = (
        _scene_node(node_id="shared-node", semantic_id="agent"),
        _scene_node(node_id="shared-node", semantic_id="plaza"),
    )
    camera = CameraSpec(follow_semantic_id="agent", orthographic_size=12.0)

    with pytest.raises(ValidationError):
        GodotSceneSpec(
            nodes=duplicate_nodes,
            camera=camera,
            controller_semantic_id="agent",
        )

    with pytest.raises(ValidationError, match="camera follow target not in scene"):
        GodotSceneSpec(
            nodes=(_scene_node(),),
            camera=CameraSpec(
                follow_semantic_id="missing",
                orthographic_size=12.0,
            ),
            controller_semantic_id="agent",
        )

    with pytest.raises(ValidationError, match="controller target not in scene"):
        GodotSceneSpec(
            nodes=(_scene_node(),),
            camera=camera,
            controller_semantic_id="missing",
        )


def test_candidate_scene_mesh_refs_must_be_manifested() -> None:
    mesh = _artifact_ref()
    scene = _scene(mesh=mesh)

    with pytest.raises(ValidationError, match="unmanifested mesh artifact"):
        CandidateScene(
            scene=scene,
            manifest=ArtifactManifest(root="run", entries=()),
        )

    with pytest.raises(ValidationError, match="unmanifested mesh artifact"):
        CandidateScene(
            scene=scene,
            manifest=ArtifactManifest(
                root="run",
                entries=(_artifact_ref(digest="b" * 64),),
            ),
        )

    candidate = CandidateScene(
        scene=scene,
        manifest=ArtifactManifest(root="run", entries=(mesh,)),
    )
    assert candidate.scene.nodes[0].mesh == mesh


def test_candidate_fingerprint_computed_and_verified() -> None:
    scene = _scene()
    manifest = ArtifactManifest(root="run", entries=())
    report = Signal(
        code="scene.precheck",
        severity=SignalSeverity.NOTE,
        message="pre-materialization observation",
    )

    without_reports = CandidateScene(scene=scene, manifest=manifest)
    with_reports = CandidateScene(
        scene=scene,
        manifest=manifest,
        pre_reports=(report,),
    )

    assert len(without_reports.candidate_fingerprint) == 64
    assert set(without_reports.candidate_fingerprint) <= set("0123456789abcdef")
    assert without_reports.candidate_fingerprint == with_reports.candidate_fingerprint

    with pytest.raises(ValidationError, match="candidate_fingerprint mismatch"):
        CandidateScene(
            scene=scene,
            manifest=manifest,
            candidate_fingerprint="0" * 64,
        )


def test_signal_bounded_strings_and_subjects() -> None:
    with pytest.raises(ValidationError):
        Signal(
            code="probe.measurements",
            severity=SignalSeverity.WARNING,
            message="string measurement too long",
            measurements={"detail": "x" * 2001},
        )

    with pytest.raises(ValidationError):
        Signal(
            code="probe.subjects",
            severity=SignalSeverity.WARNING,
            message="too many subjects",
            subject_ids=tuple(f"subject-{index}" for index in range(65)),
        )

    signal = Signal(
        code="probe.bounds",
        severity=SignalSeverity.NOTE,
        message="bounded values",
        subject_ids=tuple(f"subject-{index}" for index in range(64)),
        measurements={"detail": "x" * 2000},
    )
    assert len(signal.subject_ids) == 64
    assert len(signal.measurements["detail"]) == 2000


from envmaker.core.episode import (
    ConnectorType,
    EpisodeResult,
    NavigationProbe,
    TerminalReason,
)
from envmaker.core.interaction import (
    ContactPoint,
    ControllerAction,
    ObservationKind,
    ObservationPacket,
    WorldSnapshot,
)


def test_world_snapshot_bounds() -> None:
    contact = ContactPoint(
        other_semantic_id="wall",
        position=Vec3(x=1.0, y=0.0, z=2.0),
        normal=Vec3(x=0.0, y=1.0, z=0.0),
    )
    values = {
        "tick_id": 12,
        "agent_transform": _identity_transform(),
        "agent_velocity": Vec3(x=0.5, y=0.0, z=-0.25),
        "grounded": True,
    }

    with pytest.raises(ValidationError):
        WorldSnapshot(**values, contacts=(contact,) * 33)
    with pytest.raises(ValidationError):
        WorldSnapshot(**(values | {"tick_id": -1}))
    with pytest.raises(ValidationError):
        WorldSnapshot(**values, events=("Bad.Code",))

    snapshot = WorldSnapshot(
        **values,
        current_nav_region="lower_plaza",
        contacts=(contact, contact),
        visible_fade_groups=("canopy",),
        faded_groups=("upper_tower",),
        events=("navigation.entered",),
    )
    assert len(snapshot.contacts) == 2
    assert snapshot.visible_fade_groups == ("canopy",)
    assert snapshot.faded_groups == ("upper_tower",)
    assert snapshot.events == ("navigation.entered",)


def test_observation_packet_kind_frame_invariant() -> None:
    frame = ArtifactRef(
        path="frames/tick-12.png",
        media_type="image/png",
        byte_count=256,
        blake2b256="d" * 64,
        sha256="e" * 64,
        producer="test",
        toolchain_version="1.0",
    )

    with pytest.raises(
        ValidationError,
        match="frame presence must match observation kind",
    ):
        ObservationPacket(tick_id=12, kind=ObservationKind.RGB_FRAME)
    with pytest.raises(ValidationError):
        ObservationPacket(
            tick_id=12,
            kind=ObservationKind.LOCAL_SEMANTIC,
            frame=frame,
        )

    packet = ObservationPacket(
        tick_id=12,
        kind=ObservationKind.RGB_FRAME,
        frame=frame,
    )
    assert packet.frame == frame

    with pytest.raises(ValidationError):
        ObservationPacket(
            tick_id=12,
            kind=ObservationKind.CONTROLLER_STATE,
            controller_state={"speed": float("inf")},
        )


def test_controller_action_bounds() -> None:
    with pytest.raises(ValidationError):
        ControllerAction(tick_id=12, move_x=1.5, move_z=0.0)
    with pytest.raises(ValidationError):
        ControllerAction(tick_id=12, move_x=float("nan"), move_z=0.0)
    with pytest.raises(
        ValidationError,
        match="planar move magnitude exceeds 1",
    ):
        ControllerAction(tick_id=12, move_x=0.8, move_z=0.8)

    action = ControllerAction(tick_id=12, move_x=0.7071, move_z=0.7071)
    assert action.move_x == 0.7071
    assert action.move_z == 0.7071

    with pytest.raises(ValidationError):
        ControllerAction(tick_id=-1, move_x=0.0, move_z=0.0)


def test_navigation_probe_required_and_termination() -> None:
    values = {
        "target_landmark_id": "observatory",
        "success_radius_m": 1.5,
        "max_ticks": 1200,
        "action_repeat": 2,
        "allowed_connector_types": (
            ConnectorType.STAIRS,
            ConnectorType.BRIDGE,
        ),
        "stuck_timeout_ticks": 180,
    }

    with pytest.raises(ValidationError):
        NavigationProbe(**values)
    with pytest.raises(
        ValidationError,
        match="terminal_reasons must include arrived and timeout",
    ):
        NavigationProbe(**values, terminal_reasons=(TerminalReason.ARRIVED,))
    with pytest.raises(ValidationError):
        NavigationProbe(
            **values,
            terminal_reasons=(
                TerminalReason.ARRIVED,
                TerminalReason.TIMEOUT,
                TerminalReason.ARRIVED,
            ),
        )
    with pytest.raises(
        ValidationError,
        match="stuck timeout must be below max_ticks",
    ):
        NavigationProbe(
            **(values | {"stuck_timeout_ticks": 1200}),
            terminal_reasons=(
                TerminalReason.ARRIVED,
                TerminalReason.TIMEOUT,
            ),
        )

    terminal_reasons = (
        TerminalReason.ARRIVED,
        TerminalReason.TIMEOUT,
        TerminalReason.STUCK,
    )
    first = NavigationProbe(**values, terminal_reasons=terminal_reasons)
    second = NavigationProbe(**values, terminal_reasons=terminal_reasons)
    assert len(first.probe_fingerprint) == 64
    assert set(first.probe_fingerprint) <= set("0123456789abcdef")
    assert first.probe_fingerprint == second.probe_fingerprint

    with pytest.raises(ValidationError, match="probe_fingerprint mismatch"):
        NavigationProbe(
            **values,
            terminal_reasons=terminal_reasons,
            probe_fingerprint="0" * 64,
        )


def test_episode_result_valid_and_bounded() -> None:
    values = {
        "probe_fingerprint": "a" * 64,
        "terminal_reason": TerminalReason.ARRIVED,
        "ticks_used": 480,
        "final_geodesic_distance_m": 0.75,
        "path_length_m": 18.25,
        "collisions": 2,
        "stuck_recoveries": 1,
    }

    with pytest.raises(ValidationError):
        EpisodeResult(**(values | {"path_length_m": -1.0}))
    with pytest.raises(ValidationError):
        EpisodeResult(
            **(values | {"final_geodesic_distance_m": float("inf")})
        )
    with pytest.raises(ValidationError):
        EpisodeResult(**values, unknown=True)

    result = EpisodeResult(**values)
    assert result.path_length_m == 18.25
    assert result.terminal_reason is TerminalReason.ARRIVED


_PINNED_NO_VISUAL_FINGERPRINT = (
    "f127dd21441a0a84dc61e12f8e41f05797daf765dc1567afe2f5b476e0f56698"
)


def _identity_transform():
    from envmaker.core.model import Transform3D, Vec3

    return Transform3D(
        origin=Vec3(x=0.0, y=0.0, z=0.0),
        basis_x=Vec3(x=1.0, y=0.0, z=0.0),
        basis_y=Vec3(x=0.0, y=1.0, z=0.0),
        basis_z=Vec3(x=0.0, y=0.0, z=1.0),
    )


def _fixed_no_visual_candidate():
    from envmaker.core.artifacts import ArtifactManifest
    from envmaker.core.scene_spec import (
        CameraSpec,
        CandidateScene,
        ColliderShape,
        ColliderSpec,
        GodotSceneSpec,
        SceneNode,
    )

    scene = GodotSceneSpec(
        nodes=(
            SceneNode(
                node_id="ground",
                semantic_id="ground",
                transform=_identity_transform(),
                collider=ColliderSpec(
                    shape=ColliderShape.BOX,
                    dimensions={"x": 40.0, "y": 0.5, "z": 40.0},
                ),
                navmesh_contributor=True,
            ),
        ),
        camera=CameraSpec(follow_semantic_id="ground", orthographic_size=14.0),
        controller_semantic_id="ground",
    )
    manifest = ArtifactManifest(root="artifacts", entries=())
    return CandidateScene(scene=scene, manifest=manifest)


def test_primitive_visual_shapes_validate() -> None:
    from envmaker.core.scene_spec import (
        BoxVisual,
        CylinderVisual,
        PlaneVisual,
        RibbonVisual,
        SphereVisual,
    )

    box = BoxVisual(size=(1.0, 2.0, 3.0))
    assert box.shape == "box"
    assert box.material == "default"

    cylinder = CylinderVisual(radius=0.5, height=2.0, material="stone")
    assert cylinder.shape == "cylinder"
    assert cylinder.top_radius is None

    plane = PlaneVisual(size_x=40.0, size_z=40.0)
    assert plane.shape == "plane"

    sphere = SphereVisual(radius=1.5)
    assert sphere.shape == "sphere"
    assert sphere.material == "default"

    ribbon = RibbonVisual(points=((0.0, 0.0), (1.0, 0.0)), width=0.25)
    assert ribbon.shape == "ribbon"

    cone = CylinderVisual(radius=1.0, height=2.0, top_radius=0.0)
    assert cone.top_radius == 0.0

    with pytest.raises(ValidationError):
        BoxVisual(size=(0.0, 1.0, 1.0))
    with pytest.raises(ValidationError):
        BoxVisual(size=(float("inf"), 1.0, 1.0))
    with pytest.raises(ValidationError):
        CylinderVisual(radius=-1.0, height=2.0)
    with pytest.raises(ValidationError):
        CylinderVisual(radius=1.0, height=2.0, top_radius=-0.1)
    with pytest.raises(ValidationError):
        PlaneVisual(size_x=40.0, size_z=0.0)
    with pytest.raises(ValidationError):
        SphereVisual(radius=0.0)
    with pytest.raises(ValidationError):
        SphereVisual(radius=float("nan"))
    with pytest.raises(ValidationError):
        RibbonVisual(points=((0.0, 0.0),), width=0.25)
    with pytest.raises(ValidationError):
        RibbonVisual(points=((0.0, 0.0), (float("inf"), 1.0)), width=0.25)
    with pytest.raises(ValidationError):
        RibbonVisual(points=((0.0, 0.0), (1.0, 0.0)), width=0.0)
    with pytest.raises(ValidationError):
        BoxVisual(size=(1.0, 1.0, 1.0), rogue=True)


def test_scene_node_visual_discriminated_union() -> None:
    from envmaker.core.scene_spec import (
        BoxVisual,
        PlaneVisual,
        RibbonVisual,
        SceneNode,
        SphereVisual,
    )

    node = SceneNode(
        node_id="crate",
        semantic_id="crate",
        transform=_identity_transform(),
        visual={"shape": "box", "size": (1.0, 1.0, 1.0), "material": "wood"},
    )
    assert isinstance(node.visual, BoxVisual)

    ground = SceneNode(
        node_id="ground",
        semantic_id="ground",
        transform=_identity_transform(),
        visual={"shape": "plane", "size_x": 40.0, "size_z": 40.0},
    )
    assert isinstance(ground.visual, PlaneVisual)

    sphere = SceneNode.model_validate(
        {
            "node_id": "orb",
            "semantic_id": "orb",
            "transform": _identity_transform().model_dump(),
            "visual": {"shape": "sphere", "radius": 1.0},
        }
    )
    assert isinstance(sphere.visual, SphereVisual)
    assert sphere.visual.radius == 1.0

    ribbon = SceneNode.model_validate(
        {
            "node_id": "path",
            "semantic_id": "path",
            "transform": _identity_transform().model_dump(),
            "visual": {
                "shape": "ribbon",
                "points": [[0.0, 0.0], [1.0, 1.0]],
                "width": 0.5,
            },
        }
    )
    assert isinstance(ribbon.visual, RibbonVisual)
    assert ribbon.visual.points == ((0.0, 0.0), (1.0, 1.0))

    with pytest.raises(ValidationError):
        SceneNode(
            node_id="bad",
            semantic_id="bad",
            transform=_identity_transform(),
            visual={"shape": "pyramid", "radius": 1.0},
        )

    plain = SceneNode(
        node_id="plain",
        semantic_id="plain",
        transform=_identity_transform(),
    )
    assert plain.visual is None


def test_cylinder_top_radius_fingerprint_stable() -> None:
    from envmaker.core.artifacts import (
        ArtifactManifest,
        _canonical_structure,
        canonical_fingerprint,
    )
    from envmaker.core.scene_spec import (
        BoxVisual,
        CameraSpec,
        CandidateScene,
        CylinderVisual,
        GodotSceneSpec,
        PlaneVisual,
        SceneNode,
    )

    cylinder = CylinderVisual(radius=1.0, height=2.0)
    structure = _canonical_structure(cylinder)
    assert "top_radius" not in structure
    assert structure == {
        "shape": "cylinder",
        "radius": 1.0,
        "height": 2.0,
        "material": "default",
    }

    with_top = CylinderVisual(radius=1.0, height=2.0, top_radius=0.0)
    assert "top_radius" in _canonical_structure(with_top)
    assert canonical_fingerprint(cylinder) != canonical_fingerprint(with_top)

    scene = GodotSceneSpec(
        nodes=(
            SceneNode(
                node_id="ground",
                semantic_id="ground",
                transform=_identity_transform(),
                visual=PlaneVisual(size_x=40.0, size_z=40.0, material="grass"),
            ),
            SceneNode(
                node_id="crate",
                semantic_id="crate",
                transform=_identity_transform(),
                visual=BoxVisual(size=(1.0, 1.0, 1.0), material="wood"),
            ),
            SceneNode(
                node_id="marker",
                semantic_id="marker",
                transform=_identity_transform(),
                visual=CylinderVisual(radius=0.3, height=1.5),
            ),
        ),
        camera=CameraSpec(follow_semantic_id="ground", orthographic_size=14.0),
        controller_semantic_id="ground",
    )
    candidate = CandidateScene(
        scene=scene,
        manifest=ArtifactManifest(root="artifacts", entries=()),
    )
    scene_structure = _canonical_structure(scene)
    marker_visual = scene_structure["nodes"][2]["visual"]
    assert "top_radius" not in marker_visual
    assert candidate.candidate_fingerprint == canonical_fingerprint(
        {"scene": scene, "manifest": ArtifactManifest(root="artifacts", entries=())}
    )


def test_candidate_fingerprint_pinned_no_visual() -> None:
    candidate = _fixed_no_visual_candidate()
    assert candidate.candidate_fingerprint == _PINNED_NO_VISUAL_FINGERPRINT


def test_collider_shape_cylinder_additive() -> None:
    from envmaker.core.scene_spec import ColliderShape, ColliderSpec

    spec = ColliderSpec(
        shape=ColliderShape.CYLINDER,
        dimensions={"radius": 0.5, "height": 2.0},
    )
    assert spec.shape.value == "cylinder"
    assert {member.value for member in ColliderShape} == {
        "box",
        "capsule",
        "convex",
        "trimesh",
        "cylinder",
    }
