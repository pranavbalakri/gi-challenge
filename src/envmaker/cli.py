"""EnvMaker command-line interface: demo, run, and check."""

from __future__ import annotations

import json as _json
import os as _os
import re as _re
import secrets as _secrets
import sys as _sys
import subprocess as _subprocess
import traceback as _traceback
import uuid as _uuid
from dataclasses import dataclass as _dataclass
from datetime import datetime as _datetime
from datetime import timezone as _timezone
import time as _time
from pathlib import Path as _Path
from typing import Any as _Any

import typer

from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.definition import HardStage as _HardStage
from envmaker.core.definition import seal_definition as _seal_definition
from envmaker.core.program import EnvironmentProgram as _EnvironmentProgram
from envmaker.core.program import ProviderInfo as _ProviderInfo
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.requirements import PromptRequirementSet as _PromptRequirementSet
from envmaker.godot_bridge.process import resolve_godot_binary as _resolve_godot_binary
from envmaker.sdk import SDK_VERSION as _SDK_VERSION
from envmaker.validation import full_bundle as _full_bundle
from envmaker.validation import validate_candidate as _validate_candidate
from envmaker.validation import validate_static as _validate_static

app = typer.Typer(name="envmaker", no_args_is_help=True, add_completion=False)

_REPO_ROOT = _Path(__file__).resolve().parents[2]
_RUNS_ROOT = _REPO_ROOT / "runs"
_DEMO_SOURCE = _REPO_ROOT / "examples" / "demo" / "environment.py"
_VIEW_WINDOW_ARGS = ("--maximized",)
_ZERO_FAILURES = _re.compile(r"(?<!\d)0 failures")
_STAGE_ORDER = tuple(
    stage.value
    for stage in (
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
)
_DEFAULT_LIMITS = _ResourceLimits(
    cpu_seconds=30.0,
    memory_mb=512,
    output_bytes=1_048_576,
    wall_seconds=60.0,
)


@app.callback()
def _root() -> None:
    """EnvMaker: text-to-environment agent harness."""


@app.command()
def version() -> None:
    """Print the EnvMaker version."""
    from envmaker import __version__

    typer.echo(__version__)


def new_run_id() -> str:
    """Return ``<utc yyyymmdd-hhmmss>-<8 hex>``."""

    stamp = _datetime.now(_timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{_secrets.token_hex(4)}"


def allocate_run_dir(parent: _Path) -> _Path:
    """Create ``parent/<new_run_id()>``; regenerate once on collision."""

    parent.mkdir(parents=True, exist_ok=True)
    last_error: FileExistsError | None = None
    for _ in range(2):
        candidate = parent / new_run_id()
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError as exc:
            last_error = exc
    raise FileExistsError(
        f"could not allocate a unique run directory under {parent}"
    ) from last_error


@_dataclass(frozen=True, slots=True)
class DemoOutcome:
    """Result of the keyless demo pipeline."""

    ok: bool
    run_dir: _Path
    stages: dict[str, bool]
    renders: dict[str, str]
    bundle_sealed: bool
    error: str | None = None


def run_demo_pipeline(
    *,
    view: bool = False,
    run_dir: _Path | None = None,
    source: str | None = None,
    hold_seconds: float | None = None,
) -> DemoOutcome:
    """Keyless demo: static + runtime validators, navigate, render, seal."""

    from envmaker.runtime import RuntimeDriver

    if run_dir is not None:
        target = _Path(run_dir)
        target.mkdir(parents=True, exist_ok=True)
    else:
        target = allocate_run_dir(_RUNS_ROOT)
    program_source = (
        source
        if source is not None
        else _DEMO_SOURCE.read_text(encoding="utf-8")
    )
    (target / "environment.py").write_text(program_source, encoding="utf-8")

    stages: dict[str, bool] = {name: False for name in _STAGE_ORDER}
    renders: dict[str, str] = {}
    driver = None
    error: str | None = None
    bundle_sealed = False

    try:
        static = _validate_static(program_source, limits=_DEFAULT_LIMITS)
        for report in static.reports:
            stages[report.stage.value] = report.passed

        if (
            static.model is None
            or static.candidate is None
            or not all(report.passed for report in static.reports)
        ):
            error = "static validation failed"
            _write_demo_artifacts(target, stages, renders, bundle_sealed, error)
            return DemoOutcome(
                ok=False,
                run_dir=target,
                stages=stages,
                renders=renders,
                bundle_sealed=False,
                error=error,
            )

        from envmaker.agent.loop import select_landmark_probe

        probe = select_landmark_probe(static.model, static.candidate)
        if probe is None:
            error = "demo fixture has no landmark probe target"
            _write_demo_artifacts(target, stages, renders, bundle_sealed, error)
            return DemoOutcome(
                ok=False,
                run_dir=target,
                stages=stages,
                renders=renders,
                bundle_sealed=False,
                error=error,
            )

        driver = RuntimeDriver(
            run_dir=target / "runtime",
            session_id="demo-" + _uuid.uuid4().hex[:12],
            windowed=True,
            window_args=_VIEW_WINDOW_ARGS if view else None,
        )
        driver.start()
        runtime_reports = _validate_candidate(
            static.model,
            static.candidate,
            driver,
            probe=probe,
        )
        for report in runtime_reports:
            stages[report.stage.value] = report.passed

        # Camera stage already captured isometric; also keep a top-down path.
        try:
            iso = driver.render("isometric")
            renders["isometric"] = str(getattr(iso, "path", ""))
        except Exception:
            pass
        try:
            top = driver.render("topdown")
            renders["topdown"] = str(getattr(top, "path", ""))
        except Exception:
            pass

        if view:
            # End on the isometric frame (not the top-down capture flash),
            # then keep the world open with the agent wandering goallessly:
            # for N seconds when --hold N was given, else until the window is
            # closed or Ctrl-C.
            try:
                driver.render("isometric")
            except Exception:
                pass
            if hold_seconds is not None:
                if hold_seconds > 0:
                    _time.sleep(hold_seconds)
            else:
                typer.echo(
                    "environment open — close the window or press Ctrl-C "
                    "to exit"
                )
                try:
                    while getattr(driver, "is_running", lambda: False)():
                        _time.sleep(0.5)
                except KeyboardInterrupt:
                    pass

        bundle = _full_bundle(static, runtime_reports)
        if bundle.all_passed():
            requirements = _PromptRequirementSet(
                prompt="checked-in village-green demo fixture",
                requirements=(),
            )
            program = _EnvironmentProgram(
                source=program_source,
                sdk_version=_SDK_VERSION,
                prompt_fingerprint=requirements.prompt_fingerprint,
                provider=_ProviderInfo(
                    provider="fixture",
                    model_name="demo",
                    prompt_version="1",
                ),
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
            bundle_sealed = True
        else:
            error = "hard validators did not all pass"

        _write_demo_artifacts(target, stages, renders, bundle_sealed, error)
        ok = all(stages.values()) and bundle_sealed
        return DemoOutcome(
            ok=ok,
            run_dir=target,
            stages=stages,
            renders=renders,
            bundle_sealed=bundle_sealed,
            error=error,
        )
    except Exception as exc:
        error = f"{exc}\n{_traceback.format_exc()[-2000:]}"
        _write_demo_artifacts(target, stages, renders, False, error)
        return DemoOutcome(
            ok=False,
            run_dir=target,
            stages=stages,
            renders=renders,
            bundle_sealed=False,
            error=error,
        )
    finally:
        if driver is not None and hasattr(driver, "close"):
            try:
                driver.close()
            except Exception:
                pass


def _write_demo_artifacts(
    run_dir: _Path,
    stages: dict[str, bool],
    renders: dict[str, str],
    bundle_sealed: bool,
    error: str | None,
) -> None:
    report = {
        "stages": stages,
        "renders": renders,
        "bundle_sealed": bundle_sealed,
        "error": error,
        "all_passed": all(stages.values()) and bundle_sealed,
    }
    (run_dir / "report.json").write_text(
        _json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = ["# Demo summary", ""]
    for name in _STAGE_ORDER:
        status = "PASS" if stages.get(name) else "FAIL"
        lines.append(f"{status} {name}")
    lines.append("")
    lines.append(f"bundle_sealed: {bundle_sealed}")
    if renders:
        lines.append("renders:")
        for view, path in sorted(renders.items()):
            lines.append(f"  - {view}: {path}")
    if error:
        lines.append("")
        lines.append(f"error: {error}")
    lines.append("")
    (run_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def _print_demo_outcome(outcome: DemoOutcome) -> None:
    for name in _STAGE_ORDER:
        status = "PASS" if outcome.stages.get(name) else "FAIL"
        typer.echo(f"{status} {name}")
    for view, path in sorted(outcome.renders.items()):
        typer.echo(f"render {view}: {path}")
    typer.echo(f"run_dir: {outcome.run_dir}")


_DIAGNOSIS_FAMILIES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "spawn placement",
        (
            "spawn must lie on the ground",
            "spawn intersects a blocker",
            "v5.spawn_intersects_blocker",
        ),
        "every attempt died on spawn placement. The rules require the spawn "
        "to sit ON the ground with 0.4 m clearance and outside every "
        "blocker; a prompt like \"spawn outside X\" only works if the ground "
        "extends beyond X. Rephrase to keep the spawn on land or ask for a "
        "bigger ground.",
    ),
    (
        "materials",
        ("unknown material",),
        "the prompt keeps requesting materials outside the curated set. "
        "Available: grass, dirt, stone, rock, wood, water, snow, default.",
    ),
    (
        "kits",
        ("unknown kit", "kit category"),
        "the prompt keeps requesting assets outside the curated kits. "
        "Available: stone_ruin, timber_hut, watchtower (structures); "
        "obelisk, banner (landmarks); pine, shrub (vegetation).",
    ),
    (
        "ground shape",
        ("axis-aligned rectangle",),
        "the prompt requires a non-rectangular ground; the current rules "
        "materialize exactly one axis-aligned rectangular ground.",
    ),
    (
        "connectivity",
        ("v6.clear_ground_fraction",),
        "the requested layout seals off most of the walkable ground; keep "
        "an open corridor so the map stays connected.",
    ),
)


def diagnose_failed_attempts(run_dirs: list[_Path]) -> str:
    """Heuristic post-mortem across preserved runlogs.

    A single constraint family dominating every attempt is the fingerprint of
    a structurally unworkable prompt; scattered one-off failures are model
    noise. This inspects only typed signals — no semantic judging.
    """

    family_hits: dict[str, int] = {}
    total_hits = 0
    for run_dir in run_dirs:
        runlog = _Path(run_dir) / "runlog.jsonl"
        if not runlog.is_file():
            continue
        for line in runlog.read_text(encoding="utf-8").splitlines():
            try:
                event = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if event.get("kind") != "tool_call":
                continue
            payload = event.get("payload", {})
            haystack = " ".join(
                (
                    " ".join(str(c) for c in payload.get("signal_codes") or ()),
                    str(payload.get("signal_messages", "")),
                )
            )
            if not haystack.strip():
                continue
            for family, needles, _advice in _DIAGNOSIS_FAMILIES:
                if any(needle in haystack for needle in needles):
                    family_hits[family] = family_hits.get(family, 0) + 1
                    total_hits += 1
                    break

    if total_hits >= 2:
        family, hits = max(family_hits.items(), key=lambda item: item[1])
        if hits >= 2 and hits * 10 >= total_hits * 6:
            advice = next(
                advice
                for name, _needles, advice in _DIAGNOSIS_FAMILIES
                if name == family
            )
            return (
                f"structural ({family}, {hits} occurrences across "
                f"{len(run_dirs)} attempt(s)): {advice}"
            )
    return (
        "no single structural blocker detected: the model failed to "
        "converge within budget. Retry with more --attempts, a larger "
        "--max-turns, or a stronger --model."
    )


def present_environment(final_source: str, run_dir: _Path) -> None:
    """Open an accepted environment fullscreen with a goallessly wandering
    agent, until the window is closed or Ctrl-C."""

    from envmaker.runtime import RuntimeDriver

    static = _validate_static(final_source, limits=_DEFAULT_LIMITS)
    if static.candidate is None:
        typer.echo("presentation skipped: final source no longer compiles")
        return

    previous_wander = _os.environ.get("ENVMAKER_WANDER")
    _os.environ["ENVMAKER_WANDER"] = "1"
    driver = None
    try:
        driver = RuntimeDriver(
            run_dir=_Path(run_dir) / "present",
            session_id="present-" + _uuid.uuid4().hex[:12],
            windowed=True,
            window_args=_VIEW_WINDOW_ARGS,
        )
        driver.start()
        driver.load_candidate(static.candidate)
        driver.wait_navigation_ready(30.0)
        typer.echo(
            "environment open — close the window or press Ctrl-C to exit"
        )
        while driver.is_running():
            _time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        typer.echo(f"presentation failed: {exc}")
    finally:
        if previous_wander is None:
            _os.environ.pop("ENVMAKER_WANDER", None)
        else:
            _os.environ["ENVMAKER_WANDER"] = previous_wander
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass


def _format_event_line(kind: str, payload: dict[str, _Any]) -> str:
    if kind == "signals":
        codes = payload.get("signal_codes") or []
        if codes:
            return f"signals codes={','.join(str(c) for c in codes)}"
        text = str(payload.get("text", ""))[:120]
        return f"signals text={text}" if text else "signals"
    if kind == "provider_turn":
        return (
            f"provider_turn turn={payload.get('turn')} "
            f"has_code={payload.get('has_code')} tool={payload.get('tool')}"
        )
    if kind == "tool_call":
        codes = payload.get("signal_codes") or []
        code_part = f" codes={','.join(str(c) for c in codes)}" if codes else ""
        message_part = ""
        messages_text = str(payload.get("signal_messages", "")).strip()
        if codes and messages_text:
            # The last non-empty line of a traceback-bearing message is the
            # exception itself; the first is just the frame header.
            lines = [l for l in messages_text.splitlines() if l.strip()]
            display = lines[-1] if len(lines) > 1 else lines[0]
            message_part = f" | {display.strip()[:160]}"
        return (
            f"tool_call {payload.get('name')} "
            f"ok={payload.get('ok')}{code_part}{message_part}"
        )
    if kind == "outcome":
        return f"outcome {payload.get('terminal_state')}"
    if kind == "revision":
        return f"revision index={payload.get('index')}"
    if kind == "nudge":
        return f"nudge {payload.get('reason')}"
    return f"{kind}"


@app.command()
def demo(
    headless: bool = typer.Option(
        False,
        "--headless",
        help="No-interaction demo (off-screen window for renders).",
    ),
    view: bool = typer.Option(
        False,
        "--view",
        help="Visible window; replay automated traversal on screen.",
    ),
    hold: float | None = typer.Option(
        None,
        "--hold",
        help=(
            "Seconds to keep the --view window open after validation "
            "(default: stay open until Enter is pressed)."
        ),
    ),
) -> None:
    """Run the checked-in village-green fixture through all hard validators."""

    if headless and view:
        raise typer.BadParameter("use only one of --headless or --view")
    use_view = bool(view)
    previous_wander = _os.environ.get("ENVMAKER_WANDER")
    if use_view:
        # The agent wanders the navmesh while the window stays open.
        _os.environ["ENVMAKER_WANDER"] = "1"
    try:
        outcome = run_demo_pipeline(view=use_view, hold_seconds=hold)
    finally:
        if use_view:
            if previous_wander is None:
                _os.environ.pop("ENVMAKER_WANDER", None)
            else:
                _os.environ["ENVMAKER_WANDER"] = previous_wander
    _print_demo_outcome(outcome)
    raise typer.Exit(0 if outcome.ok else 1)


@app.command()
def run(
    prompt: str = typer.Argument(..., help="Natural-language environment prompt."),
    seed: int = typer.Option(7, "--seed", help="Deterministic authoring seed."),
    max_turns: int = typer.Option(12, "--max-turns", help="Provider turn budget."),
    wall_seconds: float = typer.Option(
        600.0, "--wall-seconds", help="Wall-clock budget seconds."
    ),
    model: str = typer.Option(
        "gpt-4o-mini", "--model", help="OpenAI chat model name."
    ),
    attempts: int = typer.Option(
        2,
        "--attempts",
        min=1,
        help="Fresh authoring attempts before giving up (retries budget "
        "exhaustion and provider errors; never harness errors).",
    ),
    open_view: bool | None = typer.Option(
        None,
        "--open/--no-open",
        help="Open the accepted environment fullscreen with a wandering "
        "agent (default: open when the terminal is interactive).",
    ),
) -> None:
    """Live authoring loop with streamed runlog events."""

    from envmaker.agent.loop import run_authoring
    from envmaker.agent.providers import OpenAIProvider

    try:

        def _on_event(kind: str, payload: dict) -> None:
            typer.echo(_format_event_line(kind, payload))

        try:
            provider_probe = OpenAIProvider(model_name=model)
        except Exception as exc:
            typer.echo(f"provider_error: {exc}")
            raise typer.Exit(1) from exc

        outcome = None
        attempt_dirs: list[_Path] = []
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                typer.echo(f"attempt {attempt}/{attempts} (fresh context)")
                provider_probe = OpenAIProvider(model_name=model)
            run_dir = allocate_run_dir(_RUNS_ROOT)
            attempt_dirs.append(run_dir)
            typer.echo(f"run_dir: {run_dir}")
            outcome = run_authoring(
                prompt,
                provider=provider_probe,
                seed=seed,
                max_turns=max_turns,
                wall_seconds=wall_seconds,
                run_dir=run_dir,
                on_event=_on_event,
            )
            typer.echo(f"terminal_state: {outcome.terminal_state}")
            typer.echo(f"run_dir: {outcome.run_dir}")
            if outcome.terminal_state in {"accepted", "harness_error"}:
                break

        assert outcome is not None
        if outcome.terminal_state != "accepted":
            typer.echo(
                f"prompt_diagnosis: {diagnose_failed_attempts(attempt_dirs)}"
            )
        elif outcome.final_source:
            should_open = (
                open_view
                if open_view is not None
                else bool(_sys.stdout is not None and _sys.stdout.isatty())
            )
            if should_open:
                present_environment(outcome.final_source, attempt_dirs[-1])
        if outcome.terminal_state in {"accepted", "rejected_after_budget"}:
            raise typer.Exit(0)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


@app.command("check")
def check_cmd() -> None:
    """Fast keyless contract suite (pytest, Godot harness, demo)."""

    try:
        lines: list[tuple[str, bool]] = []
        messages: list[str] = []

        pytest_cmd = ["uv", "run", "pytest", "tests", "--ignore=tests/cli", "-q"]
        try:
            pytest_proc = _subprocess.run(
                pytest_cmd,
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
            )
            pytest_ok = pytest_proc.returncode == 0
        except OSError as exc:
            pytest_ok = False
            messages.append(f"pytest: {exc}")
        lines.append(("pytest", pytest_ok))

        godot_bin = _resolve_godot_binary()
        godot_cmd = [
            str(godot_bin),
            "--headless",
            "--path",
            "godot",
            "--script",
            "tests/run_all.gd",
        ]
        try:
            godot_proc = _subprocess.run(
                godot_cmd,
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
            )
            godot_output = (godot_proc.stdout or "") + (godot_proc.stderr or "")
            godot_ok = (
                godot_proc.returncode == 0
                and _ZERO_FAILURES.search(godot_output) is not None
            )
        except OSError as exc:
            godot_ok = False
            messages.append(f"godot: {exc}")
        lines.append(("godot", godot_ok))

        demo_outcome = run_demo_pipeline(view=False)
        lines.append(("demo", demo_outcome.ok))

        for name, ok in lines:
            typer.echo(f"{'PASS' if ok else 'FAIL'} {name}")
        for message in messages:
            typer.echo(message)
        typer.echo(f"run_dir: {demo_outcome.run_dir}")
        raise typer.Exit(0 if all(ok for _, ok in lines) else 1)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


def main() -> None:
    app()
