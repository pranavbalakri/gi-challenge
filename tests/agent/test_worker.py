"""Fault-contained worker coverage for generated environment programs."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from envmaker.core.artifacts import canonical_fingerprint
from envmaker.core.model import EnvironmentModel
from envmaker.core.program import ResourceLimits, WorkerExitReason
from envmaker.sdk import SDK_VERSION, compile_environment_model
from envmaker.agent.worker import run_generated_program
import envmaker.agent.worker as worker_mod


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEMO_SOURCE = _REPO_ROOT / "examples" / "demo" / "environment.py"
_EMPTY_HASH = hashlib.blake2b(b"", digest_size=32).hexdigest()


def _limits(
    *,
    cpu_seconds: float = 5.0,
    memory_mb: int = 256,
    output_bytes: int = 65536,
    wall_seconds: float = 10.0,
) -> ResourceLimits:
    return ResourceLimits(
        cpu_seconds=cpu_seconds,
        memory_mb=memory_mb,
        output_bytes=output_bytes,
        wall_seconds=wall_seconds,
    )


_HAPPY_SOURCE = """\
from envmaker.sdk import EnvironmentBuilder, Polygon2D

def build_environment() -> object:
    return (
        EnvironmentBuilder("tiny", seed=1)
        .ground(
            "ground",
            footprint=Polygon2D([(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]),
            material="grass",
        )
        .spawn("hero", position=(0.0, 0.0))
        .camera(orthographic_size=12.0)
        .freeze()
    )

environment = build_environment()
"""


def test_worker_happy_path_returns_model() -> None:
    execution, model, _stderr = run_generated_program(_HAPPY_SOURCE, limits=_limits())
    expected_fp = canonical_fingerprint(
        {"source": _HAPPY_SOURCE, "sdk_version": SDK_VERSION}
    )
    assert execution.exit_reason is WorkerExitReason.COMPLETED
    assert execution.quarantined is False
    assert execution.program_fingerprint == expected_fp
    assert isinstance(model, EnvironmentModel)
    assert model.name == "tiny"


def test_worker_rejects_os_import() -> None:
    source = "import os\n" + _HAPPY_SOURCE
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.REJECTED_IMPORTS
    assert execution.quarantined is True
    assert model is None
    assert execution.stdout_blake2b256 == _EMPTY_HASH
    assert execution.stderr_blake2b256 == _EMPTY_HASH


def test_worker_rejects_open_call() -> None:
    source = (
        "def build_environment():\n"
        "    open('/etc/passwd')\n"
        "    return None\n"
        "environment = build_environment()\n"
    )
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.REJECTED_IMPORTS
    assert execution.quarantined is True
    assert model is None


def test_worker_rejects_exec_call() -> None:
    source = (
        "def build_environment():\n"
        "    exec('x=1')\n"
        "    return None\n"
        "environment = build_environment()\n"
    )
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.REJECTED_IMPORTS
    assert execution.quarantined is True
    assert model is None


def test_worker_rejects_dunder_access() -> None:
    source = (
        "def build_environment():\n"
        "    return ().__class__\n"
        "environment = build_environment()\n"
    )
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.REJECTED_IMPORTS
    assert execution.quarantined is True
    assert model is None


def test_worker_rejects_core_import() -> None:
    source = (
        "from envmaker.core.model import EnvironmentModel\n"
        "environment = None\n"
    )
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.REJECTED_IMPORTS
    assert execution.quarantined is True
    assert model is None


def test_worker_rejects_syntax_error() -> None:
    execution, model, _stderr = run_generated_program("def build_environment(:\n", limits=_limits())
    assert execution.exit_reason is WorkerExitReason.REJECTED_IMPORTS
    assert execution.quarantined is True
    assert model is None


def test_worker_timeout() -> None:
    source = (
        "def build_environment():\n"
        "    while True:\n"
        "        pass\n"
        "environment = build_environment()\n"
    )
    execution, model, _stderr = run_generated_program(
        source, limits=_limits(wall_seconds=1.5, cpu_seconds=5.0)
    )
    assert execution.exit_reason is WorkerExitReason.TIMEOUT
    assert execution.quarantined is True
    assert execution.duration_seconds >= 1.0
    assert model is None


def test_worker_output_cap() -> None:
    source = (
        "def build_environment():\n"
        "    print('x' * 2000)\n"
        "    return None\n"
        "environment = build_environment()\n"
    )
    execution, model, _stderr = run_generated_program(
        source, limits=_limits(output_bytes=1024)
    )
    assert execution.exit_reason is WorkerExitReason.RESOURCE_LIMIT
    assert execution.quarantined is True
    assert model is None


def test_worker_crash_on_runtime_error() -> None:
    source = (
        "def build_environment():\n"
        "    raise RuntimeError('boom')\n"
        "environment = build_environment()\n"
    )
    execution, model, stderr_tail = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.CRASH
    assert execution.quarantined is True
    assert model is None
    assert execution.stderr_blake2b256 != _EMPTY_HASH
    assert "boom" in stderr_tail


def test_worker_crash_missing_environment() -> None:
    source = (
        "def build_environment():\n"
        "    return None\n"
        "result = build_environment()\n"
    )
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.CRASH
    assert execution.quarantined is True
    assert model is None


def test_worker_fingerprint_determinism() -> None:
    first, _, _ = run_generated_program(_HAPPY_SOURCE, limits=_limits())
    second, _, _ = run_generated_program(_HAPPY_SOURCE, limits=_limits())
    assert first.program_fingerprint == second.program_fingerprint
    assert first.program_fingerprint == canonical_fingerprint(
        {"source": _HAPPY_SOURCE, "sdk_version": SDK_VERSION}
    )


def test_demo_fixture_worker_compile_and_corridor() -> None:
    source = _DEMO_SOURCE.read_text(encoding="utf-8")
    execution, model, _stderr = run_generated_program(source, limits=_limits(wall_seconds=30.0))
    assert execution.exit_reason is WorkerExitReason.COMPLETED
    assert model is not None

    first = compile_environment_model(model)
    second = compile_environment_model(model)
    assert first.candidate_fingerprint == second.candidate_fingerprint

    corridor_min_x, corridor_max_x = -3.3, 1.3
    corridor_min_z, corridor_max_z = -3.3, 3.3
    for node in first.scene.nodes:
        if not node.node_id.startswith("pines."):
            continue
        x = node.transform.origin.x
        z = node.transform.origin.z
        assert not (
            corridor_min_x <= x <= corridor_max_x
            and corridor_min_z <= z <= corridor_max_z
        ), f"pine node {node.node_id} blocks moat gap at ({x}, {z})"

    obelisk_nodes = [
        node
        for node in first.scene.nodes
        if node.node_id.startswith("obelisk_goal")
    ]
    assert len(obelisk_nodes) >= 1

    spawn = next(node for node in first.scene.nodes if node.node_id == "wanderer")
    assert spawn.transform.origin.x == -14.0
    assert spawn.transform.origin.z == -14.0


def test_marker_shadow_line_does_not_mask_real_model() -> None:
    source = _HAPPY_SOURCE.replace(
        "environment = build_environment()",
        'print("ENVMAKER_MODEL:not-json")\nenvironment = build_environment()',
    )
    execution, model, _stderr = run_generated_program(source, limits=_limits())
    assert execution.exit_reason is WorkerExitReason.COMPLETED
    assert model is not None


def test_worker_without_resource_module_still_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_mod, "_resource", None)
    assert worker_mod._make_preexec(_limits()) is None
    execution, model, _stderr = run_generated_program(
        _HAPPY_SOURCE, limits=_limits()
    )
    assert execution.exit_reason is WorkerExitReason.COMPLETED
    assert model is not None


def test_sigxcpu_absent_negative_returncode_not_resource_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(worker_mod._signal, "SIGXCPU", raising=False)
    assert worker_mod._is_cpu_limit_returncode(-24) is False


def test_runner_injects_src_dir_despite_isolated_mode() -> None:
    import ast

    from envmaker.agent.worker import _RUNNER_SOURCE, _src_dir

    rendered = _RUNNER_SOURCE.replace("@@SRC_DIR@@", repr(_src_dir()))
    tree = ast.parse(rendered)
    assert "sys.path.insert(0, " in rendered
    assert _src_dir() in rendered
    assert isinstance(tree, ast.Module)
