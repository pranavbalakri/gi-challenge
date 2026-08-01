from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path

import pytest

from envmaker.godot_bridge.process import GodotProcess, ProcessError


def _write_child(tmp_path: Path, body: str) -> Path:
    child_path = tmp_path / "child.py"
    child_path.write_text(f"#!/usr/bin/env python3\n{body}")
    child_path.chmod(0o755)
    return child_path


def test_start_missing_binary(tmp_path: Path) -> None:
    process = GodotProcess(
        godot_bin=tmp_path / "nope",
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs",
    )

    with pytest.raises(ProcessError, match="godot binary not found"):
        process.start()


def test_start_and_clean_exit(tmp_path: Path) -> None:
    child = _write_child(
        tmp_path,
        """\
import os

host = os.environ["ENVMAKER_BRIDGE_HOST"]
port = os.environ["ENVMAKER_BRIDGE_PORT"]
session = os.environ["ENVMAKER_BRIDGE_SESSION"]
token = os.environ["ENVMAKER_BRIDGE_TOKEN"]
if all((host, port, session, token)):
    print(f"ENV_OK {session}")
""",
    )
    process = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs",
    )

    process.start()

    assert process.wait_closed(10.0) == 0
    assert process.stdout_path.read_text().strip() == "ENV_OK sess-1"
    assert process.running is False


def test_env_sanitized_and_token_not_in_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVMAKER_TEST_SECRET", "leakme")
    child = _write_child(
        tmp_path,
        """\
import json
import os
import sys

print(json.dumps({
    "argv": sys.argv,
    "secret": os.environ.get("ENVMAKER_TEST_SECRET"),
    "token": os.environ.get("ENVMAKER_BRIDGE_TOKEN"),
}))
""",
    )
    token = "token-must-not-appear-in-argv"
    process = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token=token,
        log_dir=tmp_path / "logs",
    )

    process.start()
    assert process.wait_closed(10.0) == 0

    payload = json.loads(process.stdout_path.read_text())
    assert payload["secret"] is None
    assert payload["token"] == token
    assert all(token not in arg for arg in payload["argv"])


def test_terminate_escalation(tmp_path: Path) -> None:
    child = _write_child(
        tmp_path,
        """\
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("READY", flush=True)
time.sleep(60)
""",
    )
    process = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs",
    )
    process.start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.stdout_path.read_text() == "READY\n":
            break
        time.sleep(0.01)
    else:
        process.terminate(grace_seconds=0.5)
        pytest.fail("child did not become ready")

    started = time.monotonic()
    exit_code = process.terminate(grace_seconds=0.5)
    elapsed = time.monotonic() - started

    assert exit_code == -signal.SIGKILL
    assert process.running is False
    assert elapsed < 5.0


def test_wait_timeout(tmp_path: Path) -> None:
    child = _write_child(
        tmp_path,
        """\
import time

time.sleep(60)
""",
    )
    process = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs",
    )
    process.start()

    with pytest.raises(ProcessError, match="process did not exit"):
        process.wait_closed(0.3)

    process.terminate()
    assert process.running is False


def test_crash_exit_code(tmp_path: Path) -> None:
    child = _write_child(
        tmp_path,
        """\
import sys

sys.exit(3)
""",
    )
    process = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs",
    )

    process.start()

    assert process.wait_closed(10.0) == 3


from envmaker.core.contracts import MessageType
from envmaker.godot_bridge.client import BridgeProtocolError, BridgeServer


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GODOT_BIN = _REPO_ROOT / "tools/godot/Godot.app/Contents/MacOS/Godot"
_GODOT_PROJECT = _REPO_ROOT / "godot"


def test_real_godot_handshake_and_serve(tmp_path: Path) -> None:
    _require_godot_user_dir()
    server = BridgeServer(session_id="run-int", token="tok-int-1")
    host, port = server.listen()
    proc = GodotProcess(
        godot_bin=_GODOT_BIN,
        project_path=_GODOT_PROJECT,
        host=host,
        port=port,
        session_id="run-int",
        token="tok-int-1",
        log_dir=tmp_path,
    )

    try:
        proc.start()
        session = server.accept(timeout=30.0)

        navigation = session.request(MessageType.NAVIGATION_STATUS)
        assert navigation.ok is True
        assert navigation.payload == {"state": "unloaded"}

        loaded = session.request(MessageType.LOAD_CANDIDATE, {})
        assert loaded.ok is True
        assert loaded.payload == {"status": "empty_candidate_loaded"}

        unsupported = session.request(
            MessageType.LOAD_CANDIDATE,
            {"unexpected": 1},
        )
        assert unsupported.ok is False
        assert unsupported.error is not None
        assert unsupported.error.code == "bridge.unsupported_candidate"

        step = session.request(MessageType.STEP, tick_id=0)
        assert step.ok is False
        assert step.error is not None
        assert step.error.code == "bridge.not_implemented"

        navigation_again = session.request(MessageType.NAVIGATION_STATUS)
        assert navigation_again.ok is True

        session.close()
        assert proc.wait_closed(15.0) == 0
    finally:
        try:
            proc.terminate()
        finally:
            server.close()


def test_real_godot_token_rejection(tmp_path: Path) -> None:
    _require_godot_user_dir()
    server = BridgeServer(session_id="run-int", token="tok-right")
    host, port = server.listen()
    proc = GodotProcess(
        godot_bin=_GODOT_BIN,
        project_path=_GODOT_PROJECT,
        host=host,
        port=port,
        session_id="run-int",
        token="tok-wrong",
        log_dir=tmp_path,
    )

    try:
        proc.start()
        with pytest.raises(BridgeProtocolError, match="token mismatch"):
            server.accept(timeout=30.0)
        assert proc.wait_closed(15.0) == 3
    finally:
        try:
            proc.terminate()
        finally:
            server.close()


import tempfile
from collections.abc import Callable


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


def test_godot_user_dir_probe_writable(tmp_path: Path) -> None:
    assert _godot_user_dir_writable(home=tmp_path) is True

    godot_dir = tmp_path / "Library" / "Application Support" / "Godot"
    assert godot_dir.parent.is_dir()
    assert not godot_dir.exists()

    godot_dir.mkdir()
    assert _godot_user_dir_writable(home=tmp_path) is True
    assert list(godot_dir.iterdir()) == []


def test_godot_user_dir_probe_unwritable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission-bit probe requires a non-root user")
    support_dir = tmp_path / "Library" / "Application Support"
    support_dir.mkdir(parents=True)
    support_dir.chmod(0o555)
    try:
        assert _godot_user_dir_writable(home=tmp_path) is False
    finally:
        support_dir.chmod(0o755)


def test_godot_user_dir_guard_skips_when_unwritable() -> None:
    with pytest.raises(pytest.skip.Exception):
        _require_godot_user_dir(probe=lambda: False)

    assert _require_godot_user_dir(probe=lambda: True) is None


def test_run_root_env_passthrough(tmp_path: Path) -> None:
    child = _write_child(
        tmp_path,
        """\
import os

print(os.environ.get("ENVMAKER_BRIDGE_RUN_ROOT", "ABSENT"))
""",
    )
    with_root = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs-a",
        run_root=tmp_path / "run",
    )
    with_root.start()
    assert with_root.wait_closed(10.0) == 0
    assert with_root.stdout_path.read_text().strip() == str(tmp_path / "run")

    without_root = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs-b",
    )
    without_root.start()
    assert without_root.wait_closed(10.0) == 0
    assert without_root.stdout_path.read_text().strip() == "ABSENT"


def test_headless_flag_controls_argv(tmp_path: Path) -> None:
    child = _write_child(
        tmp_path,
        """\
import json
import sys

print(json.dumps(sys.argv[1:]))
""",
    )
    headless = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs-a",
    )
    headless.start()
    assert headless.wait_closed(10.0) == 0
    headless_argv = json.loads(headless.stdout_path.read_text())
    assert headless_argv[0] == "--headless"

    windowed = GodotProcess(
        godot_bin=child,
        project_path=tmp_path,
        host="127.0.0.1",
        port=7654,
        session_id="sess-1",
        token="test-token",
        log_dir=tmp_path / "logs-b",
        headless=False,
    )
    windowed.start()
    assert windowed.wait_closed(10.0) == 0
    windowed_argv = json.loads(windowed.stdout_path.read_text())
    assert "--headless" not in windowed_argv
    assert windowed_argv[0] == "--path"
