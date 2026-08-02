"""Unit tests for RuntimeDriver windowed argv assembly (no Godot spawn)."""

from __future__ import annotations

from pathlib import Path

import pytest

from envmaker.runtime import RuntimeDriver, RuntimeDriverError, windowed_extra_args


def test_windowed_extra_args_default_resolution() -> None:
    assert windowed_extra_args(resolution="320x180", env={}) == (
        "--resolution",
        "320x180",
        "--position",
        "4000,4000",
    )


def test_windowed_extra_args_env_overrides_param() -> None:
    assert windowed_extra_args(
        resolution="320x180",
        env={"ENVMAKER_RESOLUTION": "640x360"},
    ) == (
        "--resolution",
        "640x360",
        "--position",
        "4000,4000",
    )


def test_windowed_extra_args_window_args_wholesale_override() -> None:
    override = ("--resolution", "1280x720", "--position", "100,100")
    assert (
        windowed_extra_args(
            resolution="320x180",
            window_args=override,
            env={"ENVMAKER_RESOLUTION": "640x360"},
        )
        == override
    )


def test_windowed_extra_args_rejects_invalid_resolution() -> None:
    with pytest.raises(RuntimeDriverError, match="invalid resolution"):
        windowed_extra_args(resolution="hd", env={})
    with pytest.raises(RuntimeDriverError, match="invalid resolution"):
        windowed_extra_args(
            resolution="320x180",
            env={"ENVMAKER_RESOLUTION": "not-a-res"},
        )


def test_runtime_driver_rejects_invalid_resolution_param(tmp_path: Path) -> None:
    with pytest.raises(RuntimeDriverError, match="invalid resolution"):
        RuntimeDriver(
            run_dir=tmp_path,
            session_id="unit-res",
            resolution="abc",
        )


def test_runtime_driver_start_uses_layered_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, **kwargs: object) -> None:
            captured["extra_args"] = kwargs.get("extra_args")

        def start(self) -> None:
            pass

        def wait_closed(self, timeout: float = 15.0) -> int:
            del timeout
            return 0

        def terminate(self) -> int:
            return 0

    class _FakeServer:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def listen(self) -> tuple[str, int]:
            return ("127.0.0.1", 9)

        def accept(self, timeout: float = 30.0) -> object:
            del timeout
            return object()

        def close(self) -> None:
            return None

    import envmaker.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "GodotProcess", _FakeProcess)
    monkeypatch.setattr(runtime_mod, "BridgeServer", _FakeServer)
    monkeypatch.setattr(
        runtime_mod, "resolve_godot_binary", lambda: Path("/usr/bin/true")
    )
    monkeypatch.setenv("ENVMAKER_RESOLUTION", "800x600")

    driver = RuntimeDriver(
        run_dir=tmp_path,
        session_id="unit-layered",
        windowed=True,
        resolution="320x180",
    )
    driver.start()
    assert captured["extra_args"] == (
        "--resolution",
        "800x600",
        "--position",
        "4000,4000",
    )
    driver.close()


def test_runtime_driver_window_args_beat_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    class _FakeProcess:
        def __init__(self, **kwargs: object) -> None:
            captured["extra_args"] = kwargs.get("extra_args")

        def start(self) -> None:
            pass

        def wait_closed(self, timeout: float = 15.0) -> int:
            del timeout
            return 0

        def terminate(self) -> int:
            return 0

    class _FakeServer:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def listen(self) -> tuple[str, int]:
            return ("127.0.0.1", 9)

        def accept(self, timeout: float = 30.0) -> object:
            del timeout
            return object()

        def close(self) -> None:
            return None

    import envmaker.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "GodotProcess", _FakeProcess)
    monkeypatch.setattr(runtime_mod, "BridgeServer", _FakeServer)
    monkeypatch.setattr(
        runtime_mod, "resolve_godot_binary", lambda: Path("/usr/bin/true")
    )
    monkeypatch.setenv("ENVMAKER_RESOLUTION", "800x600")

    override = ("--resolution", "1280x720", "--position", "100,100")
    driver = RuntimeDriver(
        run_dir=tmp_path,
        session_id="unit-override",
        windowed=True,
        window_args=override,
        resolution="320x180",
    )
    driver.start()
    assert captured["extra_args"] == override
    driver.close()
