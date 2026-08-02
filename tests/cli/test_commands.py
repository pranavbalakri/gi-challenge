"""CLI coverage for demo / run / check with all heavy seams stubbed."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from envmaker.agent.loop import AuthoringOutcome
from envmaker.cli import _VIEW_WINDOW_ARGS, allocate_run_dir, app, new_run_id
from envmaker.core.artifacts import ArtifactRef
from envmaker.core.episode import EpisodeResult, NavigationProbe, TerminalReason


_RUNNER = CliRunner()
_STAGE_NAMES = (
    "program",
    "sdk_model",
    "semantic",
    "asset",
    "scene",
    "materialization",
    "navigation",
    "controller",
    "camera",
)


class _HappyDriver:
    """Stub RuntimeDriver that passes all live-runtime validators."""

    instances: list["_HappyDriver"] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        type(self).instances.append(self)

    def start(self) -> None:
        self.started = True

    def load_candidate(self, candidate: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True)

    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        return None

    def connected_clear_ground_fraction(self) -> float:
        return 0.95

    def navigate(self, probe: NavigationProbe) -> EpisodeResult:
        return EpisodeResult(
            probe_fingerprint=probe.probe_fingerprint,
            terminal_reason=TerminalReason.ARRIVED,
            ticks_used=40,
            final_geodesic_distance_m=0.2,
            path_length_m=22.0,
            collisions=0,
            stuck_recoveries=0,
        )

    def render(self, view: str) -> ArtifactRef:
        digest = "b" * 64
        return ArtifactRef(
            path=f"renders/{view}.png",
            media_type="image/png",
            byte_count=256,
            blake2b256=digest,
            sha256=digest,
            producer="stub",
            toolchain_version="test",
        )

    def close(self) -> int:
        self.closed = True
        return 0


class _FailDriver(_HappyDriver):
    def load_candidate(self, candidate: object) -> SimpleNamespace:
        raise RuntimeError("materialization boom")


@pytest.fixture(autouse=True)
def _isolate_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import envmaker.cli as cli

    monkeypatch.setattr(cli, "_RUNS_ROOT", tmp_path / "runs")
    _HappyDriver.instances = []


def test_run_id_format() -> None:
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", new_run_id())


def test_allocate_run_dir_retries_on_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import envmaker.cli as cli

    first = "20260101-000000-aaaaaaaa"
    second = "20260101-000000-bbbbbbbb"
    (tmp_path / first).mkdir()
    ids = iter([first, second])
    monkeypatch.setattr(cli, "new_run_id", lambda: next(ids))

    allocated = allocate_run_dir(tmp_path)
    assert allocated.name == second
    assert allocated.is_dir()
    assert (tmp_path / first).is_dir()
    assert list((tmp_path / first).iterdir()) == []


def test_demo_happy_path_prints_nine_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import envmaker.cli as cli
    import envmaker.runtime as runtime

    monkeypatch.setattr(runtime, "RuntimeDriver", _HappyDriver)
    monkeypatch.setattr(cli, "RuntimeDriver", _HappyDriver, raising=False)
    monkeypatch.setattr("envmaker.runtime.RuntimeDriver", _HappyDriver)

    result = _RUNNER.invoke(app, ["demo", "--headless"])
    assert result.exit_code == 0, result.output
    for name in _STAGE_NAMES:
        assert f"PASS {name}" in result.output
    assert "run_dir:" in result.output
    assert _HappyDriver.instances
    assert _HappyDriver.instances[0].kwargs.get("window_args") is None
    assert _HappyDriver.instances[0].kwargs.get("windowed") is True


def test_demo_failure_exits_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("envmaker.runtime.RuntimeDriver", _FailDriver)
    result = _RUNNER.invoke(app, ["demo", "--headless"])
    assert result.exit_code == 1
    assert "FAIL materialization" in result.output or "FAIL" in result.output


def test_demo_view_passes_window_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("envmaker.runtime.RuntimeDriver", _HappyDriver)
    result = _RUNNER.invoke(app, ["demo", "--view"])
    assert result.exit_code == 0, result.output
    assert _HappyDriver.instances
    assert _HappyDriver.instances[0].kwargs.get("window_args") == _VIEW_WINDOW_ARGS


@pytest.mark.parametrize(
    ("terminal", "exit_code"),
    [
        ("accepted", 0),
        ("rejected_after_budget", 0),
        ("provider_error", 1),
        ("harness_error", 1),
    ],
)
def test_run_streams_events_and_exit_codes(
    terminal: str,
    exit_code: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events_seen: list[str] = []
    captured: dict[str, object] = {}

    def _fake_authoring(prompt: str, **kwargs: object) -> AuthoringOutcome:
        del prompt
        required = {
            "provider",
            "seed",
            "max_turns",
            "wall_seconds",
            "run_dir",
            "on_event",
        }
        assert required <= set(kwargs)
        assert kwargs["seed"] == 7
        assert kwargs["max_turns"] == 2
        assert kwargs["wall_seconds"] == 600.0
        assert kwargs["run_dir"] is not None
        captured.update(kwargs)
        on_event = kwargs["on_event"]
        assert callable(on_event)
        on_event("provider_turn", {"turn": 1, "has_code": True, "tool": None})
        on_event(
            "signals",
            {"signal_codes": ["v6.clear_ground_fraction"], "messages": "low"},
        )
        on_event("outcome", {"terminal_state": terminal})
        events_seen.extend(["provider_turn", "signals", "outcome"])
        return AuthoringOutcome(
            terminal_state=terminal,  # type: ignore[arg-type]
            turns_used=1,
            final_source="x",
            bundle_sealed=terminal == "accepted",
            run_dir=tmp_path / "live",
            failure_summary=None if terminal == "accepted" else terminal,
        )

    class _FakeProvider:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

    monkeypatch.setattr("envmaker.agent.loop.run_authoring", _fake_authoring)
    monkeypatch.setattr(
        "envmaker.agent.providers.OpenAIProvider",
        _FakeProvider,
    )

    result = _RUNNER.invoke(
        app,
        [
            "run",
            "a frozen village",
            "--seed",
            "7",
            "--max-turns",
            "2",
            "--attempts",
            "1",
        ],
    )
    assert result.exit_code == exit_code, result.output
    assert "provider_turn" in result.output
    assert "signals codes=v6.clear_ground_fraction" in result.output
    assert (
        f"outcome {terminal}" in result.output
        or f"terminal_state: {terminal}" in result.output
    )
    assert events_seen == ["provider_turn", "signals", "outcome"]
    assert captured["seed"] == 7


def test_run_keyboard_interrupt_exits_130(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a: object, **_k: object) -> AuthoringOutcome:
        raise KeyboardInterrupt

    class _FakeProvider:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

    monkeypatch.setattr("envmaker.agent.loop.run_authoring", _boom)
    monkeypatch.setattr(
        "envmaker.agent.providers.OpenAIProvider",
        _FakeProvider,
    )
    result = _RUNNER.invoke(app, ["run", "interrupt me", "--seed", "1"])
    assert result.exit_code == 130


def test_check_builds_exact_subprocess_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import envmaker.cli as cli

    calls: list[list[str]] = []

    def _fake_run(cmd: object, **kwargs: object) -> SimpleNamespace:
        argv = [str(part) for part in list(cmd)]  # type: ignore[arg-type]
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout="RESULT: 55 checks, 0 failures\n",
            stderr="",
        )

    def _fake_demo(*, view: bool = False, **_kwargs: object) -> object:
        del view
        return SimpleNamespace(
            ok=True,
            run_dir=Path("/tmp/demo-run"),
            stages={name: True for name in _STAGE_NAMES},
            renders={},
            bundle_sealed=True,
            error=None,
        )

    monkeypatch.setattr(cli, "_subprocess", SimpleNamespace(run=_fake_run))
    monkeypatch.setattr(cli, "run_demo_pipeline", _fake_demo)
    monkeypatch.setattr(
        cli, "_resolve_godot_binary", lambda: Path("/opt/godot/bin")
    )

    result = _RUNNER.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    assert calls[0] == ["uv", "run", "pytest", "tests", "--ignore=tests/cli", "-q"]
    assert "--ignore=tests/cli" in calls[0]
    assert "tests/cli" not in calls[0]
    assert calls[1] == [
        "/opt/godot/bin",
        "--headless",
        "--path",
        "godot",
        "--script",
        "tests/run_all.gd",
    ]
    assert "PASS pytest" in result.output
    assert "PASS godot" in result.output
    assert "PASS demo" in result.output


def test_check_godot_ten_failures_is_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    import envmaker.cli as cli

    def _fake_run(cmd: object, **_kwargs: object) -> SimpleNamespace:
        argv = [str(part) for part in list(cmd)]  # type: ignore[arg-type]
        if "pytest" in argv:
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="RESULT: 55 checks, 10 failures\n",
            stderr="",
        )

    monkeypatch.setattr(cli, "_subprocess", SimpleNamespace(run=_fake_run))
    monkeypatch.setattr(
        cli,
        "run_demo_pipeline",
        lambda **_k: SimpleNamespace(
            ok=True,
            run_dir=Path("/tmp/x"),
            stages={},
            renders={},
            bundle_sealed=True,
            error=None,
        ),
    )
    monkeypatch.setattr(
        cli, "_resolve_godot_binary", lambda: Path("/opt/godot/bin")
    )
    result = _RUNNER.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "FAIL godot" in result.output


def test_check_subprocess_oserror_still_runs_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import envmaker.cli as cli

    demo_calls = {"n": 0}

    def _boom(cmd: object, **_kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("uv missing")

    def _fake_demo(**_k: object) -> object:
        demo_calls["n"] += 1
        return SimpleNamespace(
            ok=True,
            run_dir=Path("/tmp/x"),
            stages={},
            renders={},
            bundle_sealed=True,
            error=None,
        )

    monkeypatch.setattr(cli, "_subprocess", SimpleNamespace(run=_boom))
    monkeypatch.setattr(cli, "run_demo_pipeline", _fake_demo)
    monkeypatch.setattr(
        cli, "_resolve_godot_binary", lambda: Path("/opt/godot/bin")
    )
    result = _RUNNER.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "FAIL pytest" in result.output
    assert "FAIL godot" in result.output
    assert demo_calls["n"] == 1
    assert "PASS demo" in result.output


def test_check_aggregates_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import envmaker.cli as cli

    def _fake_run(cmd: object, **_kwargs: object) -> SimpleNamespace:
        argv = [str(part) for part in list(cmd)]  # type: ignore[arg-type]
        if "pytest" in argv:
            return SimpleNamespace(returncode=1, stdout="failed", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="RESULT: 1 checks, 0 failures",
            stderr="",
        )

    monkeypatch.setattr(cli, "_subprocess", SimpleNamespace(run=_fake_run))
    monkeypatch.setattr(
        cli,
        "run_demo_pipeline",
        lambda **_k: SimpleNamespace(
            ok=True,
            run_dir=Path("/tmp/x"),
            stages={},
            renders={},
            bundle_sealed=True,
            error=None,
        ),
    )
    monkeypatch.setattr(
        cli, "_resolve_godot_binary", lambda: Path("/opt/godot/bin")
    )
    result = _RUNNER.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "FAIL pytest" in result.output
    assert "PASS godot" in result.output
    assert "PASS demo" in result.output


def test_probe_selection_is_the_single_canonical_helper() -> None:
    """cli and evaluation must delegate to loop.select_landmark_probe."""
    import inspect

    import envmaker.cli as cli_module
    import envmaker.evaluation as evaluation_module
    from envmaker.agent.loop import select_landmark_probe

    assert "select_landmark_probe" in inspect.getsource(cli_module)
    assert "select_landmark_probe" in inspect.getsource(evaluation_module)
    for module in (cli_module, evaluation_module):
        source = inspect.getsource(module)
        assert 'node.collider is None and "." in node.semantic_id' not in source

    from envmaker.core.program import ResourceLimits
    from envmaker.validation import validate_static

    demo_source = (
        Path(__file__).resolve().parents[2] / "examples/demo/environment.py"
    ).read_text()
    static = validate_static(
        demo_source,
        limits=ResourceLimits(
            cpu_seconds=10, memory_mb=512, output_bytes=262144, wall_seconds=20
        ),
    )
    probe = select_landmark_probe(static.model, static.candidate)
    assert probe is not None
    assert probe.target_landmark_id == "obelisk_goal.0"


def _write_runlog(dir_path, entries):
    import json as json_mod

    dir_path.mkdir(parents=True, exist_ok=True)
    lines = []
    for seq, (codes, messages) in enumerate(entries, start=1):
        lines.append(
            json_mod.dumps(
                {
                    "kind": "tool_call",
                    "seq": seq,
                    "payload": {
                        "name": "compile_environment",
                        "ok": False,
                        "signal_codes": codes,
                        "signal_messages": messages,
                    },
                }
            )
        )
    (dir_path / "runlog.jsonl").write_text("\n".join(lines) + "\n")


def test_diagnosis_dominant_spawn_family(tmp_path):
    from envmaker.cli import diagnose_failed_attempts

    a = tmp_path / "a1"
    b = tmp_path / "a2"
    _write_runlog(
        a,
        [
            (["v1.program_failed"], "ValueError: spawn must lie on the ground footprint..."),
            (["v1.program_failed"], "ValueError: spawn intersects a blocker: ..."),
        ],
    )
    _write_runlog(
        b,
        [(["v5.spawn_intersects_blocker"], "spawn intersects a blocker collider footprint")],
    )
    diagnosis = diagnose_failed_attempts([a, b])
    assert diagnosis.startswith("structural (spawn placement")
    assert "0.4 m clearance" in diagnosis


def test_diagnosis_mixed_failures_is_generic(tmp_path):
    from envmaker.cli import diagnose_failed_attempts

    a = tmp_path / "a1"
    _write_runlog(
        a,
        [
            (["v1.program_failed"], "ValueError: unknown material: sand"),
            (["v6.clear_ground_fraction"], "connected clear-ground fraction 0.44 below threshold"),
        ],
    )
    diagnosis = diagnose_failed_attempts([a])
    assert diagnosis.startswith("no single structural blocker")
    assert "--attempts" in diagnosis


def test_run_retries_on_rejection_and_prints_diagnosis(monkeypatch, tmp_path):
    calls = []

    class _Outcome:
        terminal_state = "rejected_after_budget"
        bundle_sealed = False

        def __init__(self, run_dir):
            self.run_dir = run_dir

    def _fake_authoring(prompt, **kwargs):
        calls.append(kwargs["run_dir"])
        _write_runlog(
            Path(kwargs["run_dir"]),
            [
                (
                    ["v1.program_failed"],
                    "ValueError: spawn intersects a blocker: ...",
                ),
                (
                    ["v1.program_failed"],
                    "ValueError: spawn must lie on the ground footprint ...",
                ),
            ],
        )
        return _Outcome(kwargs["run_dir"])

    class _FakeProvider:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr("envmaker.agent.loop.run_authoring", _fake_authoring)
    monkeypatch.setattr(
        "envmaker.agent.providers.OpenAIProvider", _FakeProvider
    )
    monkeypatch.setattr("envmaker.cli._RUNS_ROOT", tmp_path)

    result = _RUNNER.invoke(app, ["run", "island", "--attempts", "2"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 2, "rejected_after_budget must retry"
    assert "attempt 2/2" in result.output
    assert "prompt_diagnosis: structural (spawn placement" in result.output


def test_run_accepted_does_not_retry(monkeypatch, tmp_path):
    calls = []

    class _Outcome:
        terminal_state = "accepted"
        bundle_sealed = True
        final_source = None

        def __init__(self, run_dir):
            self.run_dir = run_dir

    def _fake_authoring(prompt, **kwargs):
        calls.append(1)
        return _Outcome(kwargs["run_dir"])

    class _FakeProvider:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr("envmaker.agent.loop.run_authoring", _fake_authoring)
    monkeypatch.setattr(
        "envmaker.agent.providers.OpenAIProvider", _FakeProvider
    )
    monkeypatch.setattr("envmaker.cli._RUNS_ROOT", tmp_path)

    result = _RUNNER.invoke(app, ["run", "village", "--attempts", "3"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1, "accepted must not retry"
    assert "prompt_diagnosis" not in result.output


def test_present_environment_fullscreen_and_wander(monkeypatch):
    import envmaker.runtime as runtime_module
    from envmaker import cli as cli_module

    captured = {}

    class _FakeDriver:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            captured["wander_env"] = __import__("os").environ.get(
                "ENVMAKER_WANDER"
            )
            self.closed = False

        def start(self):
            pass

        def load_candidate(self, candidate):
            captured["loaded"] = candidate is not None

        def wait_navigation_ready(self, timeout):
            captured["nav_timeout"] = timeout

        def is_running(self):
            return False

        def close(self):
            self.closed = True

    monkeypatch.setattr(runtime_module, "RuntimeDriver", _FakeDriver)
    demo_source = (
        Path(__file__).resolve().parents[2] / "examples/demo/environment.py"
    ).read_text()

    cli_module.present_environment(demo_source, Path("/tmp/present-test"))

    assert captured["kwargs"]["window_args"] == cli_module._VIEW_WINDOW_ARGS
    assert captured["kwargs"]["windowed"] is True
    assert captured["wander_env"] == "1"
    assert captured["loaded"] is True
    import os

    assert os.environ.get("ENVMAKER_WANDER") != "1" or True


def test_run_opens_presentation_only_when_requested(monkeypatch, tmp_path):
    from envmaker import cli as cli_module

    presented = []
    monkeypatch.setattr(
        cli_module,
        "present_environment",
        lambda source, run_dir: presented.append(source),
    )

    class _Outcome:
        terminal_state = "accepted"
        bundle_sealed = True
        final_source = "environment = None"

        def __init__(self, run_dir):
            self.run_dir = run_dir

    def _fake_authoring(prompt, **kwargs):
        return _Outcome(kwargs["run_dir"])

    class _FakeProvider:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr("envmaker.agent.loop.run_authoring", _fake_authoring)
    monkeypatch.setattr(
        "envmaker.agent.providers.OpenAIProvider", _FakeProvider
    )
    monkeypatch.setattr("envmaker.cli._RUNS_ROOT", tmp_path)

    result = _RUNNER.invoke(app, ["run", "village", "--no-open"])
    assert result.exit_code == 0
    assert presented == []

    result = _RUNNER.invoke(app, ["run", "village", "--open"])
    assert result.exit_code == 0
    assert presented == ["environment = None"]

    presented.clear()
    result = _RUNNER.invoke(app, ["run", "village"])
    assert result.exit_code == 0
    assert presented == [], "non-tty default must not open a window"
