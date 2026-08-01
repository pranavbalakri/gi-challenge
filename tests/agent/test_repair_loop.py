"""Authoring-loop coverage: keyless scripted repair + planner-gated live money test."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from envmaker.agent.loop import AuthoringOutcome, run_authoring
from envmaker.agent.providers import ProviderTurn, ScriptedProvider
from envmaker.agent.tools import ToolSurface
from envmaker.core.artifacts import ArtifactRef
from envmaker.core.episode import EpisodeResult, NavigationProbe, TerminalReason
from envmaker.core.program import ProviderInfo, ResourceLimits
from envmaker.godot_bridge.process import resolve_godot_binary
from envmaker.runtime import (
    _blocker_oriented_list,
    _point_clear,
    _segment_clear,
)
from envmaker.validation import validate_static


_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "frozen_village"
_GODOT_BIN = resolve_godot_binary()
_LIMITS = ResourceLimits(
    cpu_seconds=30.0,
    memory_mb=512,
    output_bytes=1_048_576,
    wall_seconds=60.0,
)
_PROMPT = "a frozen village with a walled square and a watchtower"


def _godot_user_dir_writable(home: Path | None = None) -> bool:
    base = (home or Path.home()) / "Library" / "Application Support" / "Godot"
    probe_dir = base if base.is_dir() else base.parent
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=probe_dir):
            pass
    except OSError:
        return False
    return True


def _require_godot_user_dir() -> None:
    if not _godot_user_dir_writable():
        pytest.skip(
            "Godot user-data dir is not writable (sandboxed shell?); "
            "live Godot tests require an unsandboxed run"
        )


def _require_godot_binary() -> None:
    if not _GODOT_BIN.is_file():
        pytest.skip(
            f"Godot binary not found: {_GODOT_BIN} (set GODOT_BIN to override)"
        )


def _descriptor() -> ProviderInfo:
    return ProviderInfo(
        provider="scripted",
        model_name="frozen-village-fixture",
        prompt_version="1",
    )


class _RepairStubDriver:
    """Stub driver: first simulate fails v6 at 0.44; second passes all stages."""

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def load_candidate(self, candidate: object) -> SimpleNamespace:
        return SimpleNamespace(ok=True)

    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        return None

    def connected_navigable_fraction(self) -> float:
        return 0.44 if self.calls == 0 else 0.95

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
        digest = "c" * 64
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


def _stub_factory(run_dir: Path) -> _RepairStubDriver:
    del run_dir
    return _RepairStubDriver()


def _money_kind_sequence(events: list[dict]) -> list[object]:
    """Condense the runlog into the money-test asserted kind sequence."""

    sequence: list[object] = []
    saw_provider = False
    saw_revision = False
    for event in events:
        kind = event["kind"]
        payload = event.get("payload") or {}
        if kind == "system_prompt":
            sequence.append("system_prompt")
        elif kind == "user_prompt":
            sequence.append("user_prompt")
        elif kind == "provider_turn" and not saw_provider:
            sequence.append("provider_turn")
            saw_provider = True
        elif kind == "revision" and not saw_revision:
            sequence.append("revision")
            saw_revision = True
        elif kind == "tool_call" and "signal_codes" in payload:
            name = payload.get("name")
            ok = payload.get("ok")
            codes = payload.get("signal_codes") or []
            messages = str(payload.get("signal_messages") or "")
            if name == "compile_environment" and not ok:
                sequence.append(
                    (
                        "tool_call",
                        "compile",
                        "v1.program_failed" in codes
                        or "spawn intersects a blocker" in messages,
                        "spawn intersects a blocker" in messages,
                    )
                )
            elif name == "compile_environment" and ok:
                sequence.append(("tool_call", "compile", True))
            elif name == "patch_program" and ok:
                sequence.append(("tool_call", "patch", True))
            elif name == "simulate_navigation" and not ok:
                sequence.append(
                    (
                        "tool_call",
                        "simulate",
                        "v6.navigation_fraction" in codes,
                        0.44,
                    )
                )
            elif name == "simulate_navigation" and ok:
                sequence.append(("tool_call", "simulate", True))
        elif kind == "outcome":
            sequence.append(("outcome", payload.get("terminal_state")))
    return sequence


def test_frozen_village_walkable_fraction_math() -> None:
    """Pure-geometry check: blocked gate ≈0.44; open gate ≥0.9."""

    rev1 = (_FIXTURE / "rev1.py").read_text(encoding="utf-8")
    td = Path(tempfile.mkdtemp())
    from envmaker.agent.tools import ToolContext
    from envmaker.runlog import RunLog

    ctx = ToolContext(
        source=rev1,
        limits=_LIMITS,
        run_dir=td,
        runlog=RunLog(td / "r.jsonl"),
    )
    surface = ToolSurface(ctx)
    assert surface.patch_program((_FIXTURE / "patch1.txt").read_text(encoding="utf-8")).ok
    blocked_source = ctx.source
    assert surface.patch_program((_FIXTURE / "patch2.txt").read_text(encoding="utf-8")).ok
    open_source = ctx.source

    # Area left of the bisecting wall (x < -2.25) over 40×40 ground.
    left_fraction = (17.75 * 40.0) / 1600.0
    assert 0.4 <= left_fraction <= 0.48

    def _grid_fraction(source: str, *, grid: int = 40) -> float:
        static = validate_static(source, limits=_LIMITS)
        assert static.candidate is not None
        blockers = _blocker_oriented_list(static.candidate)
        min_x = min_z = -20.0
        max_x = max_z = 20.0
        xs = [min_x + (i + 0.5) * (max_x - min_x) / grid for i in range(grid)]
        zs = [min_z + (j + 0.5) * (max_z - min_z) / grid for j in range(grid)]
        walkable = [[_point_clear(x, z, blockers) for z in zs] for x in xs]
        origin = (-16.0, -16.0)
        best = None
        best_dist = math.inf
        for i, x in enumerate(xs):
            for j, z in enumerate(zs):
                if not walkable[i][j]:
                    continue
                dist = math.hypot(x - origin[0], z - origin[1])
                if dist < best_dist:
                    best_dist = dist
                    best = (i, j)
        assert best is not None
        seen = [[False] * grid for _ in range(grid)]
        queue = [best]
        seen[best[0]][best[1]] = True
        reachable = 0
        while queue:
            i, j = queue.pop()
            reachable += 1
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ni, nj = i + di, j + dj
                if not (0 <= ni < grid and 0 <= nj < grid):
                    continue
                if seen[ni][nj] or not walkable[ni][nj]:
                    continue
                if not _segment_clear(xs[i], zs[j], xs[ni], zs[nj], blockers):
                    continue
                seen[ni][nj] = True
                queue.append((ni, nj))
        return reachable / float(grid * grid)

    blocked = _grid_fraction(blocked_source)
    opened = _grid_fraction(open_source)
    assert 0.4 <= blocked <= 0.48
    assert opened >= 0.9


def test_keyless_two_repair_loop_accepts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = ScriptedProvider.from_fixture(_FIXTURE / "transcript.json")
    driver = _RepairStubDriver()

    def factory(run_dir: Path) -> _RepairStubDriver:
        del run_dir
        return driver

    # Keep the stub's zero-arg fraction in sync with call order by bumping
    # after each validate_candidate (simulate) invocation.
    import envmaker.agent.tools as tools_mod

    original = tools_mod._validate_candidate

    def _counting_validate(model, candidate, drv, *, probe, min_walkable_fraction=0.5):
        reports = original(
            model,
            candidate,
            drv,
            probe=probe,
            min_walkable_fraction=min_walkable_fraction,
        )
        driver.calls += 1
        return reports

    monkeypatch.setattr(tools_mod, "_validate_candidate", _counting_validate)

    outcome = run_authoring(
        _PROMPT,
        provider=provider,
        seed=7,
        max_turns=8,
        wall_seconds=120.0,
        run_dir=tmp_path,
        driver_factory=factory,
        limits=_LIMITS,
    )
    assert isinstance(outcome, AuthoringOutcome)
    assert outcome.terminal_state == "accepted"
    assert outcome.turns_used == 8
    assert outcome.bundle_sealed is True

    rev1 = (tmp_path / "revisions" / "rev-1.py").read_text(encoding="utf-8")
    rev2 = (tmp_path / "revisions" / "rev-2.py").read_text(encoding="utf-8")
    rev3 = (tmp_path / "revisions" / "rev-3.py").read_text(encoding="utf-8")
    assert 'position=(-11.0, -11.0)' in rev1
    assert 'position=(-16.0, -16.0)' in rev2
    assert "gate_ice" in rev2
    assert "gate_ice" not in rev3

    from envmaker.runlog import RunLog

    events = RunLog(tmp_path / "runlog.jsonl").events()
    sequence = _money_kind_sequence(events)
    assert sequence == [
        "system_prompt",
        "user_prompt",
        "provider_turn",
        "revision",
        ("tool_call", "compile", True, True),
        ("tool_call", "patch", True),
        ("tool_call", "compile", True),
        ("tool_call", "simulate", True, 0.44),
        ("tool_call", "patch", True),
        ("tool_call", "compile", True),
        ("tool_call", "simulate", True),
        ("outcome", "accepted"),
    ]
    # First compile signal message must carry the builder error text.
    compile_events = [
        event
        for event in events
        if event["kind"] == "tool_call"
        and event["payload"].get("name") == "compile_environment"
    ]
    assert "spawn intersects a blocker" in compile_events[0]["payload"]["signal_messages"]
    assert "v1.program_failed" in compile_events[0]["payload"]["signal_codes"]
    sim_events = [
        event
        for event in events
        if event["kind"] == "tool_call"
        and event["payload"].get("name") == "simulate_navigation"
    ]
    assert "v6.navigation_fraction" in sim_events[0]["payload"]["signal_codes"]
    assert driver.closed is True


def test_budget_exhaustion_rejected(tmp_path: Path) -> None:
    turns = tuple(ProviderTurn(text=f"thinking {index}") for index in range(20))
    provider = ScriptedProvider(turns, descriptor=_descriptor())
    outcome = run_authoring(
        "noop",
        provider=provider,
        seed=1,
        max_turns=3,
        wall_seconds=30.0,
        run_dir=tmp_path,
        driver_factory=_stub_factory,
        limits=_LIMITS,
    )
    assert outcome.terminal_state == "rejected_after_budget"
    assert outcome.turns_used == 3


def test_provider_error_on_exhausted_transcript(tmp_path: Path) -> None:
    provider = ScriptedProvider((), descriptor=_descriptor())
    outcome = run_authoring(
        "noop",
        provider=provider,
        seed=1,
        max_turns=4,
        wall_seconds=30.0,
        run_dir=tmp_path,
        driver_factory=_stub_factory,
        limits=_LIMITS,
    )
    assert outcome.terminal_state == "provider_error"
    assert outcome.turns_used == 0


def test_harness_error_when_tool_surface_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        (
            ProviderTurn(code="x = 1\n"),
            ProviderTurn(tool="compile_environment", args={}),
        ),
        descriptor=_descriptor(),
    )

    import envmaker.agent.loop as loop_mod

    def _trim_boom(messages: list) -> None:
        raise RuntimeError("trim failed")

    monkeypatch.setattr(loop_mod, "_trim_messages", _trim_boom)

    outcome = run_authoring(
        "noop",
        provider=provider,
        seed=1,
        max_turns=4,
        wall_seconds=30.0,
        run_dir=tmp_path,
        driver_factory=_stub_factory,
        limits=_LIMITS,
    )
    assert outcome.terminal_state == "harness_error"


def test_unknown_tool_records_error_and_continues(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        (
            ProviderTurn(tool="not_a_real_tool", args={}),
            ProviderTurn(text="still going"),
            ProviderTurn(text="and again"),
        ),
        descriptor=_descriptor(),
    )
    outcome = run_authoring(
        "noop",
        provider=provider,
        seed=1,
        max_turns=3,
        wall_seconds=30.0,
        run_dir=tmp_path,
        driver_factory=_stub_factory,
        limits=_LIMITS,
    )
    assert outcome.terminal_state == "rejected_after_budget"
    from envmaker.runlog import RunLog

    events = RunLog(tmp_path / "runlog.jsonl").events()
    tool_calls = [
        event for event in events if event["kind"] == "tool_call"
    ]
    assert tool_calls
    assert tool_calls[0]["payload"]["ok"] is False
    assert "unknown tool" in str(tool_calls[0]["payload"].get("reason", ""))


# planner-gated: drives live Godot
def test_live_two_repair_money_fixture(tmp_path: Path) -> None:
    _require_godot_binary()
    _require_godot_user_dir()

    provider = ScriptedProvider.from_fixture(_FIXTURE / "transcript.json")
    outcome = run_authoring(
        _PROMPT,
        provider=provider,
        seed=7,
        max_turns=8,
        wall_seconds=300.0,
        run_dir=tmp_path,
        driver_factory=None,
        limits=_LIMITS,
    )
    assert outcome.terminal_state == "accepted"
    assert outcome.bundle_sealed is True
    assert outcome.turns_used == 8

    from envmaker.runlog import RunLog

    events = RunLog(tmp_path / "runlog.jsonl").events()
    sequence = _money_kind_sequence(events)
    assert sequence[0:4] == [
        "system_prompt",
        "user_prompt",
        "provider_turn",
        "revision",
    ]
    assert sequence[-1] == ("outcome", "accepted")
    sim_events = [
        event
        for event in events
        if event["kind"] == "tool_call"
        and event["payload"].get("name") == "simulate_navigation"
    ]
    assert sim_events[-1]["payload"]["ok"] is True
    terminal = sim_events[-1]["payload"]["result"].get("terminal_reason")
    assert terminal == TerminalReason.ARRIVED.value


def test_default_factory_session_id_always_contract_valid() -> None:
    import re

    from envmaker.agent import loop as loop_module
    from envmaker.core.contracts import SESSION_ID_PATTERN

    captured: dict[str, str] = {}

    class _FakeDriver:
        def __init__(self, **kwargs: object) -> None:
            captured["session_id"] = str(kwargs["session_id"])

        def start(self) -> None:
            pass

    import envmaker.runtime as runtime_module

    original = runtime_module.RuntimeDriver
    runtime_module.RuntimeDriver = _FakeDriver  # type: ignore[misc]
    try:
        loop_module._default_driver_factory(Path("/tmp/has_under_scores_0"))
    finally:
        runtime_module.RuntimeDriver = original  # type: ignore[misc]

    assert re.fullmatch(SESSION_ID_PATTERN, captured["session_id"])


def test_empty_source_tool_turn_appends_nudge(tmp_path: Path) -> None:
    import json

    from envmaker.agent.providers import ProviderTurn, ScriptedProvider
    from envmaker.core.program import ProviderInfo

    provider = ScriptedProvider(
        [
            ProviderTurn(tool="compile_environment"),
            ProviderTurn(tool="patch_program", args={"patch": "x"}),
        ],
        descriptor=ProviderInfo(
            provider="scripted", model_name="fixture", prompt_version="1"
        ),
    )
    outcome = run_authoring(
        "nudge probe",
        provider=provider,
        seed=1,
        max_turns=2,
        wall_seconds=30.0,
        run_dir=tmp_path,
        driver_factory=lambda run_dir: (_ for _ in ()).throw(
            AssertionError("driver must not start")
        ),
        limits=_LIMITS,
    )
    assert outcome.terminal_state == "provider_error" or outcome.turns_used == 2
    kinds = [
        json.loads(line)["kind"] for line in open(tmp_path / "runlog.jsonl")
    ]
    assert "nudge" in kinds
    patch_events = [
        json.loads(line)
        for line in open(tmp_path / "runlog.jsonl")
        if json.loads(line)["kind"] == "tool_call"
        and "patch_program"
        in (
            str(json.loads(line)["payload"].get("tool", ""))
            + str(json.loads(line)["payload"].get("name", ""))
        )
    ]
    assert patch_events, "patch tool_call should be logged"
    assert "no program exists yet" in str(patch_events[-1]["payload"])
