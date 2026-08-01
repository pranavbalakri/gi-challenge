"""Live end-to-end coverage for the checked-in village-green fixture."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from envmaker.agent.worker import run_generated_program
from envmaker.core.contracts import ArtifactStore
from envmaker.core.episode import NavigationProbe, TerminalReason
from envmaker.core.program import ResourceLimits, WorkerExitReason
from envmaker.godot_bridge.process import resolve_godot_binary
from envmaker.sdk import compile_environment_model


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO = _REPO_ROOT / "examples/demo/environment.py"
_GODOT_BIN = resolve_godot_binary()


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


def _require_godot_user_dir(probe: Callable[[], bool] = _godot_user_dir_writable) -> None:
    if not probe():
        pytest.skip(
            "Godot user-data dir is not writable (sandboxed shell?); "
            "live Godot tests require an unsandboxed run"
        )


def _require_godot_binary() -> None:
    if not _GODOT_BIN.is_file():
        pytest.skip(
            f"Godot binary not found: {_GODOT_BIN} (set GODOT_BIN to override)"
        )


def _probe() -> NavigationProbe:
    return NavigationProbe(
        target_landmark_id="obelisk_goal.0",
        success_radius_m=1.5,
        max_ticks=2400,
        action_repeat=1,
        allowed_connector_types=(),
        stuck_timeout_ticks=180,
        terminal_reasons=(TerminalReason.ARRIVED, TerminalReason.TIMEOUT),
    )


def test_demo_fixture_live_traversal(tmp_path: Path) -> None:
    _require_godot_binary()
    _require_godot_user_dir()
    from envmaker.runtime import RuntimeDriver

    source = _DEMO.read_text(encoding="utf-8")
    execution, model, _stderr = run_generated_program(
        source,
        limits=ResourceLimits(
            cpu_seconds=30.0,
            memory_mb=512,
            output_bytes=1_048_576,
            wall_seconds=60.0,
        ),
    )
    assert execution.exit_reason is WorkerExitReason.COMPLETED
    assert model is not None

    candidate = compile_environment_model(model)

    driver = RuntimeDriver(
        run_dir=tmp_path,
        session_id="demo-fixture",
        windowed=True,
    )
    try:
        driver.start()
        loaded = driver.load_candidate(candidate)
        assert loaded.ok is True
        driver.wait_navigation_ready(30.0)

        episode = driver.navigate(_probe())
        assert episode.terminal_reason is TerminalReason.ARRIVED
        assert episode.ticks_used < 2400

        iso_ref = driver.render("isometric")
        top_ref = driver.render("topdown")
        assert iso_ref.byte_count > 0
        assert top_ref.byte_count > 0
        assert iso_ref.blake2b256 != top_ref.blake2b256

        store = ArtifactStore(tmp_path)
        resolved_paths = []
        for ref in (iso_ref, top_ref):
            resolved = store.resolve_verified(ref)
            assert resolved.is_file()
            assert resolved.stat().st_size == ref.byte_count
            resolved_paths.append(resolved)

        with Image.open(resolved_paths[1]) as opened:
            image = opened.convert("RGB")

        agent_hits = 0
        for y in range(0, image.height, 2):
            for x in range(0, image.width, 2):
                red, green, blue = image.getpixel((x, y))
                if (
                    abs(red - 217) < 40
                    and abs(green - 51) < 40
                    and abs(blue - 140) < 45
                ):
                    agent_hits += 1

        assert agent_hits > 0
    finally:
        exit_code = driver.close()

    assert exit_code == 0
