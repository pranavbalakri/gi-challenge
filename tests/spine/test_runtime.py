"""End-to-end coverage for EnvMaker's minimal playable spine."""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image

from envmaker.core.contracts import ArtifactStore
from envmaker.core.episode import NavigationProbe, TerminalReason
from envmaker.core.scene_spec import CandidateScene


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPINE = _REPO_ROOT / "examples/spine/candidate-scene.json"


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


def _load_spine() -> CandidateScene:
    return CandidateScene.model_validate(json.loads(_SPINE.read_text()))


def _probe() -> NavigationProbe:
    return NavigationProbe(
        target_landmark_id="obelisk",
        success_radius_m=1.5,
        max_ticks=900,
        action_repeat=1,
        allowed_connector_types=(),
        stuck_timeout_ticks=120,
        terminal_reasons=(TerminalReason.ARRIVED, TerminalReason.TIMEOUT),
    )


def test_spine_candidate_fingerprint() -> None:
    candidate = _load_spine()

    assert (
        candidate.candidate_fingerprint
        == "e36306c606663b88ea039c137bf00dced42288c753f5003842dc6f0ef3942816"
    )


def test_minimal_slice_headless(tmp_path: Path) -> None:
    _require_godot_user_dir()
    from envmaker.runtime import RenderUnavailableError, RuntimeDriver

    driver = RuntimeDriver(run_dir=tmp_path, session_id="spine-headless")
    try:
        driver.start()
        loaded = driver.load_candidate(_load_spine())
        assert loaded.ok is True
        assert loaded.payload["status"] == "candidate_loaded"
        assert loaded.payload["nodes"] == 10

        driver.wait_navigation_ready(30.0)
        episode = driver.navigate(_probe())
        euclidean = math.hypot(32.0, 32.0)
        assert episode.terminal_reason is TerminalReason.ARRIVED
        assert episode.ticks_used < 900
        assert driver.last_planned_path_length_m >= 1.15 * euclidean
        assert (
            abs(episode.path_length_m - driver.last_planned_path_length_m)
            <= 0.15 * driver.last_planned_path_length_m
        )

        first = driver.snapshot()
        second = driver.snapshot()
        assert second.tick_id > first.tick_id

        with pytest.raises(RenderUnavailableError):
            driver.render("isometric")
    finally:
        exit_code = driver.close()

    assert exit_code == 0


def test_minimal_slice_windowed_render(tmp_path: Path) -> None:
    _require_godot_user_dir()
    from envmaker.runtime import RuntimeDriver

    driver = RuntimeDriver(
        run_dir=tmp_path,
        session_id="spine-windowed",
        windowed=True,
    )
    try:
        driver.start()
        loaded = driver.load_candidate(_load_spine())
        assert loaded.ok is True
        driver.wait_navigation_ready(30.0)

        episode = driver.navigate(_probe())
        assert episode.terminal_reason is TerminalReason.ARRIVED

        iso_ref = driver.render("isometric")
        top_ref = driver.render("topdown")
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
        snow_hits = 0
        for y in range(0, image.height, 2):
            for x in range(0, image.width, 2):
                red, green, blue = image.getpixel((x, y))
                if (
                    abs(red - 217) < 40
                    and abs(green - 51) < 40
                    and abs(blue - 140) < 45
                ):
                    agent_hits += 1
                if (
                    abs(red - 232) < 20
                    and abs(green - 236) < 20
                    and abs(blue - 241) < 20
                ):
                    snow_hits += 1

        assert agent_hits > 0
        assert snow_hits > 0
    finally:
        exit_code = driver.close()

    assert exit_code == 0
