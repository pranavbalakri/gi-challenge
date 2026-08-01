from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import BinaryIO

__all__ = [
    "ENV_HOST",
    "ENV_PORT",
    "ENV_RUN_ROOT",
    "ENV_SESSION",
    "ENV_TOKEN",
    "ProcessError",
    "GodotProcess",
]

ENV_HOST: str = "ENVMAKER_BRIDGE_HOST"
ENV_PORT: str = "ENVMAKER_BRIDGE_PORT"
ENV_RUN_ROOT: str = "ENVMAKER_BRIDGE_RUN_ROOT"
ENV_SESSION: str = "ENVMAKER_BRIDGE_SESSION"
ENV_TOKEN: str = "ENVMAKER_BRIDGE_TOKEN"

_SANITIZED_ENV_NAMES = ("PATH", "HOME", "USER", "TMPDIR")


class ProcessError(RuntimeError):
    pass


class GodotProcess:
    def __init__(
        self,
        *,
        godot_bin: Path,
        project_path: Path,
        host: str,
        port: int,
        session_id: str,
        token: str,
        log_dir: Path,
        run_root: Path | None = None,
        extra_args: tuple[str, ...] = (),
        headless: bool = True,
    ) -> None:
        self._godot_bin = godot_bin
        self._project_path = project_path
        self._host = host
        self._port = port
        self._session_id = session_id
        self._token = token
        self._log_dir = log_dir
        self._run_root = run_root
        self._extra_args = extra_args
        self._headless = headless

        self._process: subprocess.Popen[bytes] | None = None
        self._exit_code: int | None = None
        self._stdout_handle: BinaryIO | None = None
        self._stderr_handle: BinaryIO | None = None

    def start(self) -> None:
        if self._process is not None:
            raise ProcessError("already started")
        if not self._godot_bin.is_file() or not os.access(
            self._godot_bin, os.X_OK
        ):
            raise ProcessError("godot binary not found")

        self._log_dir.mkdir(parents=True, exist_ok=True)
        stdout_handle = self.stdout_path.open("wb")
        try:
            stderr_handle = self.stderr_path.open("wb")
        except BaseException:
            stdout_handle.close()
            raise

        argv = [
            str(self._godot_bin),
            *(("--headless",) if self._headless else ()),
            "--path",
            str(self._project_path),
            *self._extra_args,
        ]
        child_env = {
            name: os.environ[name]
            for name in _SANITIZED_ENV_NAMES
            if name in os.environ
        }
        child_env.update(
            {
                ENV_HOST: self._host,
                ENV_PORT: str(self._port),
                ENV_SESSION: self._session_id,
                ENV_TOKEN: self._token,
            }
        )
        if self._run_root is not None:
            child_env[ENV_RUN_ROOT] = str(self._run_root)

        try:
            process = subprocess.Popen(
                argv,
                env=child_env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except BaseException:
            stdout_handle.close()
            stderr_handle.close()
            raise

        self._stdout_handle = stdout_handle
        self._stderr_handle = stderr_handle
        self._process = process

    def wait_closed(self, timeout: float) -> int:
        if self._process is None:
            raise ProcessError("not started")
        if self._exit_code is not None:
            return self._exit_code

        try:
            exit_code = self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ProcessError("process did not exit") from None

        self._record_exit(exit_code)
        return exit_code

    def terminate(self, grace_seconds: float = 5.0) -> int:
        if self._process is None:
            raise ProcessError("not started")
        if self._exit_code is not None:
            return self._exit_code

        exit_code = self._process.poll()
        if exit_code is not None:
            self._record_exit(exit_code)
            return exit_code

        try:
            self._process.terminate()
        except ProcessLookupError:
            pass

        try:
            exit_code = self._process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                self._process.kill()
            except ProcessLookupError:
                pass
            exit_code = self._process.wait()

        self._record_exit(exit_code)
        return exit_code

    @property
    def running(self) -> bool:
        if self._process is None:
            return False
        if self._exit_code is not None:
            return False

        exit_code = self._process.poll()
        if exit_code is None:
            return True
        self._record_exit(exit_code)
        return False

    @property
    def pid(self) -> int | None:
        if self._process is None:
            return None
        return self._process.pid

    @property
    def stdout_path(self) -> Path:
        return self._log_dir / "godot-stdout.log"

    @property
    def stderr_path(self) -> Path:
        return self._log_dir / "godot-stderr.log"

    def _record_exit(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self._close_logs()

    def _close_logs(self) -> None:
        if self._stdout_handle is not None:
            self._stdout_handle.close()
            self._stdout_handle = None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None
