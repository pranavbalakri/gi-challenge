"""EnvMaker command-line interface: demo, run, and check."""

from __future__ import annotations

import json as _json
import re as _re
import secrets as _secrets
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
_VIEW_WINDOW_ARGS = ("--resolution", "1280x720", "--position", "100,100")
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
    hold_seconds: float = 8.0,
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

        if view and hold_seconds > 0:
            # End on the isometric frame (not the top-down capture flash) and
            # keep the window up so the traversal result can actually be seen.
            try:
                driver.render("isometric")
            except Exception:
                pass
            _time.sleep(hold_seconds)

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
        return (
            f"tool_call {payload.get('name')} ok={payload.get('ok')}{code_part}"
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
    hold: float = typer.Option(
        8.0,
        "--hold",
        help="Seconds to keep the --view window open after validation.",
    ),
) -> None:
    """Run the checked-in village-green fixture through all hard validators."""

    if headless and view:
        raise typer.BadParameter("use only one of --headless or --view")
    use_view = bool(view)
    outcome = run_demo_pipeline(view=use_view, hold_seconds=hold)
    _print_demo_outcome(outcome)
    raise typer.Exit(0 if outcome.ok else 1)


@app.command()
def run(
    prompt: str = typer.Argument(..., help="Natural-language environment prompt."),
    seed: int = typer.Option(7, "--seed", help="Deterministic authoring seed."),
    max_turns: int = typer.Option(8, "--max-turns", help="Provider turn budget."),
    wall_seconds: float = typer.Option(
        600.0, "--wall-seconds", help="Wall-clock budget seconds."
    ),
    model: str = typer.Option(
        "gpt-4o-mini", "--model", help="OpenAI chat model name."
    ),
) -> None:
    """Live authoring loop with streamed runlog events."""

    from envmaker.agent.loop import run_authoring
    from envmaker.agent.providers import OpenAIProvider

    try:
        run_dir = allocate_run_dir(_RUNS_ROOT)
        typer.echo(f"run_dir: {run_dir}")

        def _on_event(kind: str, payload: dict) -> None:
            typer.echo(_format_event_line(kind, payload))

        try:
            provider = OpenAIProvider(model_name=model)
        except Exception as exc:
            typer.echo(f"provider_error: {exc}")
            raise typer.Exit(1) from exc

        outcome = run_authoring(
            prompt,
            provider=provider,
            seed=seed,
            max_turns=max_turns,
            wall_seconds=wall_seconds,
            run_dir=run_dir,
            on_event=_on_event,
        )
        typer.echo(f"terminal_state: {outcome.terminal_state}")
        typer.echo(f"run_dir: {outcome.run_dir}")
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
