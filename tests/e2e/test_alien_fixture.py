"""Alien visual-extension fixture: offline determinism + live traversal."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from envmaker.agent.worker import run_generated_program
from envmaker.core.episode import NavigationProbe, TerminalReason
from envmaker.core.program import ResourceLimits, WorkerExitReason
from envmaker.godot_bridge.process import godot_user_data_dir, resolve_godot_binary
from envmaker.sdk import compile_environment_model


def _require_godot_binary() -> None:
    binary = resolve_godot_binary()
    if not binary.is_file():
        pytest.skip(f"Godot binary not found: {binary} (set GODOT_BIN to override)")


def _require_godot_user_dir() -> None:
    base = godot_user_data_dir()
    probe_dir = base if base.is_dir() else base.parent
    try:
        probe_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=probe_dir):
            pass
    except OSError:
        pytest.skip("Godot user-data dir is not writable (sandboxed shell?)")


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALIEN = _REPO_ROOT / "examples/alien/environment.py"
_LIMITS = ResourceLimits(
    cpu_seconds=30.0,
    memory_mb=512,
    output_bytes=1_048_576,
    wall_seconds=60.0,
)


def _alien_candidate() -> object:
    execution, model, _stderr = run_generated_program(
        _ALIEN.read_text(encoding="utf-8"), limits=_LIMITS
    )
    assert execution.exit_reason is WorkerExitReason.COMPLETED
    assert model is not None
    return compile_environment_model(model)


def test_alien_fixture_compiles_deterministically_offline() -> None:
    first = _alien_candidate()
    second = _alien_candidate()
    assert first.candidate_fingerprint == second.candidate_fingerprint

    materials = first.scene.materials
    assert materials is not None
    assert materials["violet_crystal"].emission_color == "#7b3dff"
    assert materials["biolume_teal"].emission_strength == 1.0
    assert materials["palette.ground"].color == "#4a3572"
    assert materials["palette.accent"].color == "#e568ff"

    presentation = first.scene.presentation
    assert presentation.sky_top == "#12081f"
    assert presentation.sun_energy == 0.4

    cluster = [
        n for n in first.scene.nodes if n.node_id.startswith("cluster_gate_w.")
    ]
    assert len(cluster) == 4
    assert all(n.collider is not None and n.navmesh_contributor for n in cluster)
    tufts = [n for n in first.scene.nodes if n.node_id.startswith("tufts.")]
    assert tufts and all(n.collider is None for n in tufts)
    beacon = [
        n for n in first.scene.nodes if n.node_id.startswith("beacon_spire.")
    ]
    # Landmark role: accent recolors the plinth; explicit customs win.
    assert {n.visual.material for n in beacon} == {
        "violet_crystal",
        "magenta_bloom",
        "palette.accent",
    }


def test_alien_fixture_live_traversal(tmp_path: Path) -> None:
    _require_godot_binary()
    _require_godot_user_dir()
    from envmaker.runtime import RuntimeDriver

    candidate = _alien_candidate()
    driver = RuntimeDriver(
        run_dir=tmp_path,
        session_id="alien-fixture",
        windowed=True,
        hidden=True,
    )
    try:
        driver.start()
        assert driver.load_candidate(candidate).ok is True
        driver.wait_navigation_ready(30.0)

        episode = driver.navigate(
            NavigationProbe(
                target_landmark_id="beacon_spire.0",
                success_radius_m=1.5,
                max_ticks=3600,
                action_repeat=1,
                allowed_connector_types=(),
                stuck_timeout_ticks=240,
                terminal_reasons=(
                    TerminalReason.ARRIVED,
                    TerminalReason.TIMEOUT,
                ),
            )
        )
        assert episode.terminal_reason is TerminalReason.ARRIVED

        iso_ref = driver.render("isometric")
        top_ref = driver.render("topdown")
        assert iso_ref.byte_count > 0
        assert iso_ref.blake2b256 != top_ref.blake2b256
    finally:
        exit_code = driver.close()

    assert exit_code == 0
