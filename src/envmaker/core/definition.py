"""Terminal lifecycle contracts for validated environment definitions."""

from __future__ import annotations

from enum import StrEnum as _StrEnum

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import ArtifactManifest as _ArtifactManifest
from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.program import EnvironmentProgram as _EnvironmentProgram
from envmaker.core.requirements import PromptRequirementSet as _PromptRequirementSet
from envmaker.core.scene_spec import CandidateScene as _CandidateScene
from envmaker.core.scene_spec import GodotSceneSpec as _GodotSceneSpec
from envmaker.core.signals import Signal as _Signal
from envmaker.core.signals import SignalSeverity as _SignalSeverity

__all__ = [
    "LifecycleStage",
    "LIFECYCLE_ORDER",
    "HardStage",
    "SealError",
    "DefinitionRequiredError",
    "StageReport",
    "ValidationBundle",
    "EnvironmentDefinition",
    "seal_definition",
    "require_definition",
]


class LifecycleStage(_StrEnum):
    """A named stage in the environment lifecycle."""

    PROGRAM = "program"
    WORKER_EXECUTION = "worker_execution"
    MODEL = "model"
    CANDIDATE = "candidate"
    SEALED_DEFINITION = "sealed_definition"
    INSTANCE = "instance"


LIFECYCLE_ORDER: tuple[LifecycleStage, ...] = (
    LifecycleStage.PROGRAM,
    LifecycleStage.WORKER_EXECUTION,
    LifecycleStage.MODEL,
    LifecycleStage.CANDIDATE,
    LifecycleStage.SEALED_DEFINITION,
    LifecycleStage.INSTANCE,
)


class HardStage(_StrEnum):
    """A hard validation stage required before sealing."""

    PROGRAM = "program"
    SDK_MODEL = "sdk_model"
    ASSET = "asset"
    SCENE = "scene"
    MATERIALIZATION = "materialization"
    NAVIGATION = "navigation"
    CONTROLLER = "controller"
    CAMERA = "camera"
    SEMANTIC = "semantic"


class SealError(ValueError):
    """Raised when a candidate cannot be sealed."""


class DefinitionRequiredError(TypeError):
    """Raised when an accepted environment definition is required."""


class StageReport(_BaseModel):
    """The result and signals for one hard validation stage."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    stage: HardStage
    passed: bool
    signals: tuple[_Signal, ...] = _Field(default=(), max_length=256)

    @_model_validator(mode="after")
    def _validate_failure_signal(self) -> StageReport:
        if not self.passed and not any(
            signal.severity == _SignalSeverity.FAILURE
            for signal in self.signals
        ):
            raise ValueError("failed stage must carry a failure signal")
        return self


class ValidationBundle(_BaseModel):
    """A complete set of hard-stage validation reports."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    reports: tuple[StageReport, ...]

    @_model_validator(mode="after")
    def _validate_reports(self) -> ValidationBundle:
        stages = [report.stage for report in self.reports]
        if len(set(stages)) != len(stages):
            raise ValueError("duplicate hard stage report")
        if set(stages) != set(HardStage):
            raise ValueError("validation bundle must cover every hard stage")
        return self

    def all_passed(self) -> bool:
        """Return whether every hard stage passed."""
        return all(report.passed for report in self.reports)

    def report_for(self, stage: HardStage) -> StageReport:
        """Return the report for a hard stage."""
        return next(
            report
            for report in self.reports
            if report.stage == stage
        )


class EnvironmentDefinition(_BaseModel):
    """An immutable accepted environment definition."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    requirements: _PromptRequirementSet
    program: _EnvironmentProgram
    model: _EnvironmentModel
    scene: _GodotSceneSpec
    manifest: _ArtifactManifest
    validation: ValidationBundle
    navmesh_fingerprint: str = _Field(pattern=r"^[0-9a-f]{64}$")
    definition_fingerprint: str = ""

    def __init__(self, /, **data: object) -> None:
        validated = self.__pydantic_validator__.validate_python(
            data,
            self_instance=self,
        )
        if validated is not self:
            object.__setattr__(self, "__dict__", validated.__dict__)
            object.__setattr__(
                self,
                "__pydantic_fields_set__",
                validated.__pydantic_fields_set__,
            )
            object.__setattr__(
                self,
                "__pydantic_extra__",
                validated.__pydantic_extra__,
            )
            object.__setattr__(
                self,
                "__pydantic_private__",
                validated.__pydantic_private__,
            )

    @_model_validator(mode="after")
    def _validate_and_fingerprint(self) -> EnvironmentDefinition:
        if not self.validation.all_passed():
            raise ValueError("cannot seal with failing hard stages")
        if self.program.prompt_fingerprint != self.requirements.prompt_fingerprint:
            raise ValueError("program prompt fingerprint must match requirements")

        fingerprint = _canonical_fingerprint(
            {
                "requirements": self.requirements,
                "program": self.program,
                "model": self.model,
                "scene": self.scene,
                "manifest": self.manifest,
                "navmesh_fingerprint": self.navmesh_fingerprint,
            }
        )
        if self.definition_fingerprint == "":
            return self.model_copy(
                update={"definition_fingerprint": fingerprint}
            )
        if self.definition_fingerprint != fingerprint:
            raise ValueError("definition_fingerprint mismatch")
        return self


def seal_definition(
    candidate: _CandidateScene,
    reports: ValidationBundle,
    *,
    requirements: _PromptRequirementSet,
    program: _EnvironmentProgram,
    model: _EnvironmentModel,
    navmesh_fingerprint: str,
) -> EnvironmentDefinition:
    """Seal a candidate after checking all hard-stage reports."""
    failing = [
        report.stage.value
        for report in reports.reports
        if not report.passed
    ]
    if failing:
        raise SealError(f"cannot seal with failing hard stages: {failing}")

    return EnvironmentDefinition(
        requirements=requirements,
        program=program,
        model=model,
        scene=candidate.scene,
        manifest=candidate.manifest,
        validation=reports,
        navmesh_fingerprint=navmesh_fingerprint,
    )


def require_definition(value: object) -> EnvironmentDefinition:
    """Return an accepted definition or raise a type-level guard error."""
    if isinstance(value, EnvironmentDefinition):
        return value
    if isinstance(value, _CandidateScene):
        raise DefinitionRequiredError(
            "candidate scene is not an accepted definition"
        )
    raise DefinitionRequiredError("accepted EnvironmentDefinition required")
