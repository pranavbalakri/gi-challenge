"""Unit coverage for the evaluation harness."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from envmaker.agent.loop import AuthoringOutcome
from envmaker.agent.providers import ProviderError, ProviderTurn, ScriptedProvider
from envmaker.core.artifacts import ArtifactRef
from envmaker.core.episode import EpisodeResult, NavigationProbe, TerminalReason
from envmaker.core.program import ProviderInfo, ResourceLimits
from envmaker.evaluation import (
    EvalRunResult,
    load_eval_config,
    run_evaluation,
    run_variant_loop,
    run_variant_oneshot,
    write_report,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MVP = _REPO_ROOT / "evals" / "mvp.yaml"
_DEMO = (_REPO_ROOT / "examples" / "demo" / "environment.py").read_text(
    encoding="utf-8"
)
_BAD_CODE = "this is not valid python environment code !!!\n"
_LIMITS = ResourceLimits(
    cpu_seconds=30.0,
    memory_mb=512,
    output_bytes=1_048_576,
    wall_seconds=60.0,
)
from envmaker.agent.prompts import PROMPT_VERSION as _LIVE_PROMPT_VERSION

# Interpolated so the config guard (config prompt_version must equal the
# imported PROMPT_VERSION) keeps passing across prompt bumps.
_MINI_YAML = f"""
model: gpt-4o-mini
prompt_version: "{_LIVE_PROMPT_VERSION}"
budgets:
  max_turns: 2
  wall_seconds: 30
  limits:
    cpu_seconds: 5
    memory_mb: 256
    output_bytes: 65536
    wall_seconds: 10
seeds: [7]
variants: [oneshot, loop]
prompts:
  - id: p1
    text: one
    checklist: ["a", "b", "c"]
  - id: p2
    text: two
    checklist: ["a", "b", "c"]
""".strip()


def _descriptor() -> ProviderInfo:
    return ProviderInfo(
        provider="scripted",
        model_name="eval-fixture",
        prompt_version="1",
    )


class _HappyDriver:
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
        digest = "a" * 64
        return ArtifactRef(
            path=f"renders/{view}.png",
            media_type="image/png",
            byte_count=128,
            blake2b256=digest,
            sha256=digest,
            producer="stub",
            toolchain_version="test",
        )

    def close(self) -> int:
        return 0


def test_mvp_yaml_schema_sanity() -> None:
    config = load_eval_config(_MVP)
    assert config["model"] == "gpt-5.4-mini"
    assert config["seeds"] == [7]
    assert config["variants"] == ["oneshot", "loop"]
    prompts = config["prompts"]
    assert len(prompts) == 6
    for entry in prompts:
        checklist = entry["checklist"]
        assert 3 <= len(checklist) <= 5
        assert entry["id"]
        assert entry["text"]


def test_load_eval_config_rejects_bare_string_prompt(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        """
model: gpt-4o-mini
prompt_version: "1"
seeds: [7]
variants: [oneshot]
prompts:
  - just a string
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="index 0") as exc:
        load_eval_config(path)
    assert "just a string" in str(exc.value)


def test_oneshot_code_provider_accepted(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [ProviderTurn(code=_DEMO)],
        descriptor=_descriptor(),
    )
    result = run_variant_oneshot(
        "village green",
        provider=provider,
        seed=7,
        run_dir=tmp_path / "oneshot",
        limits=_LIMITS,
        driver_factory=lambda _run_dir: _HappyDriver(),
        prompt_id="village",
    )
    assert result.terminal_state == "accepted"
    assert result.accepted is True
    assert result.stages_passed.get("program") is True
    assert result.stages_passed.get("controller") is True


def test_oneshot_tool_turn_is_rejected_after_budget(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [ProviderTurn(tool="compile_environment")],
        descriptor=_descriptor(),
    )
    result = run_variant_oneshot(
        "village green",
        provider=provider,
        seed=7,
        run_dir=tmp_path / "oneshot-bad",
        limits=_LIMITS,
        driver_factory=lambda _run_dir: _HappyDriver(),
        prompt_id="village",
    )
    assert result.terminal_state == "rejected_after_budget"
    assert result.accepted is False
    assert result.error == "oneshot contract violated: model called a tool"


def test_oneshot_noncompiling_code_no_second_provider_turn(tmp_path: Path) -> None:
    """Pins oneshot no-feedback: one provider turn even when compile fails."""

    provider = ScriptedProvider(
        [ProviderTurn(code=_BAD_CODE)],
        descriptor=_descriptor(),
    )
    result = run_variant_oneshot(
        "broken",
        provider=provider,
        seed=7,
        run_dir=tmp_path / "oneshot-bad-code",
        limits=_LIMITS,
        driver_factory=lambda _run_dir: (_ for _ in ()).throw(
            AssertionError("driver must not start for failed compile")
        ),
        prompt_id="broken",
    )
    assert result.terminal_state == "rejected_after_budget"
    assert result.turns_used == 1
    # Exhaustion would flip a second next_turn into ProviderError.
    with pytest.raises(ProviderError, match="exhausted"):
        provider.next_turn([])


def test_loop_variant_maps_run_authoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import envmaker.evaluation as evaluation

    def _fake_authoring(*_args: object, **_kwargs: object) -> AuthoringOutcome:
        return AuthoringOutcome(
            terminal_state="rejected_after_budget",
            turns_used=3,
            final_source="x = 1",
            bundle_sealed=False,
            run_dir=tmp_path / "loop",
            failure_summary="budget",
        )

    monkeypatch.setattr(evaluation, "_run_authoring", _fake_authoring)
    provider = ScriptedProvider([], descriptor=_descriptor())
    result = run_variant_loop(
        "prompt",
        provider=provider,
        seed=7,
        run_dir=tmp_path / "loop",
        limits=_LIMITS,
        prompt_id="p1",
    )
    assert result.variant == "loop"
    assert result.terminal_state == "rejected_after_budget"
    assert result.turns_used == 3
    assert result.error == "budget"


def test_write_report_required_sections(tmp_path: Path) -> None:
    results = [
        EvalRunResult(
            prompt_id="a",
            variant="oneshot",
            seed=7,
            terminal_state="accepted",
            stages_passed={"program": True, "controller": True},
            turns_used=1,
            wall_seconds=1.5,
            accepted=True,
            run_dir=str(tmp_path / "a"),
            error=None,
        ),
        EvalRunResult(
            prompt_id="b",
            variant="loop",
            seed=7,
            terminal_state="rejected_after_budget",
            stages_passed={"program": True, "controller": False},
            turns_used=4,
            wall_seconds=9.0,
            accepted=False,
            run_dir=str(tmp_path / "b"),
            error="nav failed",
        ),
        EvalRunResult(
            prompt_id="c",
            variant="loop",
            seed=7,
            terminal_state="provider_error",
            stages_passed={},
            turns_used=0,
            wall_seconds=0.2,
            accepted=False,
            run_dir=str(tmp_path / "c"),
            error="api down",
        ),
    ]
    path = tmp_path / "mvp-report.md"
    write_report(
        results,
        {
            "model": "gpt-4o-mini",
            "prompt_version": "1",
            "eval_root": str(tmp_path / "eval-root"),
        },
        path,
    )
    text = path.read_text(encoding="utf-8")
    assert "## Aggregate by variant" in text
    assert "program execution rate" in text
    assert "hard-valid rate" in text
    assert "traversal success rate" in text
    assert "mean repair turns" in text
    assert "mean latency" in text
    assert "Means exclude provider_error/harness_error rows" in text
    assert "Loop-variant program execution is derived from observed compile events" in text
    assert "eval_root:" in text
    assert "## Per-run outcomes" in text
    assert "prompt_id" in text and "terminal_state" in text
    assert "## Failed examples" in text
    assert "## Limitations" in text
    assert "Provider/harness errors are reported separately" in text
    # Means exclude the 0.2s provider_error row: (1.5+9.0)/2 for oneshot? 
    # oneshot scored=[a]; loop scored=[b] only → loop mean latency 9.00, mean turns 4.00
    assert "| loop | 2 |" in text
    assert "4.00" in text
    assert "9.00" in text


def test_run_evaluation_injected_factories(tmp_path: Path) -> None:
    config_path = tmp_path / "mini.yaml"
    config_path.write_text(_MINI_YAML + "\n", encoding="utf-8")

    class _CodeProvider:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._done = False

        @property
        def descriptor(self) -> ProviderInfo:
            return _descriptor()

        def next_turn(self, messages: list[dict[str, object]]) -> ProviderTurn:
            del messages
            if self._done:
                return ProviderTurn(text="stop")
            self._done = True
            return ProviderTurn(code=_DEMO)

    report_path = tmp_path / "report.md"
    results = run_evaluation(
        config_path,
        report_path,
        provider_factory=lambda _name: _CodeProvider(),
        driver_factory=lambda _run_dir: _HappyDriver(),
        runs_root=tmp_path / "eval-runs",
    )
    assert len(results) == 4
    assert report_path.is_file()
    variants = {(r.prompt_id, r.variant) for r in results}
    assert variants == {
        ("p1", "oneshot"),
        ("p1", "loop"),
        ("p2", "oneshot"),
        ("p2", "loop"),
    }
    # Cells live under an invocation-namespaced root.
    roots = {Path(r.run_dir).parent for r in results}
    assert len(roots) == 1
    assert next(iter(roots)).parent == tmp_path / "eval-runs"


def test_run_evaluation_namespaces_roots_no_stage_bleed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import envmaker.evaluation as evaluation

    config_path = tmp_path / "mini.yaml"
    config_path.write_text(
        f"""
model: gpt-4o-mini
prompt_version: "{_LIVE_PROMPT_VERSION}"
budgets:
  max_turns: 1
  wall_seconds: 5
  limits:
    cpu_seconds: 5
    memory_mb: 256
    output_bytes: 65536
    wall_seconds: 5
seeds: [7]
variants: [loop]
prompts:
  - id: p1
    text: one
    checklist: ["a", "b", "c"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    def _authoring_pass(prompt: str, **kwargs: object) -> AuthoringOutcome:
        del prompt
        run_dir = Path(str(kwargs["run_dir"]))
        run_dir.mkdir(parents=True, exist_ok=True)
        from envmaker.runlog import RunLog

        log = RunLog(run_dir / "runlog.jsonl")
        log.append(
            "tool_call",
            {
                "name": "compile_environment",
                "ok": True,
                "stage_outcomes": {"program": True, "sdk_model": True},
            },
        )
        return AuthoringOutcome(
            terminal_state="accepted",
            turns_used=2,
            final_source="ok",
            bundle_sealed=True,
            run_dir=run_dir,
            failure_summary=None,
        )

    def _authoring_empty(prompt: str, **kwargs: object) -> AuthoringOutcome:
        del prompt
        run_dir = Path(str(kwargs["run_dir"]))
        run_dir.mkdir(parents=True, exist_ok=True)
        return AuthoringOutcome(
            terminal_state="rejected_after_budget",
            turns_used=1,
            final_source=None,
            bundle_sealed=False,
            run_dir=run_dir,
            failure_summary="no compile",
        )

    monkeypatch.setattr(evaluation, "_run_authoring", _authoring_pass)
    first_report = tmp_path / "r1.md"
    first = run_evaluation(
        config_path,
        first_report,
        provider_factory=lambda _n: ScriptedProvider([], descriptor=_descriptor()),
        runs_root=tmp_path / "eval-runs",
    )
    assert first[0].stages_passed.get("program") is True
    first_root = Path(first[0].run_dir).parent

    monkeypatch.setattr(evaluation, "_run_authoring", _authoring_empty)
    second_report = tmp_path / "r2.md"
    second = run_evaluation(
        config_path,
        second_report,
        provider_factory=lambda _n: ScriptedProvider([], descriptor=_descriptor()),
        runs_root=tmp_path / "eval-runs",
    )
    second_root = Path(second[0].run_dir).parent
    assert first_root != second_root
    assert first_root.parent == second_root.parent == tmp_path / "eval-runs"
    assert second[0].stages_passed.get("program", False) is False
    sidecar = json.loads(second_report.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["eval_root"] == str(second_root)


def test_run_evaluation_keyboard_interrupt_writes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import envmaker.evaluation as evaluation

    config_path = tmp_path / "mini.yaml"
    config_path.write_text(_MINI_YAML + "\n", encoding="utf-8")
    calls = {"n": 0}

    class _Provider:
        @property
        def descriptor(self) -> ProviderInfo:
            return _descriptor()

        def next_turn(self, messages: list[dict[str, object]]) -> ProviderTurn:
            del messages
            return ProviderTurn(code=_BAD_CODE)

    def _loop_or_interrupt(prompt: str, **kwargs: object) -> AuthoringOutcome:
        del prompt, kwargs
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt
        return AuthoringOutcome(
            terminal_state="rejected_after_budget",
            turns_used=1,
            final_source=None,
            bundle_sealed=False,
            run_dir=tmp_path / "partial-cell",
            failure_summary="budget",
        )

    monkeypatch.setattr(evaluation, "_run_authoring", _loop_or_interrupt)
    report_path = tmp_path / "partial.md"
    with pytest.raises(KeyboardInterrupt):
        run_evaluation(
            config_path,
            report_path,
            provider_factory=lambda _n: _Provider(),
            driver_factory=lambda _d: _HappyDriver(),
            runs_root=tmp_path / "eval-runs",
            only=["p1", "p2"],
        )
    assert report_path.is_file()
    text = report_path.read_text(encoding="utf-8")
    # First cell is oneshot (bad code) — at least one row written before interrupt.
    assert "| p1 |" in text
    sidecar = json.loads(report_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert len(sidecar["results"]) >= 1


def test_run_matrix_refuses_prompt_version_mismatch(tmp_path: Path) -> None:
    """Reports must never be silently mislabeled with a stale prompt version."""

    config = tmp_path / "mismatch.yaml"
    config.write_text(
        "model: gpt-5.4-mini\n"
        'prompt_version: "1"\n'
        "seeds: [7]\n"
        "variants: [oneshot]\n"
        "prompts:\n"
        "  - id: sample\n"
        "    text: a meadow\n"
        "    checklist:\n"
        "      - ground exists\n"
        "      - spawn exists\n"
        "      - route works\n",
        encoding="utf-8",
    )
    from envmaker.agent.prompts import PROMPT_VERSION
    from envmaker.evaluation import run_evaluation

    assert PROMPT_VERSION != "1", "update this test if v1 returns"
    with pytest.raises(ValueError, match="does not match the imported"):
        run_evaluation(
            config, tmp_path / "report.md", runs_root=tmp_path / "runs"
        )
