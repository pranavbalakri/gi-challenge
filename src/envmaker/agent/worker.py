"""Fault-contained execution of generated EnvMaker environment programs."""

from __future__ import annotations

import ast as _ast
import hashlib as _hashlib
import math as _math
import resource as _resource
import shutil as _shutil
import signal as _signal
import subprocess as _subprocess
import sys as _sys
import tempfile as _tempfile
import time as _time
from pathlib import Path as _Path

import envmaker as _envmaker
from envmaker.core.artifacts import canonical_fingerprint as _canonical_fingerprint
from envmaker.core.model import EnvironmentModel as _EnvironmentModel
from envmaker.core.program import ResourceLimits as _ResourceLimits
from envmaker.core.program import WorkerExecution as _WorkerExecution
from envmaker.core.program import WorkerExitReason as _WorkerExitReason
from envmaker.runlog import _redact as _redact_value
from envmaker.sdk import SDK_VERSION as _SDK_VERSION

__all__ = ["run_generated_program"]

_ALLOWED_MODULES = frozenset({"math", "envmaker.sdk"})
_FORBIDDEN_NAMES = frozenset(
    {
        "exec",
        "eval",
        "open",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "breakpoint",
        "input",
        "exit",
        "quit",
    }
)
_MODEL_PREFIX = "ENVMAKER_MODEL:"
# @@SRC_DIR@@ is replaced with repr(_src_dir()) at write time: the child runs
# under `python -I`, which ignores PYTHONPATH, so the repo src dir must be
# injected into sys.path inside the runner itself.
_RUNNER_SOURCE = """\
import sys

sys.path.insert(0, @@SRC_DIR@@)

import traceback

from envmaker.core.model import EnvironmentModel


def main() -> None:
    try:
        with open("program.py", encoding="utf-8") as handle:
            source = handle.read()
        namespace = {"__name__": "env_program"}
        exec(compile(source, "program.py", "exec"), namespace)
        environment = namespace.get("environment")
        if not isinstance(environment, EnvironmentModel):
            raise TypeError("environment must be an EnvironmentModel")
        sys.stdout.write("ENVMAKER_MODEL:" + environment.model_dump_json() + "\\n")
        sys.stdout.flush()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
"""


def _blake2b256(data: bytes) -> str:
    return _hashlib.blake2b(data, digest_size=32).hexdigest()


def _src_dir() -> str:
    return str(_Path(_envmaker.__file__).resolve().parent.parent)


def _static_gate_rejects(source: str) -> bool:
    try:
        tree = _ast.parse(source)
    except SyntaxError:
        return True

    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                if alias.name not in _ALLOWED_MODULES:
                    return True
        elif isinstance(node, _ast.ImportFrom):
            if node.level and node.level > 0:
                return True
            if node.module not in _ALLOWED_MODULES:
                return True
        elif isinstance(node, _ast.Name):
            if node.id in _FORBIDDEN_NAMES or node.id.startswith("__"):
                return True
        elif isinstance(node, _ast.Attribute):
            if node.attr in _FORBIDDEN_NAMES or node.attr.startswith("__"):
                return True
    return False


def _make_preexec(limits: _ResourceLimits):
    cpu_limit = int(_math.ceil(limits.cpu_seconds))
    memory_bytes = int(limits.memory_mb) * 1024 * 1024

    def _apply_limits() -> None:
        _resource.setrlimit(_resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        try:
            _resource.setrlimit(_resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, OSError):
            pass

    return _apply_limits


def _rejected_execution(
    *,
    program_fingerprint: str,
    limits: _ResourceLimits,
    duration_seconds: float,
) -> _WorkerExecution:
    empty = _blake2b256(b"")
    return _WorkerExecution(
        program_fingerprint=program_fingerprint,
        limits=limits,
        exit_reason=_WorkerExitReason.REJECTED_IMPORTS,
        duration_seconds=duration_seconds,
        stdout_blake2b256=empty,
        stderr_blake2b256=empty,
        quarantined=True,
    )


def _parse_model(stdout: bytes) -> _EnvironmentModel | None:
    for line in stdout.splitlines():
        if not line.startswith(_MODEL_PREFIX.encode("utf-8")):
            continue
        payload = line[len(_MODEL_PREFIX) :]
        try:
            return _EnvironmentModel.model_validate_json(payload)
        except Exception:
            continue
    return None


def _stderr_tail(stderr: bytes, *, chars: int) -> str:
    if chars <= 0 or not stderr:
        return ""
    decoded = stderr.decode("utf-8", errors="replace")
    # Redact BEFORE truncating: slicing first can cut a secret's prefix off
    # and leave an unmatchable key body in the tail.
    redacted = _redact_value(decoded)
    text = redacted if isinstance(redacted, str) else str(redacted)
    return text[-chars:] if len(text) > chars else text


def run_generated_program(
    source: str,
    *,
    limits: _ResourceLimits,
    stderr_tail_chars: int = 400,
) -> tuple[_WorkerExecution, _EnvironmentModel | None, str]:
    """Execute one generated program under the fault-containment worker."""

    program_fingerprint = _canonical_fingerprint(
        {"source": source, "sdk_version": _SDK_VERSION}
    )
    started = _time.monotonic()

    if _static_gate_rejects(source):
        duration = max(0.0, _time.monotonic() - started)
        return (
            _rejected_execution(
                program_fingerprint=program_fingerprint,
                limits=limits,
                duration_seconds=duration,
            ),
            None,
            "",
        )

    temp_dir: str | None = None
    stdout = b""
    stderr = b""
    exit_reason = _WorkerExitReason.CRASH
    model: _EnvironmentModel | None = None

    try:
        temp_dir = _tempfile.mkdtemp(prefix="envmaker-worker-")
        program_path = _Path(temp_dir) / "program.py"
        runner_path = _Path(temp_dir) / "_runner.py"
        program_path.write_text(source, encoding="utf-8")
        runner_path.write_text(
            _RUNNER_SOURCE.replace("@@SRC_DIR@@", repr(_src_dir())),
            encoding="utf-8",
        )

        env = {
            "HOME": temp_dir,
            "PATH": "",
        }
        process = _subprocess.Popen(
            [_sys.executable, "-I", "_runner.py"],
            cwd=temp_dir,
            env=env,
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
            preexec_fn=_make_preexec(limits),
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=limits.wall_seconds)
        except _subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            stdout, stderr = process.communicate()

        if timed_out:
            exit_reason = _WorkerExitReason.TIMEOUT
        elif (
            len(stdout) > limits.output_bytes or len(stderr) > limits.output_bytes
        ):
            exit_reason = _WorkerExitReason.RESOURCE_LIMIT
        elif process.returncode == -int(_signal.SIGXCPU):
            exit_reason = _WorkerExitReason.RESOURCE_LIMIT
        elif process.returncode != 0:
            exit_reason = _WorkerExitReason.CRASH
        else:
            model = _parse_model(stdout)
            if model is None:
                exit_reason = _WorkerExitReason.CRASH
            else:
                exit_reason = _WorkerExitReason.COMPLETED
    except Exception:
        exit_reason = _WorkerExitReason.CRASH
        model = None
    finally:
        duration_seconds = max(0.0, _time.monotonic() - started)
        if temp_dir is not None:
            _shutil.rmtree(temp_dir, ignore_errors=True)

    if exit_reason != _WorkerExitReason.COMPLETED:
        model = None
    execution = _WorkerExecution(
        program_fingerprint=program_fingerprint,
        limits=limits,
        exit_reason=exit_reason,
        duration_seconds=duration_seconds,
        stdout_blake2b256=_blake2b256(stdout),
        stderr_blake2b256=_blake2b256(stderr),
        quarantined=exit_reason != _WorkerExitReason.COMPLETED,
    )
    return execution, model, _stderr_tail(stderr, chars=stderr_tail_chars)
