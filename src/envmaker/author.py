"""Agent-driven authoring sessions: an enclosing coding agent is the model.

The API loop (`envmaker run`) brings its own provider; this module inverts
that: an agent such as Claude Code or Codex edits ``environment.py`` directly
with its native file tools and drives the same validators through discrete
CLI steps. Same runlog, same typed signals, same sealed definitions.
"""

from __future__ import annotations

from dataclasses import dataclass as _dataclass
from dataclasses import field as _field
from pathlib import Path as _Path
from typing import Callable as _Callable

from envmaker.agent.loop import select_landmark_probe as _select_landmark_probe
from envmaker.agent.prompts import AGENT_CONTRACT as _AGENT_CONTRACT
from envmaker.agent.prompts import PROMPT_VERSION as _PROMPT_VERSION
from envmaker.agent.tools import _aesthetics_probe
from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.artifacts import canonical_json as _canonical_json
from envmaker.core.definition import seal_definition as _seal_definition
from envmaker.core.program import EnvironmentProgram as _EnvironmentProgram
from envmaker.core.program import ProviderInfo as _ProviderInfo
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.requirements import PromptRequirementSet as _PromptRequirementSet
from envmaker.runlog import RunLog as _RunLog
from envmaker.validation import full_bundle as _full_bundle
from envmaker.validation import validate_candidate as _validate_candidate
from envmaker.validation import validate_static as _validate_static

__all__ = [
    "AGENT_WORKFLOW",
    "STARTER_TEMPLATE",
    "StepOutcome",
    "init_session",
    "step_session",
]

_DEFAULT_LIMITS = _ResourceLimits(
    cpu_seconds=10.0,
    memory_mb=512,
    output_bytes=262144,
    wall_seconds=30.0,
)

AGENT_WORKFLOW = """\
AGENT-DRIVEN AUTHORING WORKFLOW
You (the coding agent) are the authoring model. The harness validates.

1. Read the SDK contract below, then write the complete program into
   <run_dir>/environment.py with your file tools.
2. Run: uv run envmaker author step <run_dir>
   The harness compiles, validates (9 hard stages), simulates traversal,
   and captures renders. Every failure is a typed signal with a stable
   code, exact measurements, and repair guidance.
3. OPEN THE RENDER PNGs it prints and look at them. Check the world
   against the user's request (inside/outside/open/closed where asked?)
   and the composition (clusters, voids, sightlines, scale variety).
4. Edit environment.py and re-run `author step` until it prints ACCEPTED
   (all nine stages pass and the sealed definition is persisted).
5. Show the human: uv run envmaker author open <run_dir>
"""

STARTER_TEMPLATE = '''"""Write your environment program here, then run `author step`.

Contract: define build_environment() -> EnvironmentModel and end the file
with `environment = build_environment()`. Imports allowed: envmaker.sdk, math.
"""

from envmaker.sdk import EnvironmentBuilder, Polygon2D


def build_environment():
    raise NotImplementedError("author your environment, then delete this line")


environment = build_environment()
'''


@_dataclass(frozen=True)
class StepOutcome:
    """One `author step` result for CLI formatting and tests."""

    status: str  # "empty" | "static_failed" | "runtime_failed" | "accepted"
    stages: dict[str, bool] = _field(default_factory=dict)
    signals: tuple[tuple[str, str, str], ...] = ()  # (code, message, guidance)
    renders: tuple[str, ...] = ()
    aesthetics: dict[str, object] = _field(default_factory=dict)
    definition_path: str | None = None
    definition_fingerprint: str | None = None


def _session_paths(run_dir: _Path) -> tuple[_Path, _Path, _Path]:
    return run_dir / "environment.py", run_dir / "prompt.txt", run_dir / "runlog.jsonl"


def init_session(prompt: str, seed: int, run_dir: _Path) -> _Path:
    """Create the session layout and record the prompt."""

    run_dir.mkdir(parents=True, exist_ok=False)
    program_path, prompt_path, runlog_path = _session_paths(run_dir)
    prompt_path.write_text(
        f"prompt: {prompt}\nseed: {seed}\n", encoding="utf-8"
    )
    program_path.write_text(STARTER_TEMPLATE, encoding="utf-8")
    runlog = _RunLog(runlog_path)
    runlog.append("system_prompt", {"mode": "agent-driven"})
    runlog.append("user_prompt", {"prompt": prompt, "seed": seed})
    return run_dir


def read_session_prompt(run_dir: _Path) -> tuple[str, int]:
    """Recover (prompt, seed) recorded by init_session."""

    _program, prompt_path, _runlog = _session_paths(run_dir)
    prompt = ""
    seed = 0
    for line in prompt_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("prompt: "):
            prompt = line[len("prompt: ") :]
        elif line.startswith("seed: "):
            seed = int(line[len("seed: ") :])
    return prompt, seed


def _snapshot_revision(run_dir: _Path, source: str, runlog: _RunLog) -> None:
    """Persist each distinct source into revisions/rev-N.py (API-loop parity)."""

    revision_dir = run_dir / "revisions"
    revision_dir.mkdir(exist_ok=True)
    existing = sorted(
        revision_dir.glob("rev-*.py"),
        key=lambda p: int(p.stem.split("-")[1]),
    )
    if existing and existing[-1].read_text(encoding="utf-8") == source:
        return
    index = len(existing) + 1
    path = revision_dir / f"rev-{index}.py"
    path.write_text(source, encoding="utf-8")
    runlog.append(
        "revision",
        {
            "index": index,
            "path": f"revisions/rev-{index}.py",
            "bytes": len(source.encode("utf-8")),
        },
    )


def _signal_rows(reports: object) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for report in reports:  # type: ignore[attr-defined]
        if report.passed:
            continue
        for signal in report.signals:
            rows.append(
                (
                    str(signal.code),
                    str(signal.message),
                    str(getattr(signal, "guidance", "") or ""),
                )
            )
    return rows


def step_session(
    run_dir: _Path,
    *,
    driver_factory: _Callable[[_Path], object] | None = None,
) -> StepOutcome:
    """Validate the session's current environment.py end to end."""

    program_path, _prompt_path, runlog_path = _session_paths(run_dir)
    source = program_path.read_text(encoding="utf-8")
    runlog = _RunLog(runlog_path)
    prompt, _seed = read_session_prompt(run_dir)

    if "raise NotImplementedError" in source:
        runlog.append("step", {"status": "empty"})
        return StepOutcome(status="empty")

    _snapshot_revision(run_dir, source, runlog)

    static = _validate_static(source, limits=_DEFAULT_LIMITS)
    stages = {report.stage.value: report.passed for report in static.reports}
    static_signals = tuple(_signal_rows(static.reports))
    if (
        static.model is None
        or static.candidate is None
        or not all(stages.values())
    ):
        runlog.append(
            "step",
            {"status": "static_failed", "stages": stages},
        )
        return StepOutcome(
            status="static_failed", stages=stages, signals=static_signals
        )

    driver = None
    renders: list[str] = []
    try:
        try:
            if driver_factory is not None:
                driver = driver_factory(run_dir)
            else:
                from envmaker.runtime import RuntimeDriver
                import uuid as _uuid

                driver = RuntimeDriver(
                    run_dir=run_dir / "runtime",
                    session_id="author-" + _uuid.uuid4().hex[:12],
                    windowed=True,
                    hidden=True,
                )
                driver.start()
        except Exception as exc:
            # Static validation already passed; report the runtime as
            # unavailable with a remedy instead of crashing the session.
            runlog.append(
                "step",
                {"status": "runtime_unavailable", "stages": stages},
            )
            return StepOutcome(
                status="runtime_unavailable",
                stages=stages,
                signals=(
                    (
                        "harness.godot_unavailable",
                        f"runtime unavailable: {exc}",
                        "static stages all passed; on a headless Linux box "
                        "run under a virtual display (e.g. xvfb-run)",
                    ),
                ),
            )

        probe = _select_landmark_probe(static.model, static.candidate)
        if probe is None:
            # Landmark-free environments get a synthesized farthest-point
            # probe; None means even that failed (no clear ground sample).
            runlog.append("step", {"status": "runtime_failed"})
            return StepOutcome(
                status="runtime_failed",
                stages=stages,
                signals=(
                    (
                        "v7.no_probe_target",
                        "no navigation target could be resolved (no landmark "
                        "and no clear ground point beyond 1.5 m of spawn)",
                        "enlarge the open ground or declare a landmark",
                    ),
                ),
            )

        runtime_reports = _validate_candidate(
            static.model, static.candidate, driver, probe=probe
        )
        for report in runtime_reports:
            stages[report.stage.value] = report.passed
        runtime_signals = tuple(_signal_rows(runtime_reports))

        for view in ("isometric", "topdown"):
            try:
                artifact = driver.render(view)  # type: ignore[attr-defined]
                rel = str(getattr(artifact, "path", ""))
                if rel:
                    renders.append(str(run_dir / "runtime" / rel))
            except Exception:
                pass

        aesthetics = _aesthetics_probe(static.model, static.candidate)

        runtime_clean = bool(runtime_reports) and all(
            report.passed for report in runtime_reports
        )
        if not runtime_clean:
            runlog.append(
                "step",
                {"status": "runtime_failed", "stages": stages},
            )
            return StepOutcome(
                status="runtime_failed",
                stages=stages,
                signals=static_signals + runtime_signals,
                renders=tuple(renders),
                aesthetics=aesthetics,
            )

        bundle = _full_bundle(static, list(runtime_reports))
        requirements = _PromptRequirementSet(prompt=prompt, requirements=())
        program = _EnvironmentProgram(
            source=source,
            sdk_version=static.model.sdk_version,
            prompt_fingerprint=requirements.prompt_fingerprint,
            provider=_ProviderInfo(
                provider="agent-driven",
                model_name="external-agent",
                prompt_version=_PROMPT_VERSION,
            ),
        )
        definition = _seal_definition(
            static.candidate,
            bundle,
            requirements=requirements,
            program=program,
            model=static.model,
            navmesh_fingerprint=_canonical_fingerprint(
                {"candidate": static.candidate.candidate_fingerprint}
            ),
        )
        rel_path = "environment-definition.json"
        (run_dir / rel_path).write_text(
            _canonical_json(definition), encoding="utf-8"
        )
        runlog.append(
            "outcome",
            {
                "terminal_state": "accepted",
                "bundle_sealed": True,
                "definition_path": rel_path,
                "definition_fingerprint": definition.definition_fingerprint,
            },
        )
        return StepOutcome(
            status="accepted",
            stages=stages,
            renders=tuple(renders),
            aesthetics=aesthetics,
            definition_path=rel_path,
            definition_fingerprint=definition.definition_fingerprint,
        )
    finally:
        if driver is not None:
            try:
                driver.close()  # type: ignore[attr-defined]
            except Exception:
                pass


def agent_instructions() -> str:
    """Everything the enclosing agent needs, in one printout.

    Uses AGENT_CONTRACT, not the API-loop SYSTEM_PROMPT: the coding agent
    writes environment.py with its own file tools, so the fenced-reply and
    tool-protocol rules do not apply here.
    """

    return AGENT_WORKFLOW + "\n\nSDK CONTRACT AND GUIDANCE\n" + _AGENT_CONTRACT
