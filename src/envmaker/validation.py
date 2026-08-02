"""Nine-stage hard validators for EnvMaker programs, models, and candidates."""

from __future__ import annotations

import math as _math
from dataclasses import dataclass as _dataclass
from typing import Protocol as _Protocol
from typing import runtime_checkable as _runtime_checkable

from envmaker.agent.worker import run_generated_program as _run_generated_program
from envmaker.core.definition import HardStage as _HardStage
from envmaker.core.definition import StageReport as _StageReport
from envmaker.core.definition import ValidationBundle as _ValidationBundle
from envmaker.core.episode import EpisodeResult as _EpisodeResult
from envmaker.core.episode import NavigationProbe as _NavigationProbe
from envmaker.core.episode import TerminalReason as _TerminalReason
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.model import Transform3D as _Transform3D
from envmaker.core.model import Vec3 as _Vec3
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.program import WorkerExitReason as _WorkerExitReason
from envmaker.core.scene_spec import CandidateScene as _CandidateScene
from envmaker.core.scene_spec import ColliderShape as _ColliderShape
from envmaker.core.scene_spec import PlaneVisual as _PlaneVisual
from envmaker.core.scene_spec import SceneNode as _SceneNode
from envmaker.core.signals import Signal as _Signal
from envmaker.core.signals import SignalSeverity as _SignalSeverity
from envmaker.sdk import SDK_VERSION as _SDK_VERSION
from envmaker.sdk import compile_environment_model as _compile_environment_model
from envmaker.sdk.kits import KITS as _KITS

__all__ = [
    "StaticValidation",
    "RuntimeDriverLike",
    "validate_static",
    "validate_model",
    "validate_candidate",
    "full_bundle",
]

_KNOWN_COMPONENTS = frozenset(
    {
        "ground",
        "path",
        "water",
        "wall",
        "obstacle",
        "structure",
        "landmark",
        "scatter",
        "spawn",
        "camera",
    }
)
_KIT_COMPONENTS = frozenset({"structure", "landmark", "scatter"})
_AGENT_RADIUS = 0.4
_ORTHONORMAL_TOL = 1e-6
_MAX_COORD = 10000.0
_PIPELINE_ORDER: tuple[_HardStage, ...] = (
    _HardStage.PROGRAM,
    _HardStage.SDK_MODEL,
    _HardStage.SEMANTIC,
    _HardStage.ASSET,
    _HardStage.SCENE,
    _HardStage.MATERIALIZATION,
    _HardStage.NAVIGATION,
    _HardStage.CONTROLLER,
    _HardStage.CAMERA,
)


@_dataclass(frozen=True, slots=True)
class StaticValidation:
    """Outcome of pure-Python hard stages (program through scene)."""

    model: _EnvironmentModel | None
    candidate: _CandidateScene | None
    reports: tuple[_StageReport, ...]


@_runtime_checkable
class RuntimeDriverLike(_Protocol):
    """Duck-typed runtime seam used by live-runtime validators.

    Real ``RuntimeDriver`` methods used: ``load_candidate``, ``wait_navigation_ready``,
    ``navigate``, ``render``. Stubs (and a future driver shim) also expose
    ``connected_clear_ground_fraction`` so connectivity math stays in validation.py.
    """

    def load_candidate(self, candidate: _CandidateScene) -> object: ...

    def wait_navigation_ready(self, timeout: float = 30.0) -> None: ...

    def connected_clear_ground_fraction(
        self, *args: object, **kwargs: object
    ) -> float: ...

    def navigate(self, probe: _NavigationProbe) -> _EpisodeResult: ...

    def render(self, view: str) -> object: ...


def _clamp_measurement_value(
    value: float | int | str | bool,
) -> float | int | str | bool:
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, float) and not _math.isfinite(value):
        return str(value)[:2000]
    return value


def _failure(
    code: str,
    message: str,
    *,
    subject_ids: tuple[str, ...] = (),
    measurements: dict[str, float | int | str | bool] | None = None,
    guidance: str = "",
) -> _Signal:
    clamped_measurements: dict[str, float | int | str | bool] = {}
    for key, value in (measurements or {}).items():
        clamped_key = key[:64] if key else "measurement"
        clamped_measurements[clamped_key] = _clamp_measurement_value(value)
    return _Signal(
        code=code,
        severity=_SignalSeverity.FAILURE,
        message=message[:2000],
        subject_ids=subject_ids,
        measurements=clamped_measurements,
        guidance=guidance[:2000],
    )


def _report(
    stage: _HardStage,
    *,
    passed: bool,
    signals: tuple[_Signal, ...] = (),
) -> _StageReport:
    return _StageReport(stage=stage, passed=passed, signals=signals)


def _vec_components(vector: _Vec3) -> tuple[float, float, float]:
    return (float(vector.x), float(vector.y), float(vector.z))


def _dot(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _norm(vector: tuple[float, float, float]) -> float:
    return _math.sqrt(_dot(vector, vector))


def _is_orthonormal(transform: _Transform3D) -> bool:
    basis = (
        _vec_components(transform.basis_x),
        _vec_components(transform.basis_y),
        _vec_components(transform.basis_z),
    )
    for axis in basis:
        if abs(_norm(axis) - 1.0) > _ORTHONORMAL_TOL:
            return False
    pairs = ((0, 1), (0, 2), (1, 2))
    for i, j in pairs:
        if abs(_dot(basis[i], basis[j])) > _ORTHONORMAL_TOL:
            return False
    return True


def _finite_components(values: tuple[float, ...]) -> bool:
    return all(_math.isfinite(value) for value in values)


def _check_program(
    source: str, *, limits: _ResourceLimits
) -> tuple[_StageReport, _EnvironmentModel | None]:
    execution, model, stderr_tail = _run_generated_program(source, limits=limits)
    if (
        execution.exit_reason is _WorkerExitReason.COMPLETED
        and model is not None
    ):
        return _report(_HardStage.PROGRAM, passed=True), model
    message = "generated program did not complete with a model"
    if stderr_tail:
        message = f"{message}: {stderr_tail}"
    measurements: dict[str, float | int | str | bool] = {
        "exit_reason": execution.exit_reason.value,
        "stderr_blake2b256": execution.stderr_blake2b256,
    }
    if stderr_tail:
        measurements["stderr_tail"] = stderr_tail
    return (
        _report(
            _HardStage.PROGRAM,
            passed=False,
            signals=(
                _failure(
                    "v1.program_failed",
                    message,
                    measurements=measurements,
                    guidance="fix the program so the worker completes and returns an EnvironmentModel",
                ),
            ),
        ),
        None,
    )


def _check_sdk_model(model: _EnvironmentModel) -> _StageReport:
    signals: list[_Signal] = []
    try:
        revalidated = _EnvironmentModel.model_validate(model.model_dump())
    except Exception as exc:
        return _report(
            _HardStage.SDK_MODEL,
            passed=False,
            signals=(
                _failure(
                    "v2.model_invalid",
                    f"environment model failed revalidation: {exc}",
                    guidance="rebuild the model through EnvironmentBuilder.freeze()",
                ),
            ),
        )

    if revalidated.sdk_version != _SDK_VERSION:
        signals.append(
            _failure(
                "v2.sdk_version_mismatch",
                "model sdk_version does not match the installed SDK",
                measurements={
                    "expected": _SDK_VERSION,
                    "actual": revalidated.sdk_version,
                },
                guidance="freeze the model with the current EnvironmentBuilder SDK_VERSION",
            )
        )
    if not revalidated.name or not revalidated.style:
        signals.append(
            _failure(
                "v2.model_metadata_invalid",
                "model name or style is invalid",
                guidance="set a valid name and non-empty style before freezing",
            )
        )
    if revalidated.seed < 0:
        signals.append(
            _failure(
                "v2.model_seed_invalid",
                "model seed must be a non-negative integer",
                guidance="pass a non-negative seed to EnvironmentBuilder",
            )
        )
    if revalidated.model_fingerprint != model.model_fingerprint:
        signals.append(
            _failure(
                "v2.fingerprint_mismatch",
                "model fingerprint does not verify on round-trip",
                guidance="do not hand-edit model_fingerprint; rebuild via freeze()",
            )
        )
    if signals:
        return _report(_HardStage.SDK_MODEL, passed=False, signals=tuple(signals))
    return _report(_HardStage.SDK_MODEL, passed=True)


def _check_semantic(model: _EnvironmentModel) -> _StageReport:
    """Validate the component graph (counts, ids, kits, scatter region).

    This hard stage checks structural semantic validity of the model — not
    prompt-faithfulness or user-intent compliance.
    """

    signals: list[_Signal] = []
    grounds: list[str] = []
    spawns: list[str] = []
    cameras: list[str] = []
    seen_ids: list[str] = []
    ground_ids = {
        component.semantic_id
        for component in model.components
        if component.payload.get("component") == "ground"
    }

    for component in model.components:
        seen_ids.append(component.semantic_id)
        discriminator = component.payload.get("component")
        if not isinstance(discriminator, str) or discriminator not in _KNOWN_COMPONENTS:
            signals.append(
                _failure(
                    "v3.unknown_component",
                    "component payload discriminator is unknown",
                    subject_ids=(component.semantic_id,),
                    measurements={
                        "discriminator": str(discriminator),
                    },
                    guidance="use only SDK builder component types",
                )
            )
            continue
        if discriminator == "ground":
            grounds.append(component.semantic_id)
        elif discriminator == "spawn":
            spawns.append(component.semantic_id)
        elif discriminator == "camera":
            cameras.append(component.semantic_id)
        elif discriminator == "scatter":
            region = component.payload.get("region")
            if not isinstance(region, str) or region not in ground_ids:
                signals.append(
                    _failure(
                        "v3.scatter_region",
                        "scatter region does not reference the declared ground",
                        subject_ids=(component.semantic_id,),
                        measurements={"region": str(region)},
                        guidance="set scatter region to the ground semantic id",
                    )
                )
        if discriminator in _KIT_COMPONENTS:
            kit_name = component.payload.get("kit")
            if not isinstance(kit_name, str) or kit_name not in _KITS:
                signals.append(
                    _failure(
                        "v3.unknown_kit",
                        "component references an unknown kit",
                        subject_ids=(component.semantic_id,),
                        measurements={"kit": str(kit_name)},
                        guidance="choose a kit name from envmaker.sdk.KITS",
                    )
                )

    if len(set(seen_ids)) != len(seen_ids):
        duplicates = sorted(
            {
                semantic_id
                for semantic_id in seen_ids
                if seen_ids.count(semantic_id) > 1
            }
        )
        signals.append(
            _failure(
                "v3.duplicate_semantic_id",
                "semantic ids must be unique",
                subject_ids=tuple(duplicates),
                guidance="give every component a distinct semantic id",
            )
        )
    if len(grounds) != 1:
        signals.append(
            _failure(
                "v3.ground_count",
                "environment must declare exactly one ground",
                subject_ids=tuple(grounds),
                measurements={"count": len(grounds)},
                guidance="declare exactly one ground component",
            )
        )
    if len(spawns) != 1:
        signals.append(
            _failure(
                "v3.spawn_count",
                "environment must declare exactly one spawn",
                subject_ids=tuple(spawns),
                measurements={"count": len(spawns)},
                guidance="declare exactly one spawn component",
            )
        )
    if len(cameras) != 1:
        signals.append(
            _failure(
                "v3.camera_count",
                "environment must declare exactly one camera",
                subject_ids=tuple(cameras),
                measurements={"count": len(cameras)},
                guidance="declare exactly one camera component",
            )
        )

    if signals:
        return _report(_HardStage.SEMANTIC, passed=False, signals=tuple(signals[:256]))
    return _report(_HardStage.SEMANTIC, passed=True)


def _check_asset(candidate: _CandidateScene) -> _StageReport:
    signals: list[_Signal] = []
    for node in candidate.scene.nodes:
        origin = _vec_components(node.transform.origin)
        basis = (
            *_vec_components(node.transform.basis_x),
            *_vec_components(node.transform.basis_y),
            *_vec_components(node.transform.basis_z),
        )
        if not _finite_components(origin + basis):
            signals.append(
                _failure(
                    "v4.non_finite_transform",
                    "node transform contains a non-finite component",
                    subject_ids=(node.semantic_id,),
                    guidance="ensure every compiled transform uses finite coordinates",
                )
            )
            continue
        if any(abs(value) > _MAX_COORD for value in origin):
            signals.append(
                _failure(
                    "v4.coordinate_bounds",
                    "node origin exceeds world coordinate bounds",
                    subject_ids=(node.semantic_id,),
                    measurements={
                        "origin_x": origin[0],
                        "origin_y": origin[1],
                        "origin_z": origin[2],
                    },
                    guidance="keep authored geometry within +/- 10000 metres",
                )
            )
        if not _is_orthonormal(node.transform):
            signals.append(
                _failure(
                    "v4.basis_not_orthonormal",
                    "node basis is not orthonormal",
                    subject_ids=(node.semantic_id,),
                    guidance="use yaw-only transforms with unit orthogonal axes",
                )
            )
        if node.collider is not None:
            for key, value in node.collider.dimensions.items():
                if (
                    not _math.isfinite(value)
                    or value <= 0.0
                    or abs(value) > _MAX_COORD
                ):
                    signals.append(
                        _failure(
                            "v4.invalid_dimensions",
                            "collider dimensions must be finite, positive, and in bounds",
                            subject_ids=(node.semantic_id,),
                            measurements={"dimension": key, "value": float(value)},
                            guidance="rebuild geometry with positive finite dimensions within +/- 10000",
                        )
                    )
        if node.visual is not None:
            visual = node.visual
            values: list[float] = []
            if isinstance(visual, _PlaneVisual):
                values = [visual.size_x, visual.size_z]
            elif hasattr(visual, "size"):
                values = list(visual.size)
            else:
                values = [float(visual.radius), float(visual.height)]
            if any(
                not _math.isfinite(value) or value <= 0.0 or abs(value) > _MAX_COORD
                for value in values
            ):
                signals.append(
                    _failure(
                        "v4.invalid_dimensions",
                        "visual dimensions must be finite, positive, and in bounds",
                        subject_ids=(node.semantic_id,),
                        guidance="rebuild geometry with positive finite dimensions within +/- 10000",
                    )
                )

    if signals:
        return _report(_HardStage.ASSET, passed=False, signals=tuple(signals[:256]))
    return _report(_HardStage.ASSET, passed=True)


def _yaw_from_basis(transform: _Transform3D) -> float:
    # basis_x = (cos, 0, -sin) under compile yaw convention
    return _math.atan2(-transform.basis_x.z, transform.basis_x.x)


def _local_xz(
    world_x: float,
    world_z: float,
    origin_x: float,
    origin_z: float,
    yaw: float,
) -> tuple[float, float]:
    dx = world_x - origin_x
    dz = world_z - origin_z
    cos_yaw = _math.cos(-yaw)
    sin_yaw = _math.sin(-yaw)
    return (cos_yaw * dx + sin_yaw * dz, -sin_yaw * dx + cos_yaw * dz)


def _blocker_half_extents(node: _SceneNode) -> tuple[float, float] | None:
    collider = node.collider
    if collider is None:
        return None
    if collider.shape is _ColliderShape.CYLINDER:
        radius = float(collider.dimensions["radius"])
        return (radius, radius)
    if "x" in collider.dimensions and "z" in collider.dimensions:
        return (
            float(collider.dimensions["x"]) / 2.0,
            float(collider.dimensions["z"]) / 2.0,
        )
    return None


def _is_ground_node(node: _SceneNode) -> bool:
    return isinstance(node.visual, _PlaneVisual)


def _is_blocker_node(node: _SceneNode) -> bool:
    return node.collider is not None and not _is_ground_node(node)


def _spawn_clear_of_blocker(
    spawn_x: float,
    spawn_z: float,
    node: _SceneNode,
    *,
    clearance: float,
) -> bool:
    half = _blocker_half_extents(node)
    if half is None:
        return True
    half_x, half_z = half
    yaw = _yaw_from_basis(node.transform)
    local_x, local_z = _local_xz(
        spawn_x,
        spawn_z,
        node.transform.origin.x,
        node.transform.origin.z,
        yaw,
    )
    return (
        abs(local_x) > half_x + clearance
        or abs(local_z) > half_z + clearance
    )


def _point_in_ground(
    spawn_x: float,
    spawn_z: float,
    ground: _SceneNode,
) -> bool:
    assert isinstance(ground.visual, _PlaneVisual)
    half_x = ground.visual.size_x / 2.0 - _AGENT_RADIUS
    half_z = ground.visual.size_z / 2.0 - _AGENT_RADIUS
    if half_x < 0.0 or half_z < 0.0:
        return False
    dx = abs(spawn_x - ground.transform.origin.x)
    dz = abs(spawn_z - ground.transform.origin.z)
    return dx <= half_x and dz <= half_z


def _check_scene(candidate: _CandidateScene) -> _StageReport:
    signals: list[_Signal] = []
    spawn_id = candidate.scene.controller_semantic_id
    spawn_nodes = [
        node for node in candidate.scene.nodes if node.semantic_id == spawn_id
    ]
    ground_nodes = [
        node for node in candidate.scene.nodes if _is_ground_node(node)
    ]
    if not spawn_nodes:
        return _report(
            _HardStage.SCENE,
            passed=False,
            signals=(
                _failure(
                    "v5.spawn_missing",
                    "candidate has no spawn node",
                    guidance="compile a model that declares a spawn",
                ),
            ),
        )
    if not ground_nodes:
        return _report(
            _HardStage.SCENE,
            passed=False,
            signals=(
                _failure(
                    "v5.ground_missing",
                    "candidate has no ground plane node",
                    guidance="compile a model that declares a ground",
                ),
            ),
        )

    spawn = spawn_nodes[0]
    ground = ground_nodes[0]
    spawn_x = spawn.transform.origin.x
    spawn_z = spawn.transform.origin.z

    if not _point_in_ground(spawn_x, spawn_z, ground):
        signals.append(
            _failure(
                "v5.spawn_outside_ground",
                "spawn is outside the ground plane bounds",
                subject_ids=(spawn.semantic_id, ground.semantic_id),
                measurements={"spawn_x": spawn_x, "spawn_z": spawn_z},
                guidance="move the spawn onto the ground footprint with clearance",
            )
        )

    for node in candidate.scene.nodes:
        if not _is_blocker_node(node):
            continue
        if not _spawn_clear_of_blocker(
            spawn_x, spawn_z, node, clearance=_AGENT_RADIUS
        ):
            signals.append(
                _failure(
                    "v5.spawn_intersects_blocker",
                    "spawn intersects a blocker collider footprint",
                    subject_ids=(spawn.semantic_id, node.semantic_id),
                    measurements={
                        "spawn_x": spawn_x,
                        "spawn_z": spawn_z,
                        "clearance": _AGENT_RADIUS,
                    },
                    guidance="move the spawn away from blocking geometry",
                )
            )

    if signals:
        return _report(_HardStage.SCENE, passed=False, signals=tuple(signals[:256]))
    return _report(_HardStage.SCENE, passed=True)


def _stages_from_model(
    model: _EnvironmentModel,
    *,
    candidate: _CandidateScene | None,
) -> StaticValidation:
    reports: list[_StageReport] = []

    sdk_report = _check_sdk_model(model)
    reports.append(sdk_report)
    if not sdk_report.passed:
        return StaticValidation(model=None, candidate=None, reports=tuple(reports))

    semantic_report = _check_semantic(model)
    reports.append(semantic_report)
    if not semantic_report.passed:
        return StaticValidation(model=model, candidate=None, reports=tuple(reports))

    if candidate is None:
        try:
            candidate = _compile_environment_model(model)
        except Exception as exc:
            reports.append(
                _report(
                    _HardStage.ASSET,
                    passed=False,
                    signals=(
                        _failure(
                            "v4.compile_failed",
                            f"model failed to compile into a candidate: {exc}",
                            guidance="repair semantic content so the SDK compiler accepts it",
                        ),
                    ),
                )
            )
            return StaticValidation(
                model=model, candidate=None, reports=tuple(reports)
            )

    asset_report = _check_asset(candidate)
    reports.append(asset_report)
    if not asset_report.passed:
        return StaticValidation(
            model=model, candidate=candidate, reports=tuple(reports)
        )

    scene_report = _check_scene(candidate)
    reports.append(scene_report)
    if not scene_report.passed:
        return StaticValidation(
            model=model, candidate=candidate, reports=tuple(reports)
        )

    return StaticValidation(
        model=model, candidate=candidate, reports=tuple(reports)
    )


def validate_model(
    model: _EnvironmentModel,
    *,
    candidate: _CandidateScene | None = None,
) -> StaticValidation:
    """Run hard stages sdk_model through scene for an already-produced model."""

    return _stages_from_model(model, candidate=candidate)


def validate_static(source: str, *, limits: _ResourceLimits) -> StaticValidation:
    """Run hard stages program through scene for one generated program source."""

    program_report, model = _check_program(source, limits=limits)
    if not program_report.passed or model is None:
        return StaticValidation(
            model=None,
            candidate=None,
            reports=(program_report,),
        )

    downstream = _stages_from_model(model, candidate=None)
    return StaticValidation(
        model=downstream.model,
        candidate=downstream.candidate,
        reports=(program_report, *downstream.reports),
    )


def _ground_bounds(
    candidate: _CandidateScene,
) -> tuple[float, float, float, float] | None:
    for node in candidate.scene.nodes:
        if not isinstance(node.visual, _PlaneVisual):
            continue
        half_x = node.visual.size_x / 2.0
        half_z = node.visual.size_z / 2.0
        cx = node.transform.origin.x
        cz = node.transform.origin.z
        return (cx - half_x, cz - half_z, cx + half_x, cz + half_z)
    return None


def _spawn_point(candidate: _CandidateScene) -> tuple[float, float] | None:
    spawn_id = candidate.scene.controller_semantic_id
    for node in candidate.scene.nodes:
        if node.semantic_id == spawn_id:
            return (node.transform.origin.x, node.transform.origin.z)
    return None


def _connected_clear_ground_fraction(
    driver: object,
    *,
    bounds: tuple[float, float, float, float],
    from_point: tuple[float, float] | None,
) -> float:
    method = getattr(driver, "connected_clear_ground_fraction", None)
    if method is None:
        raise RuntimeError("driver lacks connected_clear_ground_fraction")
    # Prefer a denser lattice than the driver default (12): narrow gate corridors
    # (~2.4 m) are otherwise invisible to grid flood-fill path queries.
    try:
        return float(method(bounds=bounds, from_point=from_point, grid=40))
    except TypeError:
        try:
            return float(method(bounds=bounds, from_point=from_point))
        except TypeError:
            return float(method())


def _response_ok(response: object) -> bool:
    if response is None:
        return True
    ok = getattr(response, "ok", None)
    if ok is None:
        return True
    return bool(ok)


def _artifact_verified(artifact: object) -> bool:
    byte_count = getattr(artifact, "byte_count", 0)
    digest = getattr(artifact, "blake2b256", "")
    path = getattr(artifact, "path", "")
    return (
        isinstance(byte_count, int)
        and byte_count > 0
        and isinstance(digest, str)
        and len(digest) == 64
        and isinstance(path, str)
        and bool(path)
    )


def validate_candidate(
    model: _EnvironmentModel,
    candidate: _CandidateScene,
    driver: object,
    *,
    probe: _NavigationProbe,
    min_walkable_fraction: float = 0.5,
) -> list[_StageReport]:
    """Run hard stages materialization through camera via a duck-typed driver."""

    del model  # model is part of the public seam for the second pipeline half
    reports: list[_StageReport] = []

    try:
        response = driver.load_candidate(candidate)
        if not _response_ok(response):
            raise RuntimeError("load_candidate returned a non-ok response")
        reports.append(_report(_HardStage.MATERIALIZATION, passed=True))
    except Exception as exc:
        reports.append(
            _report(
                _HardStage.MATERIALIZATION,
                passed=False,
                signals=(
                    _failure(
                        "v.materialization_failed",
                        f"candidate materialization failed: {exc}",
                        guidance="ensure the candidate compiles and the runtime can load it",
                    ),
                ),
            )
        )
        return reports

    try:
        driver.wait_navigation_ready()
        bounds = _ground_bounds(candidate)
        if bounds is None:
            raise RuntimeError("candidate has no ground plane for navigation fraction")
        fraction = _connected_clear_ground_fraction(
            driver,
            bounds=bounds,
            from_point=_spawn_point(candidate),
        )
        if not _math.isfinite(fraction):
            raise RuntimeError("connected clear-ground fraction is not finite")
        if fraction < min_walkable_fraction:
            reports.append(
                _report(
                    _HardStage.NAVIGATION,
                    passed=False,
                    signals=(
                        _failure(
                            "v6.clear_ground_fraction",
                            "connected clear-ground fraction is below the threshold",
                            measurements={
                                "clear_ground_fraction": fraction,
                                "min_walkable_fraction": min_walkable_fraction,
                            },
                            guidance=(
                                "open corridors so most clear-ground cells "
                                "(grid samples inside the ground footprint, "
                                "excluding blocker footprints) are reachable "
                                "from spawn; Godot traversal remains the "
                                "authoritative witness"
                            ),
                        ),
                    ),
                )
            )
            return reports
        reports.append(_report(_HardStage.NAVIGATION, passed=True))
    except Exception as exc:
        reports.append(
            _report(
                _HardStage.NAVIGATION,
                passed=False,
                signals=(
                    _failure(
                        "v6.navigation_not_ready",
                        f"navigation readiness check failed: {exc}",
                        guidance="repair geometry so navigation bake succeeds",
                    ),
                ),
            )
        )
        return reports

    try:
        episode = driver.navigate(probe)
        episode_measurements = {
            "terminal_reason": episode.terminal_reason.value,
            "ticks_used": episode.ticks_used,
            "path_length_m": episode.path_length_m,
        }
        if episode.terminal_reason is not _TerminalReason.ARRIVED:
            reports.append(
                _report(
                    _HardStage.CONTROLLER,
                    passed=False,
                    signals=(
                        _failure(
                            "v7.controller_not_arrived",
                            "navigation episode did not arrive at the probe target",
                            measurements=episode_measurements,
                            guidance="ensure a clear route exists from spawn to the probe target",
                        ),
                    ),
                )
            )
            return reports
        if episode.path_length_m < 1.0:
            reports.append(
                _report(
                    _HardStage.CONTROLLER,
                    passed=False,
                    signals=(
                        _failure(
                            "v7.trivial_route",
                            "navigation episode was trivial: the probe target is "
                            "not meaningfully distinct from the spawn",
                            measurements=episode_measurements,
                            guidance=(
                                "declare a landmark (or another distinct goal) "
                                "away from the spawn so a real route is traversed"
                            ),
                        ),
                    ),
                )
            )
            return reports
        reports.append(
            _report(
                _HardStage.CONTROLLER,
                passed=True,
                signals=(
                    _Signal(
                        code="v7.episode_summary",
                        severity=_SignalSeverity.NOTE,
                        message="navigation episode arrived at the probe target",
                        measurements=episode_measurements,
                    ),
                ),
            )
        )
    except Exception as exc:
        reports.append(
            _report(
                _HardStage.CONTROLLER,
                passed=False,
                signals=(
                    _failure(
                        "v7.controller_not_arrived",
                        f"navigation episode failed: {exc}",
                        guidance="ensure a clear route exists from spawn to the probe target",
                    ),
                ),
            )
        )
        return reports

    try:
        artifact = driver.render("isometric")
        if not _artifact_verified(artifact):
            reports.append(
                _report(
                    _HardStage.CAMERA,
                    passed=False,
                    signals=(
                        _failure(
                            "v.camera_render_invalid",
                            "render capture did not return a verified artifact ref",
                            guidance="capture a non-empty render with path, hash, and byte size",
                        ),
                    ),
                )
            )
            return reports
        reports.append(_report(_HardStage.CAMERA, passed=True))
    except Exception as exc:
        reports.append(
            _report(
                _HardStage.CAMERA,
                passed=False,
                signals=(
                    _failure(
                        "v.camera_render_invalid",
                        f"render capture failed: {exc}",
                        guidance="capture a non-empty render with path, hash, and byte size",
                    ),
                ),
            )
        )
    return reports


def full_bundle(
    static: StaticValidation,
    runtime_reports: list[_StageReport],
) -> _ValidationBundle:
    """Assemble the frozen nine-stage bundle in pipeline order."""

    by_stage = {report.stage: report for report in static.reports}
    for report in runtime_reports:
        by_stage[report.stage] = report

    ordered: list[_StageReport] = []
    for stage in _PIPELINE_ORDER:
        report = by_stage.get(stage)
        if report is None:
            ordered.append(
                _report(
                    stage,
                    passed=False,
                    signals=(
                        _failure(
                            "v.stage_not_run",
                            "hard stage was not executed because an earlier stage failed",
                            guidance="repair earlier failing stages before re-running validation",
                        ),
                    ),
                )
            )
        else:
            ordered.append(report)
    return _ValidationBundle(reports=tuple(ordered))
