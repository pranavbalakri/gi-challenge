"""Keyless monkeypatch coverage for Godot path/env portability helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from envmaker.godot_bridge import process as process_mod
from envmaker.godot_bridge.process import godot_user_data_dir, resolve_godot_binary


def test_godot_user_data_dir_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "darwin")
    assert godot_user_data_dir() == (
        Path.home() / "Library" / "Application Support" / "Godot"
    )


def test_godot_user_data_dir_win32_with_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    assert godot_user_data_dir() == appdata / "Godot"


def test_godot_user_data_dir_win32_without_appdata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    monkeypatch.delenv("APPDATA", raising=False)
    assert godot_user_data_dir() == (
        Path.home() / "AppData" / "Roaming" / "Godot"
    )


def test_godot_user_data_dir_linux_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "linux")
    xdg = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert godot_user_data_dir() == xdg / "godot"


def test_godot_user_data_dir_linux_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert godot_user_data_dir() == Path.home() / ".local" / "share" / "godot"


def test_resolve_godot_binary_godot_bin_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GODOT_BIN", str(tmp_path / "custom-godot"))
    monkeypatch.setattr(process_mod.sys, "platform", "linux")
    tools = tmp_path / "tools" / "godot"
    tools.mkdir(parents=True)
    (tools / "godot").write_text("#!/bin/sh\n")
    monkeypatch.setattr(process_mod, "_TOOLS_GODOT", tools)
    assert resolve_godot_binary() == tmp_path / "custom-godot"


def test_resolve_godot_binary_first_existing_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(process_mod.sys, "platform", "linux")
    tools = tmp_path / "tools" / "godot"
    tools.mkdir(parents=True)
    secondary = tools / "godot4"
    secondary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(process_mod, "_TOOLS_GODOT", tools)
    assert resolve_godot_binary() == secondary


def test_resolve_godot_binary_platform_primary_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    tools = tmp_path / "tools" / "godot"
    tools.mkdir(parents=True)
    monkeypatch.setattr(process_mod, "_TOOLS_GODOT", tools)
    assert resolve_godot_binary() == tools / "godot.exe"


def test_resolve_godot_binary_darwin_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GODOT_BIN", raising=False)
    monkeypatch.setattr(process_mod.sys, "platform", "darwin")
    tools = tmp_path / "tools" / "godot"
    primary = tools / "Godot.app" / "Contents" / "MacOS" / "Godot"
    primary.parent.mkdir(parents=True)
    primary.write_text("#!/bin/sh\n")
    monkeypatch.setattr(process_mod, "_TOOLS_GODOT", tools)
    assert resolve_godot_binary() == primary


def test_sanitized_env_names_win32_includes_system_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    names = process_mod._sanitized_env_names()
    for key in ("SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        assert key in names
    monkeypatch.setattr(process_mod.sys, "platform", "darwin")
    posix_names = process_mod._sanitized_env_names()
    assert "SYSTEMROOT" not in posix_names
    assert posix_names == ("PATH", "HOME", "USER", "TMPDIR")
