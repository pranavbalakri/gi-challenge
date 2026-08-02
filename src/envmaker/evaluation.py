"""Compact evaluation harness: oneshot baseline vs repair-loop variant."""

from __future__ import annotations

import json as _json
import time as _time
import traceback as _traceback
import uuid as _uuid
from collections.abc import Callable as _Callable
from dataclasses import asdict as _asdict
from dataclasses import dataclass as _dataclass
from pathlib import Path as _Path
from typing import Any as _Any
from typing import Literal as _Literal

import yaml as _yaml

from envmaker.agent.loop import AuthoringOutcome as _AuthoringOutcome
from envmaker.agent.loop import run_authoring as _run_authoring
from envmaker.agent.prompts import PROMPT_VERSION as _PROMPT_VERSION
from envmaker.agent.prompts import SYSTEM_PROMPT as _SYSTEM_PROMPT
from envmaker.agent.prompts import build_user_prompt as _build_user_prompt
from envmaker.agent.providers import OpenAIProvider as _OpenAIProvider
from envmaker.agent.providers import Provider as _Provider
from envmaker.agent.providers import ProviderError as _ProviderError
from envmaker.agent.tools import ToolContext as _ToolContext
from envmaker.agent.tools import ToolSurface as _ToolSurface
from envmaker.cli import new_run_id as _new_run_id
from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.definition import seal_definition as _seal_definition
from envmaker.core.program import EnvironmentProgram as _EnvironmentProgram
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.requirements import PromptRequirementSet as _PromptRequirementSet
from envmaker.runlog import RunLog as _RunLog
from envmaker.sdk import SDK_VERSION as _SDK_VERSION
from envmaker.validation import full_bundle as _full_bundle

__all__ = [
    "EvalRunResult",
    "load_eval_config",
    "run_variant_oneshot",
    "run_variant_loop",
    "run_evaluation",
    "write_report",
]

_REPO_ROOT = _Path(__file__).resolve().parents[2]
_ALLOWED_VARIANTS = frozenset({"oneshot", "loop"})
_SYSTEM_TERMINALS = frozenset({"provider_error", "harness_error"})
_Terminal = _Literal[
    "accepted",
    "rejected_after_budget",
    "provider_error",
    "harness_error",
]


@_dataclass(frozen=True, slots=True)
class EvalRunResult:
    """One evaluation cell outcome."""

    prompt_id: str
    variant: str
    seed: int
    terminal_state: str
    stages_passed: dict[str, bool]
    turns_used: int
    wall_seconds: float
    accepted: bool
    run_dir: str
    error: str | None


def load_eval_config(path: _Path | str) -> dict[str, _Any]:
    """Load and validate the evaluation YAML configuration."""

    payload = _yaml.safe_load(_Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("evaluation config must be a mapping")

    seeds_raw = payload.get("seeds", [7])
    if not isinstance(seeds_raw, list) or not seeds_raw:
        raise ValueError("seeds must be a non-empty list")
    try:
        seeds = [int(seed) for seed in seeds_raw]
    except (TypeError, ValueError) as exc:
        raise ValueError("seeds must be int-castable") from exc
    payload["seeds"] = seeds

    variants_raw = payload.get("variants", ["oneshot", "loop"])
    if not isinstance(variants_raw, list) or not variants_raw:
        raise ValueError("variants must be a non-empty list")
    variants = [str(variant) for variant in variants_raw]
    unknown = [variant for variant in variants if variant not in _ALLOWED_VARIANTS]
    if unknown:
        raise ValueError(
            f"variants must be subset of {sorted(_ALLOWED_VARIANTS)}; "
            f"got {unknown}"
        )
    payload["variants"] = variants

    prompts = payload.get("prompts", [])
    if not isinstance(prompts, list) or not prompts:
        raise ValueError("prompts must be a non-empty list")
    for index, entry in enumerate(prompts):
        if not isinstance(entry, dict):
            raise ValueError(
                f"prompt entry at index {index} must be a mapping, got {entry!r}"
            )
        prompt_id = entry.get("id")
        text = entry.get("text")
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError(
                f"prompt entry at index {index} requires nonempty str id"
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"prompt entry {prompt_id!r} requires nonempty str text"
            )
        checklist = entry.get("checklist")
        if not isinstance(checklist, list) or not (3 <= len(checklist) <= 5):
            raise ValueError(
                f"prompt entry {prompt_id!r} checklist must have 3–5 items"
            )
        if not all(isinstance(item, str) and item.strip() for item in checklist):
            raise ValueError(
                f"prompt entry {prompt_id!r} checklist items must be nonempty strings"
            )
    return payload


def _limits_from_config(config: dict[str, _Any]) -> _ResourceLimits:
    budgets = dict(config.get("budgets") or {})
    raw = dict(budgets.get("limits") or {})
    return _ResourceLimits(
        cpu_seconds=float(raw.get("cpu_seconds", 30.0)),
        memory_mb=int(raw.get("memory_mb", 512)),
        output_bytes=int(raw.get("output_bytes", 1_048_576)),
        wall_seconds=float(raw.get("wall_seconds", 60.0)),
    )


def _default_driver_factory(run_dir: _Path) -> object:
    from envmaker.runtime import RuntimeDriver

    driver = RuntimeDriver(
        run_dir=run_dir / "runtime",
        session_id="eval-" + _uuid.uuid4().hex[:12],
        windowed=True,
    )
    driver.start()
    return driver


def _ensure_probe(context: _ToolContext) -> None:
    """Delegate to the canonical landmark-first probe selection."""

    from envmaker.agent.loop import select_landmark_probe

    static = context.static
    if static is None or static.model is None or static.candidate is None:
        return
    context.probe = select_landmark_probe(static.model, static.candidate)


def _try_seal(context: _ToolContext, *, prompt: str, provider: _Provider) -> bool:
    static = context.static
    if static is None or static.model is None or static.candidate is None:
        return False
    if not static.reports or not all(report.passed for report in static.reports):
        return False
    if not context.runtime_reports or not all(
        report.passed for report in context.runtime_reports
    ):
        return False
    bundle = _full_bundle(static, context.runtime_reports)
    if not bundle.all_passed():
        return False
    requirements = _PromptRequirementSet(prompt=prompt, requirements=())
    descriptor = provider.descriptor
    if descriptor.prompt_version != _PROMPT_VERSION:
        descriptor = descriptor.model_copy(update={"prompt_version": _PROMPT_VERSION})
    program = _EnvironmentProgram(
        source=context.source,
        sdk_version=_SDK_VERSION,
        prompt_fingerprint=requirements.prompt_fingerprint,
        provider=descriptor,
    )
    _seal_definition(
        static.candidate,
        bundle,
        requirements=requirements,
        program=program,
        model=static.model,
        navmesh_fingerprint=_canonical_fingerprint(
            {"candidate": static.candidate.candidate_fingerprint}
        ),
    )
    return True


def _result(
    *,
    prompt_id: str,
    variant: str,
    seed: int,
    terminal_state: _Terminal,
    stages_passed: dict[str, bool],
    turns_used: int,
    wall_seconds: float,
    run_dir: _Path,
    error: str | None,
) -> EvalRunResult:
    return EvalRunResult(
        prompt_id=prompt_id,
        variant=variant,
        seed=seed,
        terminal_state=terminal_state,
        stages_passed=dict(stages_passed),
        turns_used=turns_used,
        wall_seconds=wall_seconds,
        accepted=terminal_state == "accepted",
        run_dir=str(run_dir),
        error=error,
    )


def run_variant_oneshot(
    prompt: str,
    *,
    provider: _Provider,
    seed: int,
    run_dir: _Path,
    limits: _ResourceLimits,
    driver_factory: _Callable[[_Path], object] | None = None,
    prompt_id: str = "prompt",
) -> EvalRunResult:
    """Single provider code turn, then harness-owned compile + simulate."""

    run_dir = _Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = _time.monotonic()
    factory = driver_factory or _default_driver_factory
    driver: object | None = None
    stages: dict[str, bool] = {}
    turns_used = 0

    try:
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(prompt, seed)},
        ]
        try:
            turn = provider.next_turn(messages)
        except _ProviderError as exc:
            return _result(
                prompt_id=prompt_id,
                variant="oneshot",
                seed=seed,
                terminal_state="provider_error",
                stages_passed=stages,
                turns_used=turns_used,
                wall_seconds=_time.monotonic() - started,
                run_dir=run_dir,
                error=str(exc),
            )
        turns_used = 1
        if turn.code is None:
            if turn.tool is not None:
                return _result(
                    prompt_id=prompt_id,
                    variant="oneshot",
                    seed=seed,
                    terminal_state="rejected_after_budget",
                    stages_passed=stages,
                    turns_used=turns_used,
                    wall_seconds=_time.monotonic() - started,
                    run_dir=run_dir,
                    error="oneshot contract violated: model called a tool",
                )
            return _result(
                prompt_id=prompt_id,
                variant="oneshot",
                seed=seed,
                terminal_state="provider_error",
                stages_passed=stages,
                turns_used=turns_used,
                wall_seconds=_time.monotonic() - started,
                run_dir=run_dir,
                error="oneshot contract requires a code turn",
            )

        context = _ToolContext(
            source=turn.code,
            limits=limits,
            run_dir=run_dir,
            runlog=_RunLog(run_dir / "runlog.jsonl"),
        )
        surface = _ToolSurface(context)
        compile_result = surface.compile_environment()
        stages.update(compile_result.stage_outcomes)
        if not compile_result.ok:
            return _result(
                prompt_id=prompt_id,
                variant="oneshot",
                seed=seed,
                terminal_state="rejected_after_budget",
                stages_passed=stages,
                turns_used=turns_used,
                wall_seconds=_time.monotonic() - started,
                run_dir=run_dir,
                error=compile_result.reason or "static validation failed",
            )

        _ensure_probe(context)
        if context.probe is None:
            return _result(
                prompt_id=prompt_id,
                variant="oneshot",
                seed=seed,
                terminal_state="rejected_after_budget",
                stages_passed=stages,
                turns_used=turns_used,
                wall_seconds=_time.monotonic() - started,
                run_dir=run_dir,
                error="no landmark probe available",
            )

        driver = factory(run_dir)
        context.driver = driver
        nav = surface.simulate_navigation()
        stages.update(nav.stage_outcomes)
        if not nav.ok:
            return _result(
                prompt_id=prompt_id,
                variant="oneshot",
                seed=seed,
                terminal_state="rejected_after_budget",
                stages_passed=stages,
                turns_used=turns_used,
                wall_seconds=_time.monotonic() - started,
                run_dir=run_dir,
                error=nav.reason or "runtime validation failed",
            )

        sealed = _try_seal(context, prompt=prompt, provider=provider)
        terminal: _Terminal = "accepted" if sealed else "rejected_after_budget"
        return _result(
            prompt_id=prompt_id,
            variant="oneshot",
            seed=seed,
            terminal_state=terminal,
            stages_passed=stages,
            turns_used=turns_used,
            wall_seconds=_time.monotonic() - started,
            run_dir=run_dir,
            error=None if sealed else "bundle did not seal",
        )
    except _ProviderError as exc:
        return _result(
            prompt_id=prompt_id,
            variant="oneshot",
            seed=seed,
            terminal_state="provider_error",
            stages_passed=stages,
            turns_used=turns_used,
            wall_seconds=_time.monotonic() - started,
            run_dir=run_dir,
            error=str(exc),
        )
    except Exception as exc:
        return _result(
            prompt_id=prompt_id,
            variant="oneshot",
            seed=seed,
            terminal_state="harness_error",
            stages_passed=stages,
            turns_used=turns_used,
            wall_seconds=_time.monotonic() - started,
            run_dir=run_dir,
            error=f"{exc}\n{_traceback.format_exc()[-2000:]}",
        )
    finally:
        if driver is not None and hasattr(driver, "close"):
            try:
                driver.close()
            except Exception:
                pass


def _map_loop_outcome(
    outcome: _AuthoringOutcome,
    *,
    prompt_id: str,
    seed: int,
    wall_seconds: float,
    stages_passed: dict[str, bool] | None = None,
) -> EvalRunResult:
    return _result(
        prompt_id=prompt_id,
        variant="loop",
        seed=seed,
        terminal_state=outcome.terminal_state,  # type: ignore[arg-type]
        stages_passed=stages_passed or {},
        turns_used=outcome.turns_used,
        wall_seconds=wall_seconds,
        run_dir=outcome.run_dir,
        error=outcome.failure_summary,
    )


def run_variant_loop(
    prompt: str,
    *,
    provider: _Provider,
    seed: int,
    run_dir: _Path,
    limits: _ResourceLimits,
    max_turns: int = 8,
    wall_seconds: float = 600.0,
    driver_factory: _Callable[[_Path], object] | None = None,
    prompt_id: str = "prompt",
) -> EvalRunResult:
    """Wrap ``run_authoring`` and map its outcome into ``EvalRunResult``."""

    started = _time.monotonic()
    try:
        outcome = _run_authoring(
            prompt,
            provider=provider,
            seed=seed,
            max_turns=max_turns,
            wall_seconds=wall_seconds,
            run_dir=_Path(run_dir),
            driver_factory=driver_factory,
            limits=limits,
        )
        stages = _stages_from_surface_dir(outcome.run_dir)
        return _map_loop_outcome(
            outcome,
            prompt_id=prompt_id,
            seed=seed,
            wall_seconds=_time.monotonic() - started,
            stages_passed=stages,
        )
    except Exception as exc:
        return _result(
            prompt_id=prompt_id,
            variant="loop",
            seed=seed,
            terminal_state="harness_error",
            stages_passed={},
            turns_used=0,
            wall_seconds=_time.monotonic() - started,
            run_dir=_Path(run_dir),
            error=f"{exc}\n{_traceback.format_exc()[-2000:]}",
        )


def _stages_from_surface_dir(run_dir: _Path) -> dict[str, bool]:
    """Best-effort stage map from runlog tool outcomes (optional detail)."""

    path = _Path(run_dir) / "runlog.jsonl"
    if not path.is_file():
        return {}
    stages: dict[str, bool] = {}
    for event in _RunLog(path).events():
        if event.get("kind") != "tool_call":
            continue
        payload = event.get("payload") or {}
        outcomes = payload.get("stage_outcomes") or {}
        if isinstance(outcomes, dict):
            for key, value in outcomes.items():
                stages[str(key)] = bool(value)
    return stages


def run_evaluation(
    config_path: _Path | str,
    out_report_path: _Path | str,
    *,
    provider_factory: _Callable[[str], _Provider] | None = None,
    driver_factory: _Callable[[_Path], object] | None = None,
    only: list[str] | None = None,
    runs_root: _Path | str | None = None,
) -> list[EvalRunResult]:
    """Iterate prompts × seeds × variants; never raise on a failed cell."""

    config = load_eval_config(config_path)
    model_name = str(config.get("model", "gpt-4o-mini"))
    budgets = dict(config.get("budgets") or {})
    max_turns = int(budgets.get("max_turns", 8))
    wall_seconds = float(budgets.get("wall_seconds", 600.0))
    limits = _limits_from_config(config)
    seeds = [int(s) for s in config["seeds"]]
    variants = [str(v) for v in config["variants"]]
    prompts = list(config.get("prompts") or [])
    if only is not None:
        allow = set(only)
        prompts = [p for p in prompts if str(p.get("id")) in allow]

    factory = provider_factory or (lambda name: _OpenAIProvider(model_name=name))
    base = _Path(runs_root) if runs_root is not None else _REPO_ROOT / "runs" / "eval"
    invocation_id = _new_run_id()
    root = base / invocation_id
    root.mkdir(parents=True, exist_ok=False)
    config = dict(config)
    config["eval_root"] = str(root)
    print(f"eval_root: {root}")

    results: list[EvalRunResult] = []
    try:
        for entry in prompts:
            prompt_id = str(entry["id"])
            text = str(entry["text"])
            for seed in seeds:
                for variant in variants:
                    cell_dir = root / f"{prompt_id}-{variant}-s{seed}"
                    cell_started = _time.monotonic()
                    try:
                        provider = factory(model_name)
                        if variant == "oneshot":
                            result = run_variant_oneshot(
                                text,
                                provider=provider,
                                seed=seed,
                                run_dir=cell_dir,
                                limits=limits,
                                driver_factory=driver_factory,
                                prompt_id=prompt_id,
                            )
                        elif variant == "loop":
                            result = run_variant_loop(
                                text,
                                provider=provider,
                                seed=seed,
                                run_dir=cell_dir,
                                limits=limits,
                                max_turns=max_turns,
                                wall_seconds=wall_seconds,
                                driver_factory=driver_factory,
                                prompt_id=prompt_id,
                            )
                        else:
                            result = _result(
                                prompt_id=prompt_id,
                                variant=variant,
                                seed=seed,
                                terminal_state="harness_error",
                                stages_passed={},
                                turns_used=0,
                                wall_seconds=_time.monotonic() - cell_started,
                                run_dir=cell_dir,
                                error=f"unknown variant: {variant}",
                            )
                    except Exception as exc:
                        result = _result(
                            prompt_id=prompt_id,
                            variant=variant,
                            seed=seed,
                            terminal_state="harness_error",
                            stages_passed={},
                            turns_used=0,
                            wall_seconds=_time.monotonic() - cell_started,
                            run_dir=cell_dir,
                            error=str(exc),
                        )
                    results.append(result)
    finally:
        if results:
            write_report(results, config, out_report_path)
    return results


def write_report(
    results: list[EvalRunResult],
    config: dict[str, _Any],
    path: _Path | str,
) -> None:
    """Write ``evals/mvp-report.md``-style aggregate markdown."""

    report_path = _Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    by_variant: dict[str, list[EvalRunResult]] = {}
    for result in results:
        by_variant.setdefault(result.variant, []).append(result)

    lines: list[str] = []
    lines.append("# EnvMaker MVP evaluation report")
    lines.append("")
    lines.append(f"- model: `{config.get('model', '')}`")
    lines.append(f"- prompt_version: `{config.get('prompt_version', '')}`")
    if config.get("eval_root"):
        lines.append(
            f"- eval_root: `{_repo_relative(str(config['eval_root']))}`"
        )
    lines.append(f"- cells: {len(results)}")
    lines.append("")
    lines.append("## Aggregate by variant")
    lines.append("")
    lines.append(
        "| variant | n | program execution rate | hard-valid rate | "
        "traversal success rate | mean repair turns | mean latency (s) |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")

    for variant, rows in sorted(by_variant.items()):
        n = len(rows)
        scored = [
            row for row in rows if row.terminal_state not in _SYSTEM_TERMINALS
        ]
        program_ok = sum(
            1 for row in rows if row.stages_passed.get("program", False)
        )
        hard_ok = sum(1 for row in rows if row.accepted)
        traversal_ok = sum(
            1
            for row in rows
            if row.stages_passed.get("controller", False)
            or row.terminal_state == "accepted"
        )
        if variant == "loop":
            loop_turns = [row.turns_used for row in scored]
            mean_repair = (
                (sum(loop_turns) / len(loop_turns)) if loop_turns else float("nan")
            )
        else:
            mean_repair = float("nan")
        mean_latency = (
            sum(row.wall_seconds for row in scored) / len(scored)
            if scored
            else float("nan")
        )
        lines.append(
            f"| {variant} | {n} | {_rate(program_ok, n)} | {_rate(hard_ok, n)} | "
            f"{_rate(traversal_ok, n)} | {_fmt(mean_repair)} | {_fmt(mean_latency)} |"
        )

    lines.append("")
    lines.append(
        "Means exclude provider_error/harness_error rows; turns count every "
        "provider turn including the initial code turn."
    )
    lines.append(
        "Loop-variant program execution is derived from observed compile events; "
        "a run that never compiled reports program=False."
    )
    lines.append("")
    lines.append("## Per-run outcomes")
    lines.append("")
    lines.append(
        "| prompt_id | variant | seed | terminal_state | accepted | "
        "turns_used | wall_seconds | error |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in results:
        err = (row.error or "").replace("|", "/").replace("\n", " ")[:80]
        lines.append(
            f"| {row.prompt_id} | {row.variant} | {row.seed} | "
            f"{row.terminal_state} | {row.accepted} | {row.turns_used} | "
            f"{_fmt(row.wall_seconds)} | {err} |"
        )

    lines.append("")
    lines.append("## Failed examples")
    lines.append("")
    failed = [
        row
        for row in results
        if not row.accepted and row.terminal_state not in _SYSTEM_TERMINALS
    ]
    system_errors = [
        row for row in results if row.terminal_state in _SYSTEM_TERMINALS
    ]
    if not failed:
        lines.append("_No hard-validation failures._")
    else:
        for row in failed:
            codes = [
                code
                for code, passed in row.stages_passed.items()
                if not passed
            ]
            lines.append(
                f"- `{row.prompt_id}` / `{row.variant}` / seed {row.seed}: "
                f"first failing stages `{', '.join(codes[:4]) or 'unknown'}` "
                f"(run_dir={_repo_relative(row.run_dir)})"
            )
    lines.append("")
    lines.append(
        "Provider/harness errors are reported separately from hard-validation "
        f"failures ({len(system_errors)} system-error cell(s) in this run)."
    )
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for limitation in _STANDING_LIMITATIONS:
        lines.append(f"- {limitation}")
    lines.append("")

    sanitized_config = dict(config)
    if "eval_root" in sanitized_config:
        sanitized_config["eval_root"] = _repo_relative(
            str(sanitized_config["eval_root"])
        )
    sidecar = report_path.with_suffix(".json")
    sidecar.write_text(
        _json.dumps(
            {
                "config": sanitized_config,
                "eval_root": sanitized_config.get("eval_root"),
                "results": [
                    {**_asdict(row), "run_dir": _repo_relative(row.run_dir)}
                    for row in results
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    report_path.write_text("\n".join(lines), encoding="utf-8")


_STANDING_LIMITATIONS: tuple[str, ...] = (
    "Sample size: six prompts, one seed, two variants; rates are directional "
    "evidence for the loop-vs-oneshot comparison, not statistics.",
    "Base model: the default is weak (`gpt-4o-mini`) and acceptance is "
    "sensitive to harness affordances (nudges); the frozen same-model "
    "comparison is what the claim rests on.",
    "Budgets: 8 provider turns / 600 s wall per run; runs one repair short "
    "of acceptance count as rejected, and budgets are never extended.",
    "Strict baseline: the one-shot variant gets the identical prompt but no "
    "tool execution and no feedback; the measured gap is the value of typed "
    "feedback, not prompt engineering.",
    "Loop stage attribution derives from observed compile events in the "
    "runlog; a run that never re-compiled reports its last known stages.",
    "Prompt compliance is human-scored via the YAML checklists, not a hard "
    "validator stage; runtime scope is a single walkable plane with "
    "box/cylinder primitives, verified on macOS arm64.",
)


def _repo_relative(value: str) -> str:
    """Render a path repo-relative so tracked reports never leak $HOME."""

    try:
        return str(_Path(value).resolve().relative_to(_REPO_ROOT))
    except Exception:
        return str(value)


def _rate(numer: int, denom: int) -> str:
    if denom <= 0:
        return "n/a"
    return f"{numer / denom:.2f}"


def _fmt(value: float) -> str:
    if value != value:  # NaN
        return "n/a"
    return f"{value:.2f}"
