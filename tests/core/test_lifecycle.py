import re
from typing import Any

import pytest
from pydantic import ValidationError

from envmaker.core.artifacts import ArtifactManifest, canonical_fingerprint
from envmaker.core.definition import (
    LIFECYCLE_ORDER,
    DefinitionRequiredError,
    EnvironmentDefinition,
    HardStage,
    LifecycleStage,
    SealError,
    StageReport,
    ValidationBundle,
    require_definition,
    seal_definition,
)
from envmaker.core.model import (
    ComponentKind,
    EnvironmentModel,
    SemanticComponent,
    Transform3D,
    Vec3,
)
from envmaker.core.program import EnvironmentProgram, ProviderInfo
from envmaker.core.requirements import PromptRequirementSet
from envmaker.core.scene_spec import (
    CameraSpec,
    CandidateScene,
    GodotSceneSpec,
    SceneNode,
)
from envmaker.core.signals import Signal, SignalSeverity


def _fixture() -> dict[str, Any]:
    requirements = PromptRequirementSet(
        prompt="a snowy town with a bridge",
        requirements=(),
    )
    program = EnvironmentProgram(
        source="environment = build_environment()",
        sdk_version="0.1.0",
        prompt_fingerprint=requirements.prompt_fingerprint,
        provider=ProviderInfo(
            provider="fake",
            model_name="fixture",
            prompt_version="v1",
        ),
    )
    model = EnvironmentModel(
        name="spine-town",
        style="pixel_isometric",
        seed=7,
        sdk_version="0.1.0",
        components=(
            SemanticComponent(
                semantic_id="lower_plaza",
                kind=ComponentKind.SURFACE,
            ),
        ),
    )
    identity = Transform3D(
        origin=Vec3(x=0.0, y=0.0, z=0.0),
        basis_x=Vec3(x=1.0, y=0.0, z=0.0),
        basis_y=Vec3(x=0.0, y=1.0, z=0.0),
        basis_z=Vec3(x=0.0, y=0.0, z=1.0),
    )
    scene = GodotSceneSpec(
        nodes=(
            SceneNode(
                node_id="lower_plaza",
                semantic_id="lower_plaza",
                transform=identity,
                mesh=None,
            ),
        ),
        camera=CameraSpec(
            follow_semantic_id="lower_plaza",
            orthographic_size=12.0,
        ),
        controller_semantic_id="lower_plaza",
    )
    manifest = ArtifactManifest(root="artifacts", entries=())
    candidate = CandidateScene(scene=scene, manifest=manifest)
    validation = ValidationBundle(
        reports=tuple(
            StageReport(stage=stage, passed=True)
            for stage in HardStage
        )
    )
    return {
        "requirements": requirements,
        "program": program,
        "model": model,
        "scene": scene,
        "manifest": manifest,
        "candidate": candidate,
        "validation": validation,
        "navmesh_fingerprint": "ab" * 32,
    }


def test_stage_report_failed_requires_failure_signal() -> None:
    with pytest.raises(
        ValidationError,
        match="failed stage must carry a failure signal",
    ):
        StageReport(stage=HardStage.SCENE, passed=False)

    failure = Signal(
        code="stage.failure",
        severity=SignalSeverity.FAILURE,
        message="The hard stage failed.",
    )
    failed = StageReport(
        stage=HardStage.SCENE,
        passed=False,
        signals=(failure,),
    )
    passed = StageReport(stage=HardStage.SCENE, passed=True)

    assert failed.signals == (failure,)
    assert passed.signals == ()


def test_validation_bundle_completeness_and_uniqueness() -> None:
    stages = tuple(HardStage)
    with pytest.raises(
        ValidationError,
        match="validation bundle must cover every hard stage",
    ):
        ValidationBundle(
            reports=tuple(
                StageReport(stage=stage, passed=True)
                for stage in stages[:-1]
            )
        )

    with pytest.raises(
        ValidationError,
        match="duplicate hard stage report",
    ):
        ValidationBundle(
            reports=tuple(
                StageReport(stage=stage, passed=True)
                for stage in (*stages, HardStage.PROGRAM)
            )
        )

    validation = _fixture()["validation"]
    assert validation.all_passed() is True
    assert validation.report_for(HardStage.CAMERA).stage is HardStage.CAMERA


def test_seal_rejects_failing_stage() -> None:
    fixture = _fixture()
    failure = Signal(
        code="navigation.failure",
        severity=SignalSeverity.FAILURE,
        message="Navigation validation failed.",
    )
    reports = ValidationBundle(
        reports=tuple(
            StageReport(
                stage=stage,
                passed=stage is not HardStage.NAVIGATION,
                signals=(failure,) if stage is HardStage.NAVIGATION else (),
            )
            for stage in HardStage
        )
    )
    definition = None

    with pytest.raises(
        SealError,
        match=r"^cannot seal with failing hard stages",
    ):
        definition = seal_definition(
            fixture["candidate"],
            reports,
            requirements=fixture["requirements"],
            program=fixture["program"],
            model=fixture["model"],
            navmesh_fingerprint=fixture["navmesh_fingerprint"],
        )

    assert definition is None


def test_seal_produces_frozen_definition() -> None:
    fixture = _fixture()
    definition = seal_definition(
        fixture["candidate"],
        fixture["validation"],
        requirements=fixture["requirements"],
        program=fixture["program"],
        model=fixture["model"],
        navmesh_fingerprint=fixture["navmesh_fingerprint"],
    )
    second = seal_definition(
        fixture["candidate"],
        fixture["validation"],
        requirements=fixture["requirements"],
        program=fixture["program"],
        model=fixture["model"],
        navmesh_fingerprint=fixture["navmesh_fingerprint"],
    )
    note = Signal(
        code="semantic.note",
        severity=SignalSeverity.NOTE,
        message="Passing semantic stage observation.",
    )
    reports_with_note = ValidationBundle(
        reports=tuple(
            StageReport(
                stage=report.stage,
                passed=report.passed,
                signals=(note,)
                if report.stage is HardStage.SEMANTIC
                else report.signals,
            )
            for report in fixture["validation"].reports
        )
    )
    with_note = seal_definition(
        fixture["candidate"],
        reports_with_note,
        requirements=fixture["requirements"],
        program=fixture["program"],
        model=fixture["model"],
        navmesh_fingerprint=fixture["navmesh_fingerprint"],
    )

    assert isinstance(definition, EnvironmentDefinition)
    with pytest.raises(ValidationError, match="frozen"):
        definition.definition_fingerprint = "0" * 64
    assert re.fullmatch(r"[0-9a-f]{64}", definition.definition_fingerprint)
    assert definition.definition_fingerprint == second.definition_fingerprint
    assert definition.definition_fingerprint == with_note.definition_fingerprint


def test_definition_prompt_consistency() -> None:
    fixture = _fixture()
    other_requirements = PromptRequirementSet(
        prompt="a desert town",
        requirements=(),
    )
    mismatched_program = EnvironmentProgram(
        source="environment = build_environment()",
        sdk_version="0.1.0",
        prompt_fingerprint=other_requirements.prompt_fingerprint,
        provider=ProviderInfo(
            provider="fake",
            model_name="fixture",
            prompt_version="v1",
        ),
    )

    with pytest.raises(
        ValidationError,
        match="program prompt fingerprint must match requirements",
    ):
        EnvironmentDefinition(
            requirements=fixture["requirements"],
            program=mismatched_program,
            model=fixture["model"],
            scene=fixture["scene"],
            manifest=fixture["manifest"],
            validation=fixture["validation"],
            navmesh_fingerprint=fixture["navmesh_fingerprint"],
        )


def test_require_definition_rejects_candidate() -> None:
    fixture = _fixture()
    definition = seal_definition(
        fixture["candidate"],
        fixture["validation"],
        requirements=fixture["requirements"],
        program=fixture["program"],
        model=fixture["model"],
        navmesh_fingerprint=fixture["navmesh_fingerprint"],
    )

    with pytest.raises(
        DefinitionRequiredError,
        match="candidate scene is not an accepted definition",
    ):
        require_definition(fixture["candidate"])
    with pytest.raises(
        DefinitionRequiredError,
        match="accepted EnvironmentDefinition required",
    ):
        require_definition(42)
    assert require_definition(definition) is definition


def test_definition_fingerprint_verify() -> None:
    fixture = _fixture()
    canonical = canonical_fingerprint(
        {
            "requirements": fixture["requirements"],
            "program": fixture["program"],
            "model": fixture["model"],
            "scene": fixture["scene"],
            "manifest": fixture["manifest"],
            "navmesh_fingerprint": fixture["navmesh_fingerprint"],
        }
    )
    values = {
        "requirements": fixture["requirements"],
        "program": fixture["program"],
        "model": fixture["model"],
        "scene": fixture["scene"],
        "manifest": fixture["manifest"],
        "validation": fixture["validation"],
        "navmesh_fingerprint": fixture["navmesh_fingerprint"],
    }

    with pytest.raises(
        ValidationError,
        match="definition_fingerprint mismatch",
    ):
        EnvironmentDefinition(
            **values,
            definition_fingerprint="0" * 64,
        )

    definition = EnvironmentDefinition(
        **values,
        definition_fingerprint=canonical,
    )
    assert definition.definition_fingerprint == canonical


def test_lifecycle_order_exported() -> None:
    assert LIFECYCLE_ORDER == (
        LifecycleStage.PROGRAM,
        LifecycleStage.WORKER_EXECUTION,
        LifecycleStage.MODEL,
        LifecycleStage.CANDIDATE,
        LifecycleStage.SEALED_DEFINITION,
        LifecycleStage.INSTANCE,
    )
