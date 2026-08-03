"""Agent-driven authoring session tests (keyless, Godot-free)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from envmaker.author import (
    STARTER_TEMPLATE,
    init_session,
    read_session_prompt,
    step_session,
)
from envmaker.cli import app

_RUNNER = CliRunner()
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_SOURCE = (_REPO_ROOT / "examples/demo/environment.py").read_text()


class _HappyDriver:
    """Duck-typed RuntimeDriver stub: everything succeeds."""

    def __init__(self, run_dir: Path) -> None:
        self._artifacts = run_dir / "runtime" / "artifacts"
        self._artifacts.mkdir(parents=True, exist_ok=True)
        self.closed = False

    def load_candidate(self, candidate: object) -> None:
        return None

    def wait_navigation_ready(self, timeout: float = 30.0) -> bool:
        return True

    def connected_clear_ground_fraction(self, **kwargs: object) -> float:
        return 0.95

    def navigate(self, probe: object) -> object:
        from envmaker.core.episode import EpisodeResult, TerminalReason

        return EpisodeResult(
            probe_fingerprint=probe.probe_fingerprint,  # type: ignore[attr-defined]
            terminal_reason=TerminalReason.ARRIVED,
            ticks_used=40,
            final_geodesic_distance_m=0.2,
            path_length_m=12.0,
            collisions=0,
            stuck_recoveries=0,
        )

    def render(self, view: str) -> object:
        import hashlib

        name = f"render-{view}.png"
        data = b"\x89PNG\r\n" + view.encode() * 20
        (self._artifacts / name).write_bytes(data)
        return type(
            "Ref",
            (),
            {
                "path": f"artifacts/{name}",
                "blake2b256": hashlib.blake2b(data, digest_size=32).hexdigest(),
                "byte_count": len(data),
            },
        )()

    def close(self) -> None:
        self.closed = True


def test_init_creates_session_layout(tmp_path: Path) -> None:
    run_dir = tmp_path / "session"
    init_session("a foggy hollow", 9, run_dir)
    assert (run_dir / "environment.py").read_text() == STARTER_TEMPLATE
    assert read_session_prompt(run_dir) == ("a foggy hollow", 9)
    kinds = [
        json.loads(line)["kind"]
        for line in (run_dir / "runlog.jsonl").read_text().splitlines()
    ]
    assert kinds[:2] == ["system_prompt", "user_prompt"]


def test_step_reports_template_and_static_failures(tmp_path: Path) -> None:
    run_dir = tmp_path / "session"
    init_session("hollow", 9, run_dir)
    assert step_session(run_dir).status == "empty"

    (run_dir / "environment.py").write_text(
        "def build_environment():\n"
        "    raise ValueError('boom')\n\n"
        "environment = build_environment()\n"
    )
    outcome = step_session(run_dir)
    assert outcome.status == "static_failed"
    assert outcome.stages.get("program") is False
    assert any("v1" in code for code, _m, _g in outcome.signals)


def test_step_accepts_demo_fixture_with_stub_driver(tmp_path: Path) -> None:
    run_dir = tmp_path / "session"
    init_session("village green", 7, run_dir)
    (run_dir / "environment.py").write_text(_DEMO_SOURCE)

    drivers: list[_HappyDriver] = []

    def _factory(rd: Path) -> _HappyDriver:
        driver = _HappyDriver(rd)
        drivers.append(driver)
        return driver

    outcome = step_session(run_dir, driver_factory=_factory)
    assert outcome.status == "accepted", outcome.signals
    assert all(outcome.stages.values()) and len(outcome.stages) == 9
    assert outcome.definition_fingerprint
    definition_file = run_dir / "environment-definition.json"
    assert definition_file.is_file()
    # Canonical envelope: {"canon": N, "payload": <definition>}.
    payload = json.loads(definition_file.read_text())["payload"]
    assert payload["program"]["provider"]["provider"] == "agent-driven"
    assert len(outcome.renders) == 2
    assert drivers and drivers[0].closed


def test_author_cli_init_and_step(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("envmaker.cli._RUNS_ROOT", tmp_path)
    result = _RUNNER.invoke(app, ["author", "init", "a mossy quarry", "--seed", "4"])
    assert result.exit_code == 0
    assert "AGENT-DRIVEN AUTHORING WORKFLOW" in result.output
    assert "def build_environment() -> EnvironmentModel" in result.output
    run_dirs = list(tmp_path.glob("author-*"))
    assert len(run_dirs) == 1

    step = _RUNNER.invoke(app, ["author", "step", str(run_dirs[0])])
    assert step.exit_code == 1
    assert "starter template" in step.output


def test_step_reports_runtime_unavailable_not_crash(tmp_path: Path) -> None:
    run_dir = tmp_path / "session"
    init_session("village green", 7, run_dir)
    (run_dir / "environment.py").write_text(_DEMO_SOURCE)

    def _broken_factory(rd: Path) -> object:
        raise RuntimeError("godot binary not found at /nowhere — run ...")

    outcome = step_session(run_dir, driver_factory=_broken_factory)
    assert outcome.status == "runtime_unavailable"
    assert outcome.stages.get("scene") is True, "static results must survive"
    code, message, guidance = outcome.signals[0]
    assert code == "harness.godot_unavailable"
    assert "godot binary not found" in message
    assert "xvfb" in guidance


def test_get_godot_asset_matrix() -> None:
    import sys

    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    try:
        from get_godot import build_asset_name, expected_binary
    finally:
        sys.path.pop(0)

    assert build_asset_name("Darwin", "arm64").endswith("macos.universal.zip")
    assert build_asset_name("Linux", "x86_64").endswith("linux.x86_64.zip")
    assert build_asset_name("Linux", "aarch64").endswith("linux.arm64.zip")
    assert build_asset_name("Windows", "AMD64").endswith("win64.exe.zip")
    assert expected_binary("Darwin").name == "Godot"
    assert expected_binary("Linux").name == "godot"
    assert expected_binary("Windows").name == "godot.exe"


def test_agent_entry_docs_exist() -> None:
    claude_md = " ".join((_REPO_ROOT / "CLAUDE.md").read_text().split())
    agents_md = " ".join((_REPO_ROOT / "AGENTS.md").read_text().split())
    for needle in ("author init", "get_godot.py", "Never modify the harness"):
        assert needle in claude_md
    assert "author init" in agents_md and "get_godot.py" in agents_md


def test_step_snapshots_distinct_revisions(tmp_path: Path) -> None:
    run_dir = tmp_path / "session"
    init_session("revision trail", 7, run_dir)
    bad_v1 = "import os\nenvironment = None\n"
    bad_v2 = "import sys\nenvironment = None\n"

    (run_dir / "environment.py").write_text(bad_v1)
    step_session(run_dir)
    (run_dir / "environment.py").write_text(bad_v1)  # unchanged re-step
    step_session(run_dir)
    (run_dir / "environment.py").write_text(bad_v2)
    step_session(run_dir)

    revisions = sorted((run_dir / "revisions").glob("rev-*.py"))
    assert [p.name for p in revisions] == ["rev-1.py", "rev-2.py"]
    assert revisions[0].read_text() == bad_v1
    assert revisions[1].read_text() == bad_v2
    events = [
        json.loads(line)
        for line in (run_dir / "runlog.jsonl").read_text().splitlines()
    ]
    assert sum(1 for e in events if e.get("kind") == "revision") == 2
