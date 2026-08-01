"""Synchronous Python orchestration for the EnvMaker Godot runtime."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from pathlib import Path
from typing import Literal

from envmaker.core.artifacts import ArtifactRef, canonical_json
from envmaker.core.contracts import ArtifactStore, BridgeResponse, MessageType
from envmaker.core.episode import EpisodeResult, NavigationProbe
from envmaker.core.interaction import WorldSnapshot
from envmaker.core.scene_spec import CandidateScene, ColliderShape, PlaneVisual, SceneNode
from envmaker.godot_bridge.client import BridgeServer, BridgeSession
from envmaker.godot_bridge.process import GodotProcess, resolve_godot_binary

_AGENT_RADIUS = 0.4


_REPO_ROOT = Path(__file__).resolve().parents[2]
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
        # Resolved: Godot's CWD is the project dir, so a relative run_dir would
        # make the bridge write artifacts under godot/ while Python reads here.
        self._run_dir = Path(run_dir).resolve()
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
        self._last_candidate: CandidateScene | None = None
        self._spawn_xz: tuple[float, float] | None = None

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
                    godot_bin=resolve_godot_binary(),
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
                self._last_candidate = candidate
                spawn_id = candidate.scene.controller_semantic_id
                spawn = next(
                    (
                        node
                        for node in candidate.scene.nodes
                        if node.semantic_id == spawn_id
                    ),
                    None,
                )
                if spawn is not None:
                    self._spawn_xz = (
                        spawn.transform.origin.x,
                        spawn.transform.origin.z,
                    )
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

    def connected_navigable_fraction(
        self,
        *,
        bounds: tuple[float, float, float, float],
        grid: int = 12,
        from_point: tuple[float, float] | None = None,
    ) -> float:
        """Estimate the fraction of a ground lattice reachable from a point.

        Samples a ``grid×grid`` lattice over ``(min_x, min_z, max_x, max_z)``,
        marks cells clear of blocker footprints, and flood-fills from
        ``from_point`` (default: current agent via snapshot, else last spawn).
        Returns reachable/total. Orchestrates over the candidate retained from
        ``load_candidate`` plus ``snapshot`` for the origin — the bridge has no
        standalone ``map_get_path`` message yet.
        """
        with self._request_lock:
            if grid < 1:
                raise RuntimeDriverError("grid must be >= 1")
            if not self._navigation_ready:
                raise RuntimeDriverError("navigation not ready")
            if self._last_candidate is None:
                raise RuntimeDriverError("no candidate loaded")

            origin = from_point
            if origin is None:
                try:
                    snap = self.snapshot()
                    origin = (
                        snap.agent_transform.origin.x,
                        snap.agent_transform.origin.z,
                    )
                except Exception:
                    origin = self._spawn_xz
            if origin is None:
                raise RuntimeDriverError("from_point unavailable")

            min_x, min_z, max_x, max_z = bounds
            if max_x <= min_x or max_z <= min_z:
                raise RuntimeDriverError("invalid bounds")

            blockers = _blocker_oriented_list(self._last_candidate)
            xs = [
                min_x + (i + 0.5) * (max_x - min_x) / grid for i in range(grid)
            ]
            zs = [
                min_z + (j + 0.5) * (max_z - min_z) / grid for j in range(grid)
            ]
            walkable = [
                [_point_clear(x, z, blockers) for z in zs] for x in xs
            ]
            # Seed BFS at the walkable cell nearest the origin.
            best: tuple[int, int] | None = None
            best_dist = float("inf")
            for i, x in enumerate(xs):
                for j, z in enumerate(zs):
                    if not walkable[i][j]:
                        continue
                    dist = math.hypot(x - origin[0], z - origin[1])
                    if dist < best_dist:
                        best_dist = dist
                        best = (i, j)
            if best is None:
                return 0.0

            seen = [[False] * grid for _ in range(grid)]
            queue = [best]
            seen[best[0]][best[1]] = True
            reachable = 0
            while queue:
                i, j = queue.pop()
                reachable += 1
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ni, nj = i + di, j + dj
                    if not (0 <= ni < grid and 0 <= nj < grid):
                        continue
                    if seen[ni][nj] or not walkable[ni][nj]:
                        continue
                    # Require a clear edge between cell centers (path query).
                    if not _segment_clear(
                        xs[i], zs[j], xs[ni], zs[nj], blockers
                    ):
                        continue
                    seen[ni][nj] = True
                    queue.append((ni, nj))
            return reachable / float(grid * grid)

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


def _yaw_from_basis(node: SceneNode) -> float:
    return math.atan2(-node.transform.basis_x.z, node.transform.basis_x.x)


def _local_xz(
    world_x: float,
    world_z: float,
    origin_x: float,
    origin_z: float,
    yaw: float,
) -> tuple[float, float]:
    dx = world_x - origin_x
    dz = world_z - origin_z
    cos_yaw = math.cos(-yaw)
    sin_yaw = math.sin(-yaw)
    return (cos_yaw * dx + sin_yaw * dz, -sin_yaw * dx + cos_yaw * dz)


def _blocker_oriented_list(
    candidate: CandidateScene,
) -> list[tuple[float, float, float, float, float]]:
    """Return (origin_x, origin_z, yaw, half_x, half_z) inflated by agent radius."""

    blockers: list[tuple[float, float, float, float, float]] = []
    for node in candidate.scene.nodes:
        if isinstance(node.visual, PlaneVisual):
            continue
        collider = node.collider
        if collider is None:
            continue
        if collider.shape is ColliderShape.CYLINDER:
            radius = float(collider.dimensions["radius"])
            half_x = half_z = radius
        elif "x" in collider.dimensions and "z" in collider.dimensions:
            half_x = float(collider.dimensions["x"]) / 2.0
            half_z = float(collider.dimensions["z"]) / 2.0
        else:
            continue
        blockers.append(
            (
                node.transform.origin.x,
                node.transform.origin.z,
                _yaw_from_basis(node),
                half_x + _AGENT_RADIUS,
                half_z + _AGENT_RADIUS,
            )
        )
    return blockers


def _point_in_oriented(
    x: float,
    z: float,
    blocker: tuple[float, float, float, float, float],
) -> bool:
    ox, oz, yaw, half_x, half_z = blocker
    lx, lz = _local_xz(x, z, ox, oz, yaw)
    return abs(lx) <= half_x and abs(lz) <= half_z


def _point_clear(
    x: float,
    z: float,
    blockers: list[tuple[float, float, float, float, float]],
) -> bool:
    return all(not _point_in_oriented(x, z, blocker) for blocker in blockers)


def _segment_hits_oriented(
    x1: float,
    z1: float,
    x2: float,
    z2: float,
    blocker: tuple[float, float, float, float, float],
) -> bool:
    ox, oz, yaw, half_x, half_z = blocker
    lx1, lz1 = _local_xz(x1, z1, ox, oz, yaw)
    lx2, lz2 = _local_xz(x2, z2, ox, oz, yaw)

    def _inside(x: float, z: float) -> bool:
        return abs(x) <= half_x and abs(z) <= half_z

    if _inside(lx1, lz1) or _inside(lx2, lz2):
        return True

    dx = lx2 - lx1
    dz = lz2 - lz1
    p = (-dx, dx, -dz, dz)
    q = (lx1 - (-half_x), half_x - lx1, lz1 - (-half_z), half_z - lz1)
    u1, u2 = 0.0, 1.0
    for pi, qi in zip(p, q):
        if pi == 0.0:
            if qi < 0.0:
                return False
            continue
        t = qi / pi
        if pi < 0.0:
            u1 = max(u1, t)
        else:
            u2 = min(u2, t)
        if u1 > u2:
            return False
    return True


def _segment_clear(
    x1: float,
    z1: float,
    x2: float,
    z2: float,
    blockers: list[tuple[float, float, float, float, float]],
) -> bool:
    return all(
        not _segment_hits_oriented(x1, z1, x2, z2, blocker)
        for blocker in blockers
    )
