"""Synchronous Python orchestration for the EnvMaker Godot runtime."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from envmaker.core.artifacts import ArtifactRef, canonical_json
from envmaker.core.contracts import ArtifactStore, BridgeResponse, MessageType
from envmaker.core.episode import EpisodeResult, NavigationProbe
from envmaker.core.interaction import WorldSnapshot
from envmaker.core.scene_spec import CandidateScene
from envmaker.godot_bridge.client import BridgeServer, BridgeSession
from envmaker.godot_bridge.process import GodotProcess


_REPO_ROOT = Path(__file__).resolve().parents[2]
_GODOT_BIN = _REPO_ROOT / "tools/godot/Godot.app/Contents/MacOS/Godot"
_GODOT_PROJECT = _REPO_ROOT / "godot"
_WINDOWED_ARGS = ("--resolution", "320x180", "--position", "4000,4000")


class RuntimeDriverError(RuntimeError):
    """Base error for runtime orchestration failures."""


class NavigationFailedError(RuntimeDriverError):
    """Raised when runtime navigation cannot become ready."""


class RenderUnavailableError(RuntimeDriverError):
    """Raised when the runtime cannot capture a render."""


class RuntimeDriver:
    """Own one serial Godot process, bridge session, and simulation clock."""

    last_planned_path_length_m: float | None

    def __init__(
        self,
        *,
        run_dir: Path,
        session_id: str,
        windowed: bool = False,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._session_id = session_id
        self._windowed = windowed
        self._server: BridgeServer | None = None
        self._process: GodotProcess | None = None
        self._session: BridgeSession | None = None
        self._tick_id = 0
        self._navigation_pending = False
        self._navigation_ready = False
        self._closed = False
        self._exit_code = 0
        self._request_lock = threading.RLock()
        self.last_planned_path_length_m = None

    def start(self) -> None:
        """Start and authenticate the owned Godot bridge process."""
        with self._request_lock:
            if self._closed or self._server is not None:
                raise RuntimeDriverError("runtime already started")

            token = secrets.token_hex(16)
            server = BridgeServer(session_id=self._session_id, token=token)
            self._server = server
            try:
                host, port = server.listen()
                process = GodotProcess(
                    godot_bin=_GODOT_BIN,
                    project_path=_GODOT_PROJECT,
                    host=host,
                    port=port,
                    session_id=self._session_id,
                    token=token,
                    log_dir=self._run_dir / "logs",
                    run_root=self._run_dir,
                    extra_args=_WINDOWED_ARGS if self._windowed else (),
                    headless=not self._windowed,
                )
                process.start()
                self._process = process
                self._session = server.accept(timeout=30.0)
            except BaseException:
                self.close()
                raise

    def load_candidate(self, candidate: CandidateScene) -> BridgeResponse:
        """Load one canonical candidate and return the bridge response unchanged."""
        with self._request_lock:
            if self._navigation_pending:
                raise RuntimeDriverError("navigation bake in progress")

            response = self._require_session().request(
                MessageType.LOAD_CANDIDATE,
                json.loads(canonical_json(candidate)),
            )
            if response.ok:
                self._navigation_pending = True
                self._navigation_ready = False
            return response

    def wait_navigation_ready(self, timeout: float = 30.0) -> None:
        """Poll until the loaded candidate's navigation map is ready."""
        with self._request_lock:
            deadline = time.monotonic() + timeout
            while True:
                response = self._require_session().request(
                    MessageType.NAVIGATION_STATUS
                )
                if not response.ok:
                    raise RuntimeDriverError(self._error_code(response))

                state = response.payload["state"]
                if state == "ready":
                    self._navigation_pending = False
                    self._navigation_ready = True
                    return
                if state == "failed":
                    self._navigation_pending = False
                    self._navigation_ready = False
                    raise NavigationFailedError("navigation failed")

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NavigationFailedError(
                        "navigation not ready within timeout"
                    )
                time.sleep(min(0.1, remaining))

    def navigate(self, probe: NavigationProbe) -> EpisodeResult:
        """Run one bounded navigation episode at the next simulation tick."""
        with self._request_lock:
            if not self._navigation_ready:
                raise RuntimeDriverError("navigation not ready")

            tick_id = self._next_tick()
            payload = json.loads(canonical_json(probe))["payload"]
            response = self._require_session().request(
                MessageType.PROBE,
                payload,
                tick_id=tick_id,
                deadline=120.0,
            )
            if not response.ok:
                raise RuntimeDriverError(self._error_code(response))

            episode_payload = dict(response.payload)
            self.last_planned_path_length_m = float(
                episode_payload.pop("planned_path_length_m")
            )
            return EpisodeResult.model_validate(episode_payload)

    def snapshot(self) -> WorldSnapshot:
        """Return a validated world snapshot at the next simulation tick."""
        with self._request_lock:
            tick_id = self._next_tick()
            response = self._require_session().request(
                MessageType.SNAPSHOT,
                tick_id=tick_id,
            )
            if not response.ok:
                raise RuntimeDriverError(self._error_code(response))
            return WorldSnapshot.model_validate(response.payload)

    def render(
        self,
        view: Literal["isometric", "topdown"],
    ) -> ArtifactRef:
        """Capture, verify, and ingest one runtime render."""
        with self._request_lock:
            tick_id = self._next_tick()
            response = self._require_session().request(
                MessageType.RENDER,
                {"view": view},
                tick_id=tick_id,
                deadline=30.0,
            )
            if not response.ok:
                error = response.error
                if error is not None and error.code == "bridge.render_unavailable":
                    raise RenderUnavailableError(error.message)
                raise RuntimeDriverError(self._error_code(response))

            render_path = self._run_dir / str(response.payload["path"])
            data = render_path.read_bytes()
            if hashlib.sha256(data).hexdigest() != response.payload["sha256"]:
                raise RuntimeDriverError("render digest mismatch")
            if len(data) != response.payload["byte_count"]:
                raise RuntimeDriverError("render byte count mismatch")

            return ArtifactStore(self._run_dir).write_bytes(
                data,
                media_type="image/png",
                producer="godot-renderer",
                toolchain_version="godot-4.7.1",
                extension="png",
            )

    def close(self) -> int:
        """Close the session, process, and listener exactly once."""
        with self._request_lock:
            if self._closed:
                return self._exit_code
            self._closed = True

            session = self._session
            process = self._process
            server = self._server
            self._session = None
            self._process = None
            self._server = None

            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

            exit_code = 0
            try:
                if process is not None:
                    try:
                        exit_code = process.wait_closed(15.0)
                    except Exception:
                        exit_code = process.terminate()
            finally:
                if server is not None:
                    server.close()

            self._exit_code = exit_code
            return exit_code

    def _require_session(self) -> BridgeSession:
        if self._session is None:
            raise RuntimeDriverError("runtime not started")
        return self._session

    def _next_tick(self) -> int:
        self._tick_id += 1
        return self._tick_id

    @staticmethod
    def _error_code(response: BridgeResponse) -> str:
        if response.error is None:
            return "bridge request failed"
        return response.error.code
