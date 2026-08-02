"""Bounded authoring tool surface for the EnvMaker agent harness."""

from __future__ import annotations

import base64 as _base64
import copy as _copy
import io as _io
import math as _math
import re as _re
from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from pathlib import Path as _Path
from typing import Literal as _Literal

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field

from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.definition import HardStage as _HardStage
from envmaker.core.definition import StageReport as _StageReport
from envmaker.core.episode import NavigationProbe as _NavigationProbe
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.scene_spec import CandidateScene as _CandidateScene
from envmaker.core.scene_spec import ColliderShape as _ColliderShape
from envmaker.core.scene_spec import PlaneVisual as _PlaneVisual
from envmaker.core.scene_spec import SceneNode as _SceneNode
from envmaker.core.signals import Signal as _Signal
from envmaker.core.signals import SignalSeverity as _SignalSeverity
from envmaker.runlog import RunLog as _RunLog
from envmaker.runlog import _redact as _redact_value
from envmaker.sdk import SDK_VERSION as _SDK_VERSION
from envmaker.sdk.kits import get_kit as _get_kit
from envmaker.validation import StaticValidation as _StaticValidation
from envmaker.validation import validate_candidate as _validate_candidate
from envmaker.validation import validate_static as _validate_static

__all__ = [
    "PatchResult",
    "CompileResult",
    "ProbeResult",
    "RenderResult",
    "AuditResult",
    "NavigationResult",
    "ToolContext",
    "ToolSurface",
]

_SOURCE_CAP = 64 * 1024
_PATCH_CAP = 16 * 1024
_SIGNAL_CAP = 32
_AUDIT_B64_CAP = 200 * 1024
_AUDIT_BUDGET = 2
_AUDIT_MAX_WIDTH = 640
_SEARCH_REPLACE = _re.compile(
    r"^<<<<<<< SEARCH\n(.*)\n=======\n(.*)\n>>>>>>> REPLACE\n?\Z",
    _re.DOTALL,
)
_PROBE_GRAMMAR = (
    '"component <semantic_id>" | "bounds" | "blockers" | "spawn" | '
    '"aesthetics" | "route <x1> <z1> <x2> <z2>"'
)
_AGENT_RADIUS = 0.4


class PatchResult(_BaseModel):
    """Outcome of a non-executing program patch attempt."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reason: str = ""
    new_source_fingerprint: str = ""


class CompileResult(_BaseModel):
    """Bounded static validation outcome for the current program."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    ok: bool
    stage_outcomes: dict[str, bool] = _Field(default_factory=dict)
    signals: tuple[_Signal, ...] = ()
    model_fingerprint: str = ""
    candidate_fingerprint: str = ""
    reason: str = ""


class ProbeResult(_BaseModel):
    """Read-only measurement packet over the latest compiled candidate."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    ok: bool
    query: str
    data: dict[str, object] = _Field(default_factory=dict)
    reason: str = ""


class RenderResult(_BaseModel):
    """Artifact-ref-only render capture result."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    ok: bool
    view: str = ""
    artifact_path: str = ""
    blake2b256: str = ""
    byte_count: int = 0
    reason: str = ""


class NavigationResult(_BaseModel):
    """Live-runtime navigation validation summary."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    ok: bool
    stage_outcomes: dict[str, bool] = _Field(default_factory=dict)
    signals: tuple[_Signal, ...] = ()
    ticks_used: int = 0
    terminal_reason: str = ""
    path_length_m: float = 0.0
    reason: str = ""


class AuditResult(_BaseModel):
    """Bounded multimodal screenshot audit (JPEG base64 + aesthetics)."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    ok: bool
    refs: tuple[str, ...] = ()
    images_b64: tuple[str, ...] = ()
    aesthetics: dict[str, object] = _Field(default_factory=dict)
    reason: str = ""


@_dataclass
class ToolContext:
    """Mutable tool state for one authoring run."""

    source: str
    limits: _ResourceLimits
    run_dir: _Path
    runlog: _RunLog
    driver: object | None = None
    probe: _NavigationProbe | None = None
    static: _StaticValidation | None = None
    runtime_reports: list[_StageReport] = _field(default_factory=list)
    min_walkable_fraction: float = 0.5
    successful_audits: int = 0


def _source_fingerprint(source: str) -> str:
    return _canonical_fingerprint({"source": source, "sdk_version": _SDK_VERSION})


def _is_unified_diff(patch: str) -> bool:
    return (
        patch.startswith("--- ")
        or patch.startswith("+++ ")
        or "\n@@" in patch
        or patch.startswith("@@")
    )


def _apply_search_replace(source: str, patch: str) -> tuple[str | None, str]:
    match = _SEARCH_REPLACE.match(patch)
    if match is None:
        return None, "search/replace patch must use the exact delimiter format"
    old, new = match.group(1), match.group(2)
    count = source.count(old)
    if count != 1:
        return None, f"search text must occur exactly once (found {count})"
    return source.replace(old, new, 1), ""


def _apply_unified_diff(source: str, patch: str) -> tuple[str | None, str]:
    working = source.splitlines()
    keep_final_newline = source.endswith("\n")
    lines = patch.splitlines()
    if not lines:
        return None, "unified diff is empty"

    index = 0
    while index < len(lines) and (
        lines[index].startswith("--- ") or lines[index].startswith("+++ ")
    ):
        index += 1

    offset = 0
    saw_hunk = False
    while index < len(lines):
        header = lines[index]
        match = _re.match(
            r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@",
            header,
        )
        if match is None:
            return None, "unified diff hunk header mismatch"
        saw_hunk = True
        old_start = int(match.group(1))
        index += 1
        hunk_old: list[str] = []
        hunk_new: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@"):
            row = lines[index]
            if row.startswith("\\"):
                index += 1
                continue
            if row.startswith(" "):
                hunk_old.append(row[1:])
                hunk_new.append(row[1:])
            elif row.startswith("-"):
                hunk_old.append(row[1:])
            elif row.startswith("+"):
                hunk_new.append(row[1:])
            else:
                return None, "unified diff line prefix mismatch"
            index += 1

        if not hunk_old:
            return None, "insertion hunks require at least one context line"

        at = old_start - 1 + offset
        if at < 0 or at + len(hunk_old) > len(working):
            return None, "unified diff hunk mismatch"
        if working[at : at + len(hunk_old)] != hunk_old:
            return None, "unified diff hunk mismatch"
        working[at : at + len(hunk_old)] = hunk_new
        offset += len(hunk_new) - len(hunk_old)

    if not saw_hunk:
        return None, "unified diff has no hunks"

    text = "\n".join(working)
    if keep_final_newline:
        text += "\n"
    return text, ""


def _yaw_from_basis(node: _SceneNode) -> float:
    return _math.atan2(-node.transform.basis_x.z, node.transform.basis_x.x)


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


def _blocker_oriented(node: _SceneNode) -> dict[str, object] | None:
    collider = node.collider
    if collider is None or isinstance(node.visual, _PlaneVisual):
        return None
    if collider.shape is _ColliderShape.CYLINDER:
        radius = float(collider.dimensions["radius"])
        half_x = half_z = radius
    elif "x" in collider.dimensions and "z" in collider.dimensions:
        half_x = float(collider.dimensions["x"]) / 2.0
        half_z = float(collider.dimensions["z"]) / 2.0
    else:
        return None
    return {
        "semantic_id": node.semantic_id,
        "origin_x": node.transform.origin.x,
        "origin_z": node.transform.origin.z,
        "yaw": _yaw_from_basis(node),
        "half_x": half_x,
        "half_z": half_z,
    }


def _blocker_rect(node: _SceneNode) -> dict[str, object] | None:
    """Axis-aligned world AABB of an oriented blocker (listing approximation)."""

    oriented = _blocker_oriented(node)
    if oriented is None:
        return None
    half_x = float(oriented["half_x"])
    half_z = float(oriented["half_z"])
    yaw = float(oriented["yaw"])
    cx = float(oriented["origin_x"])
    cz = float(oriented["origin_z"])
    cos_yaw = abs(_math.cos(yaw))
    sin_yaw = abs(_math.sin(yaw))
    extent_x = half_x * cos_yaw + half_z * sin_yaw
    extent_z = half_x * sin_yaw + half_z * cos_yaw
    return {
        "semantic_id": oriented["semantic_id"],
        "min_x": cx - extent_x,
        "max_x": cx + extent_x,
        "min_z": cz - extent_z,
        "max_z": cz + extent_z,
    }


def _segment_hits_oriented(
    x1: float,
    z1: float,
    x2: float,
    z2: float,
    oriented: dict[str, object],
) -> bool:
    """Return whether a segment intersects a yaw-aware blocker footprint."""

    origin_x = float(oriented["origin_x"])
    origin_z = float(oriented["origin_z"])
    yaw = float(oriented["yaw"])
    half_x = float(oriented["half_x"])
    half_z = float(oriented["half_z"])
    lx1, lz1 = _local_xz(x1, z1, origin_x, origin_z, yaw)
    lx2, lz2 = _local_xz(x2, z2, origin_x, origin_z, yaw)

    def _point_in(x: float, z: float) -> bool:
        return abs(x) <= half_x and abs(z) <= half_z

    if _point_in(lx1, lz1) or _point_in(lx2, lz2):
        return True

    dx = lx2 - lx1
    dz = lz2 - lz1
    p = (-dx, dx, -dz, dz)
    q = (lx1 - (-half_x), half_x - lx1, lz1 - (-half_z), half_z - lz1)
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0.0:
            if qi < 0.0:
                return False
            continue
        t = qi / pi
        if pi < 0.0:
            u1 = max(u1, t)
        else:
            u2 = min(u2, t)
        if u1 > u2:
            return False
    return True


def _clearance_to_oriented(
    x: float,
    z: float,
    oriented: dict[str, object],
) -> float:
    local_x, local_z = _local_xz(
        x,
        z,
        float(oriented["origin_x"]),
        float(oriented["origin_z"]),
        float(oriented["yaw"]),
    )
    dx = max(abs(local_x) - float(oriented["half_x"]), 0.0)
    dz = max(abs(local_z) - float(oriented["half_z"]), 0.0)
    return _math.hypot(dx, dz)


def _prop_keepout_radius(kit: object, scale: float) -> float:
    reach = 0.0
    for part in kit.parts:  # type: ignore[attr-defined]
        if part.shape == "box":
            assert part.size is not None
            half_x = part.size[0] / 2.0
            half_z = part.size[2] / 2.0
        else:
            assert part.radius is not None
            half_x = half_z = float(part.radius)
        part_reach = max(
            abs(float(part.offset[0])) + half_x,
            abs(float(part.offset[2])) + half_z,
        )
        reach = max(reach, part_reach)
    return float(scale) * reach


def _placement_origins(
    model: _EnvironmentModel,
    candidate: _CandidateScene,
) -> list[tuple[float, float]]:
    """Scatter/prop instance origins (each instance's ``.0`` kit-part node)."""

    by_id = {node.semantic_id: node for node in candidate.scene.nodes}
    origins: list[tuple[float, float]] = []
    for component in model.components:
        discriminator = component.payload.get("component")
        name = component.semantic_id
        if discriminator == "prop":
            node = by_id.get(f"{name}.0")
            if node is not None:
                origins.append((node.transform.origin.x, node.transform.origin.z))
        elif discriminator == "scatter":
            for node in candidate.scene.nodes:
                parts = node.semantic_id.split(".")
                if (
                    len(parts) == 3
                    and parts[0] == name
                    and parts[2] == "0"
                ):
                    origins.append(
                        (node.transform.origin.x, node.transform.origin.z)
                    )
    return origins


def _nearest_neighbor_stats(
    origins: list[tuple[float, float]],
) -> tuple[float, float, float, float]:
    if len(origins) < 2:
        return 0.0, 0.0, 0.0, 0.0
    nearest: list[float] = []
    for index, (x, z) in enumerate(origins):
        best = min(
            _math.hypot(x - ox, z - oz)
            for other, (ox, oz) in enumerate(origins)
            if other != index
        )
        nearest.append(best)
    nn_min = min(nearest)
    nn_mean = sum(nearest) / len(nearest)
    variance = sum((value - nn_mean) ** 2 for value in nearest) / len(nearest)
    nn_stddev = _math.sqrt(variance)
    if nn_mean <= 1e-12:
        cluster_score = 0.0
    else:
        cluster_score = nn_stddev / nn_mean
    return nn_min, nn_mean, nn_stddev, cluster_score


def _aesthetics_probe(
    model: _EnvironmentModel,
    candidate: _CandidateScene,
) -> dict[str, object]:
    origins = _placement_origins(model, candidate)
    nn_min, nn_mean, nn_stddev, cluster_score = _nearest_neighbor_stats(origins)

    ground = next(
        (
            node
            for node in candidate.scene.nodes
            if isinstance(node.visual, _PlaneVisual)
        ),
        None,
    )
    if ground is None or not isinstance(ground.visual, _PlaneVisual):
        raise ValueError("candidate has no ground plane")
    ground_area = float(ground.visual.size_x) * float(ground.visual.size_z)

    prop_ids = {
        component.semantic_id
        for component in model.components
        if component.payload.get("component") == "prop"
    }
    blocker_area = 0.0
    for node in candidate.scene.nodes:
        oriented = _blocker_oriented(node)
        if oriented is None:
            continue
        semantic_id = str(oriented["semantic_id"])
        if any(
            semantic_id == prop_id or semantic_id.startswith(f"{prop_id}.")
            for prop_id in prop_ids
        ):
            continue
        blocker_area += 4.0 * float(oriented["half_x"]) * float(oriented["half_z"])

    for component in model.components:
        if component.payload.get("component") != "prop":
            continue
        kit = _get_kit(str(component.payload["kit"]))
        if not kit.blocking:
            continue
        scale = float(component.payload["scale"])  # type: ignore[arg-type]
        radius = _prop_keepout_radius(kit, scale)
        blocker_area += _math.pi * radius * radius

    coverage_fraction = blocker_area / ground_area if ground_area > 0.0 else 0.0

    spawn_id = candidate.scene.controller_semantic_id
    spawn = next(
        node for node in candidate.scene.nodes if node.semantic_id == spawn_id
    )
    target_id: str | None = None
    for component in model.components:
        discriminator = component.payload.get("component")
        if discriminator == "landmark":
            target_id = f"{component.semantic_id}.0"
            break
        if discriminator == "prop":
            kit = _get_kit(str(component.payload["kit"]))
            if kit.category == "landmark":
                target_id = f"{component.semantic_id}.0"
                break

    sightline_clear = True
    sightline_blocker: str | None = None
    if target_id is not None:
        target = next(
            (
                node
                for node in candidate.scene.nodes
                if node.semantic_id == target_id
            ),
            None,
        )
        if target is not None:
            x1 = spawn.transform.origin.x
            z1 = spawn.transform.origin.z
            x2 = target.transform.origin.x
            z2 = target.transform.origin.z
            target_root = target_id.rsplit(".", 1)[0]
            for node in candidate.scene.nodes:
                if node.semantic_id == target_root or node.semantic_id.startswith(
                    f"{target_root}."
                ):
                    continue
                oriented = _blocker_oriented(node)
                if oriented is None:
                    continue
                if _segment_hits_oriented(x1, z1, x2, z2, oriented):
                    sightline_clear = False
                    sightline_blocker = node.semantic_id
                    break

    guidance: list[str] = []
    if len(origins) >= 2 and cluster_score > 0.6:
        guidance.append(
            "placements look clumpy; regroup with purpose or widen spacing"
        )
    if coverage_fraction < 0.02 and len(origins) <= 3:
        guidance.append(
            "scene looks sparse-and-empty; add deliberate props or denser scatter"
        )
    if not sightline_clear:
        guidance.append(
            "spawn→landmark sightline is blocked; open a corridor or move the goal"
        )

    return {
        "instance_count": len(origins),
        "nn_min": nn_min,
        "nn_mean": nn_mean,
        "nn_stddev": nn_stddev,
        "cluster_score": cluster_score,
        "coverage_fraction": coverage_fraction,
        "sightline_clear": sightline_clear,
        "sightline_blocker": sightline_blocker,
        "guidance": guidance,
    }


def _resolve_artifact_path(run_dir: _Path, artifact_path: str) -> _Path:
    path = _Path(artifact_path)
    if path.is_file():
        return path
    # The default driver factory roots the runtime under run_dir/"runtime",
    # so driver-relative refs like "artifacts/x.png" live one level down.
    for base in (run_dir, run_dir / "runtime"):
        candidate = base / artifact_path
        if candidate.is_file():
            return candidate
    matches = sorted(run_dir.rglob(path.name))
    if matches:
        return matches[0]
    raise FileNotFoundError(f"artifact not found: {artifact_path}")


def _encode_audit_jpeg(png_path: _Path) -> str:
    """Downscale a PNG and return a base64 JPEG string within the hard cap."""

    from PIL import Image as _Image

    with _Image.open(png_path) as image:
        working = image.convert("RGB")
        if working.width > _AUDIT_MAX_WIDTH:
            ratio = _AUDIT_MAX_WIDTH / float(working.width)
            height = max(1, int(round(working.height * ratio)))
            working = working.resize(
                (_AUDIT_MAX_WIDTH, height),
                _Image.Resampling.LANCZOS,
            )

        def _encode(quality: int) -> str:
            buffer = _io.BytesIO()
            working.save(buffer, format="JPEG", quality=quality, optimize=True)
            return _base64.b64encode(buffer.getvalue()).decode("ascii")

        encoded = _encode(70)
        if len(encoded.encode("utf-8")) > _AUDIT_B64_CAP:
            encoded = _encode(45)
        if len(encoded.encode("utf-8")) > _AUDIT_B64_CAP:
            raise ValueError(
                f"encoded audit image exceeds {_AUDIT_B64_CAP} byte cap"
            )
        return encoded


class ToolSurface:
    """The agent's complete seven-tool authoring surface."""

    def __init__(self, context: ToolContext) -> None:
        self.context = context

    def _log(self, name: str, args: dict, outcome: dict) -> None:
        self.context.runlog.append(
            f"tool.{name}",
            {"args": args, "outcome": outcome},
        )

    def read_program(self) -> str:
        """Return the current program source, rejecting oversize programs."""

        source = self.context.source
        if len(source.encode("utf-8")) > _SOURCE_CAP:
            self._log(
                "read_program",
                {},
                {"ok": False, "reason": "program source exceeds 64 kib read cap"},
            )
            raise ValueError("program source exceeds 64 kib read cap")
        self._log("read_program", {}, {"ok": True, "bytes": len(source.encode("utf-8"))})
        # Redact before truncating so secrets near the end of a long source are not
        # left behind by a blind prefix cut.
        redacted_source = _redact_value(source)
        if isinstance(redacted_source, str) and len(redacted_source) > 512:
            redacted_source = redacted_source[:512]
        self.context.runlog.append(
            "tool.read_program.source",
            {"source": redacted_source},
        )
        return source

    def patch_program(self, patch: str) -> PatchResult:
        """Apply a unified diff or search/replace patch without executing code."""

        args = {"patch_bytes": len(patch.encode("utf-8")), "preview": patch[:200]}
        if not self.context.source.strip():
            result = PatchResult(
                ok=False,
                reason=(
                    "no program exists yet; reply with the complete program in "
                    "one ```python fenced block (not a tool call)"
                ),
            )
            self._log("patch_program", args, result.model_dump())
            return result
        if len(patch.encode("utf-8")) > _PATCH_CAP:
            result = PatchResult(ok=False, reason="patch exceeds 16 kib cap")
            self._log("patch_program", args, result.model_dump())
            return result

        if _SEARCH_REPLACE.match(patch):
            updated, reason = _apply_search_replace(self.context.source, patch)
        elif _is_unified_diff(patch):
            updated, reason = _apply_unified_diff(self.context.source, patch)
        else:
            updated, reason = None, "patch must be a unified diff or search/replace block"

        if updated is None:
            result = PatchResult(ok=False, reason=reason)
            self._log("patch_program", args, result.model_dump())
            return result

        if len(updated.encode("utf-8")) > _SOURCE_CAP:
            result = PatchResult(ok=False, reason="patched source exceeds 64 kib cap")
            self._log("patch_program", args, result.model_dump())
            return result

        self.context.source = updated
        self.context.static = None
        self.context.runtime_reports = []
        fingerprint = _source_fingerprint(updated)
        result = PatchResult(
            ok=True,
            reason="patched",
            new_source_fingerprint=fingerprint,
        )
        self._log("patch_program", args, result.model_dump())
        return result

    def compile_environment(self) -> CompileResult:
        """Run static validation stages program through scene."""

        static = _validate_static(self.context.source, limits=self.context.limits)
        self.context.static = static
        self.context.runtime_reports = []
        outcomes = {report.stage.value: report.passed for report in static.reports}
        signals: list[_Signal] = []
        for report in static.reports:
            signals.extend(report.signals)
        signals = signals[:_SIGNAL_CAP]
        ok = bool(static.reports) and all(report.passed for report in static.reports)
        result = CompileResult(
            ok=ok,
            stage_outcomes=outcomes,
            signals=tuple(signals),
            model_fingerprint=(
                static.model.model_fingerprint if static.model is not None else ""
            ),
            candidate_fingerprint=(
                static.candidate.candidate_fingerprint
                if static.candidate is not None
                else ""
            ),
            reason="" if ok else "static validation failed",
        )
        self._log(
            "compile_environment",
            {"source_fingerprint": _source_fingerprint(self.context.source)},
            {
                "ok": result.ok,
                "stages": result.stage_outcomes,
                "signal_count": len(result.signals),
            },
        )
        return result

    def probe_environment(self, query: str) -> ProbeResult:
        """Return read-only measurements over the latest compiled model/candidate."""

        static = self.context.static
        if static is None or static.candidate is None:
            result = ProbeResult(
                ok=False,
                query=query,
                reason="probe requires a prior successful compile",
            )
            self._log("probe_environment", {"query": query}, result.model_dump())
            return result

        model = static.model
        candidate = static.candidate
        try:
            data = self._probe_data(query, model, candidate)
        except ValueError as exc:
            result = ProbeResult(ok=False, query=query, reason=str(exc))
            self._log("probe_environment", {"query": query}, result.model_dump())
            return result

        result = ProbeResult(ok=True, query=query, data=data)
        self._log(
            "probe_environment",
            {"query": query},
            {"ok": True, "keys": sorted(data.keys())},
        )
        return result

    def _probe_data(
        self,
        query: str,
        model: _EnvironmentModel | None,
        candidate: _CandidateScene,
    ) -> dict[str, object]:
        text = query.strip()
        if text.startswith("component "):
            if model is None:
                raise ValueError("component probe requires a compiled model")
            semantic_id = text[len("component ") :].strip()
            for component in model.components:
                if component.semantic_id == semantic_id:
                    return {
                        "semantic_id": semantic_id,
                        "kind": component.kind.value,
                        "payload": _copy.deepcopy(component.payload),
                    }
            raise ValueError(f"unknown component semantic id: {semantic_id}")

        if text == "bounds":
            ground = next(
                (
                    node
                    for node in candidate.scene.nodes
                    if isinstance(node.visual, _PlaneVisual)
                ),
                None,
            )
            if ground is None or not isinstance(ground.visual, _PlaneVisual):
                raise ValueError("candidate has no ground plane")
            return {
                "ground_semantic_id": ground.semantic_id,
                "center": [
                    ground.transform.origin.x,
                    ground.transform.origin.z,
                ],
                "size_x": ground.visual.size_x,
                "size_z": ground.visual.size_z,
                "node_count": len(candidate.scene.nodes),
            }

        if text == "blockers":
            blockers = []
            for node in candidate.scene.nodes:
                rect = _blocker_rect(node)
                if rect is not None:
                    blockers.append(rect)
            return {"blockers": blockers}

        if text == "spawn":
            spawn_id = candidate.scene.controller_semantic_id
            spawn = next(
                node
                for node in candidate.scene.nodes
                if node.semantic_id == spawn_id
            )
            sx = spawn.transform.origin.x
            sz = spawn.transform.origin.z
            clearances = []
            nearest = None
            nearest_distance = None
            for node in candidate.scene.nodes:
                oriented = _blocker_oriented(node)
                if oriented is None:
                    continue
                distance = _clearance_to_oriented(sx, sz, oriented)
                clearances.append(
                    {
                        "semantic_id": node.semantic_id,
                        "clearance": distance,
                    }
                )
                if nearest_distance is None or distance < nearest_distance:
                    nearest_distance = distance
                    nearest = node.semantic_id
            return {
                "position": [sx, sz],
                "nearest_blocker": nearest,
                "nearest_clearance": (
                    nearest_distance if nearest_distance is not None else 0.0
                ),
                "agent_radius": _AGENT_RADIUS,
                "clearances": clearances,
            }

        if text == "aesthetics":
            if model is None:
                raise ValueError("aesthetics probe requires a compiled model")
            return _aesthetics_probe(model, candidate)

        route_match = _re.fullmatch(
            r"route\s+([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)\s+"
            r"([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+(?:\.\d+)?)",
            text,
        )
        if route_match is not None:
            x1, z1, x2, z2 = (float(group) for group in route_match.groups())
            hits = []
            for node in candidate.scene.nodes:
                oriented = _blocker_oriented(node)
                if oriented is None:
                    continue
                if _segment_hits_oriented(x1, z1, x2, z2, oriented):
                    hits.append(node.semantic_id)
            return {
                "distance": _math.hypot(x2 - x1, z2 - z1),
                "blockers_intersected": hits,
            }

        raise ValueError(f"unknown probe query; expected {_PROBE_GRAMMAR}")

    def render_environment(
        self,
        view: _Literal["isometric", "topdown"] | str,
    ) -> RenderResult:
        """Capture a render artifact ref through the optional runtime driver."""

        if view not in {"isometric", "topdown"}:
            result = RenderResult(ok=False, view=str(view), reason="view must be isometric or topdown")
            self._log("render_environment", {"view": view}, result.model_dump())
            return result
        if self.context.driver is None:
            result = RenderResult(
                ok=False,
                view=view,
                reason="driver unavailable in static context",
            )
            self._log("render_environment", {"view": view}, result.model_dump())
            return result
        try:
            artifact = self.context.driver.render(view)
            path = str(getattr(artifact, "path", ""))
            digest = str(getattr(artifact, "blake2b256", ""))
            byte_count = int(getattr(artifact, "byte_count", 0))
            if byte_count <= 0 or len(digest) != 64 or not path:
                raise RuntimeError("render artifact is not verified")
            result = RenderResult(
                ok=True,
                view=view,
                artifact_path=path,
                blake2b256=digest,
                byte_count=byte_count,
            )
        except Exception as exc:
            result = RenderResult(
                ok=False,
                view=view,
                reason=f"render failed: {exc}",
            )
        self._log(
            "render_environment",
            {"view": view},
            {
                "ok": result.ok,
                "artifact_path": result.artifact_path,
                "byte_count": result.byte_count,
            },
        )
        return result

    def audit_render(self) -> AuditResult:
        """Capture both views, encode bounded JPEGs, and attach aesthetics."""

        if self.context.successful_audits >= _AUDIT_BUDGET:
            result = AuditResult(
                ok=False,
                reason="audit budget exhausted (2 per run)",
            )
            self._log("audit_render", {}, {"ok": False, "reason": result.reason})
            return result
        if self.context.driver is None:
            result = AuditResult(
                ok=False,
                reason="driver unavailable in static context",
            )
            self._log("audit_render", {}, {"ok": False, "reason": result.reason})
            return result

        static = self.context.static
        if (
            static is None
            or static.model is None
            or static.candidate is None
            or not static.reports
            or not all(report.passed for report in static.reports)
        ):
            result = AuditResult(
                ok=False,
                reason="audit requires a prior successful compile",
            )
            self._log("audit_render", {}, {"ok": False, "reason": result.reason})
            return result

        try:
            load = getattr(self.context.driver, "load_candidate", None)
            if callable(load):
                response = load(static.candidate)
                if response is not None and hasattr(response, "ok") and not response.ok:
                    raise RuntimeError("load_candidate returned a non-ok response")
                # Settle the navigation bake the load kicked off; leaving it
                # in flight wedges every later load with "bake in progress".
                wait_ready = getattr(
                    self.context.driver, "wait_navigation_ready", None
                )
                if callable(wait_ready):
                    wait_ready(30.0)

            refs: list[str] = []
            images: list[str] = []
            for view in ("isometric", "topdown"):
                artifact = self.context.driver.render(view)
                path = str(getattr(artifact, "path", ""))
                digest = str(getattr(artifact, "blake2b256", ""))
                byte_count = int(getattr(artifact, "byte_count", 0))
                if byte_count <= 0 or len(digest) != 64 or not path:
                    raise RuntimeError("render artifact is not verified")
                png_path = _resolve_artifact_path(self.context.run_dir, path)
                images.append(_encode_audit_jpeg(png_path))
                refs.append(path)

            aesthetics = _aesthetics_probe(static.model, static.candidate)
            result = AuditResult(
                ok=True,
                refs=tuple(refs),
                images_b64=tuple(images),
                aesthetics=aesthetics,
            )
            self.context.successful_audits += 1
        except Exception as exc:
            result = AuditResult(ok=False, reason=f"audit failed: {exc}")

        self._log(
            "audit_render",
            {},
            {
                "ok": result.ok,
                "refs": list(result.refs),
                "byte_sizes": [len(item.encode("utf-8")) for item in result.images_b64],
                "aesthetics": result.aesthetics,
                "reason": result.reason,
            },
        )
        return result

    def simulate_navigation(self) -> NavigationResult:
        """Run live-runtime stages materialization through camera when available."""

        if self.context.driver is None:
            result = NavigationResult(
                ok=False,
                reason="driver unavailable in static context",
            )
            self._log("simulate_navigation", {}, result.model_dump())
            return result
        static = self.context.static
        if (
            static is None
            or static.model is None
            or static.candidate is None
            or not all(report.passed for report in static.reports)
        ):
            result = NavigationResult(
                ok=False,
                reason="simulate requires a prior successful compile",
            )
            self._log("simulate_navigation", {}, result.model_dump())
            return result
        if self.context.probe is None:
            signal = _Signal(
                code="v7.no_landmark",
                severity=_SignalSeverity.FAILURE,
                message="navigation probe requires a declared landmark",
                guidance="declare a landmark so navigation has a distinct goal",
            )
            result = NavigationResult(
                ok=False,
                stage_outcomes={_HardStage.CONTROLLER.value: False},
                signals=(signal,),
                reason="navigation probe requires a declared landmark",
            )
            self._log("simulate_navigation", {}, result.model_dump())
            return result

        reports = _validate_candidate(
            static.model,
            static.candidate,
            self.context.driver,
            probe=self.context.probe,
            min_walkable_fraction=self.context.min_walkable_fraction,
        )
        self.context.runtime_reports = list(reports)
        outcomes = {report.stage.value: report.passed for report in reports}
        ok = bool(reports) and all(report.passed for report in reports)
        signals: list[_Signal] = []
        for report in reports:
            signals.extend(report.signals)
        signals = signals[:_SIGNAL_CAP]

        ticks_used = 0
        terminal_reason = ""
        path_length_m = 0.0
        controller = next(
            (report for report in reports if report.stage is _HardStage.CONTROLLER),
            None,
        )
        if controller is not None:
            for signal in controller.signals:
                measurements = signal.measurements
                if "terminal_reason" in measurements:
                    ticks_used = int(measurements.get("ticks_used", 0))
                    terminal_reason = str(measurements.get("terminal_reason", ""))
                    path_length_m = float(measurements.get("path_length_m", 0.0))
                    break

        result = NavigationResult(
            ok=ok,
            stage_outcomes=outcomes,
            signals=tuple(signals),
            ticks_used=ticks_used,
            terminal_reason=terminal_reason,
            path_length_m=path_length_m,
            reason="" if ok else "runtime validation failed",
        )
        self._log(
            "simulate_navigation",
            {"probe": self.context.probe.target_landmark_id},
            {
                "ok": result.ok,
                "stages": result.stage_outcomes,
                "signal_count": len(result.signals),
                "terminal_reason": result.terminal_reason,
            },
        )
        return result
