"""Versioned Python/Godot bridge contracts and artifact storage."""

from __future__ import annotations

import hashlib as _hashlib
import os as _os
import re as _re
import stat as _stat
import tempfile as _tempfile
from enum import StrEnum as _StrEnum
from pathlib import Path as _Path

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import field_validator as _field_validator
from pydantic import model_validator as _model_validator

from envmaker.core.artifacts import ArtifactManifest as _ArtifactManifest
from envmaker.core.artifacts import ArtifactRef as _ArtifactRef
from envmaker.core.artifacts import validate_artifact_relpath as _validate_artifact_relpath
from envmaker.core.signals import Signal as _Signal

__all__ = [
    "PROTOCOL_VERSION",
    "MAX_CONTROL_MESSAGE_BYTES",
    "MAX_IN_FLIGHT_BYTES",
    "MAX_QUEUE_DEPTH",
    "DEFAULT_DEADLINE_SECONDS",
    "SESSION_ID_PATTERN",
    "MessageType",
    "SIMULATION_TYPES",
    "BridgeRequest",
    "BridgeResponse",
    "ArtifactStoreError",
    "ArtifactStore",
]

PROTOCOL_VERSION: int = 1
MAX_CONTROL_MESSAGE_BYTES: int = 1_048_576
MAX_IN_FLIGHT_BYTES: int = 8_388_608
MAX_QUEUE_DEPTH: int = 64
DEFAULT_DEADLINE_SECONDS: float = 30.0
SESSION_ID_PATTERN: str = r"^[a-z0-9][a-z0-9-]{0,63}$"

_EXTENSION_PATTERN = _re.compile(r"^[a-z0-9]{1,8}$")


class MessageType(_StrEnum):
    """Type of one Python/Godot bridge message."""

    HELLO = "hello"
    LOAD_CANDIDATE = "load_candidate"
    NAVIGATION_STATUS = "navigation_status"
    RESET = "reset"
    STEP = "step"
    SNAPSHOT = "snapshot"
    RENDER = "render"
    PROBE = "probe"
    CLOSE = "close"


SIMULATION_TYPES: frozenset[MessageType] = frozenset(
    {
        MessageType.RESET,
        MessageType.STEP,
        MessageType.SNAPSHOT,
        MessageType.RENDER,
        MessageType.PROBE,
    }
)


def _check_protocol_version(protocol_version: int) -> int:
    if protocol_version != PROTOCOL_VERSION:
        raise ValueError(f"protocol_version must equal {PROTOCOL_VERSION}")
    return protocol_version


def _check_payload(payload: dict[str, object]) -> dict[str, object]:
    if len(payload) > 64:
        raise ValueError("payload must contain at most 64 entries")
    for key in payload:
        if not key:
            raise ValueError("payload keys must not be empty")
        if len(key) > 64:
            raise ValueError("payload keys must be at most 64 characters")
    return payload


def _check_tick(message_type: MessageType, tick_id: int | None) -> None:
    if message_type in SIMULATION_TYPES:
        if tick_id is None:
            raise ValueError("tick_id required for simulation message")
    elif tick_id is not None:
        raise ValueError("tick_id forbidden for control message")


class BridgeRequest(_BaseModel):
    """A bounded request envelope sent across the Python/Godot boundary."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    protocol_version: int
    session_id: str = _Field(pattern=SESSION_ID_PATTERN)
    request_id: int = _Field(ge=1)
    tick_id: int | None = _Field(default=None, ge=0)
    type: MessageType
    payload: dict[str, object] = {}

    @_field_validator("protocol_version")
    @classmethod
    def _validate_protocol_version(cls, protocol_version: int) -> int:
        return _check_protocol_version(protocol_version)

    @_field_validator("payload")
    @classmethod
    def _validate_payload(cls, payload: dict[str, object]) -> dict[str, object]:
        return _check_payload(payload)

    @_model_validator(mode="after")
    def _validate_tick(self) -> BridgeRequest:
        _check_tick(self.type, self.tick_id)
        return self


class BridgeResponse(_BaseModel):
    """A bounded response envelope sent across the Python/Godot boundary."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    protocol_version: int
    session_id: str = _Field(pattern=SESSION_ID_PATTERN)
    request_id: int = _Field(ge=1)
    tick_id: int | None = _Field(default=None, ge=0)
    type: MessageType
    ok: bool
    payload: dict[str, object] = {}
    error: _Signal | None = None

    @_field_validator("protocol_version")
    @classmethod
    def _validate_protocol_version(cls, protocol_version: int) -> int:
        return _check_protocol_version(protocol_version)

    @_field_validator("payload")
    @classmethod
    def _validate_payload(cls, payload: dict[str, object]) -> dict[str, object]:
        return _check_payload(payload)

    @_model_validator(mode="after")
    def _validate_invariants(self) -> BridgeResponse:
        _check_tick(self.type, self.tick_id)
        if not self.ok and self.error is None:
            raise ValueError("error signal required when not ok")
        if self.ok and self.error is not None:
            raise ValueError("error signal forbidden when ok")
        return self


class ArtifactStoreError(ValueError):
    """Raised when an artifact cannot be safely stored or verified."""


class ArtifactStore:
    """A content-addressed artifact store rooted in one run directory."""

    def __init__(self, run_root: _Path) -> None:
        self._run_root = _Path(run_root)
        self._artifacts_dir = self._run_root / "artifacts"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

    @property
    def run_root(self) -> _Path:
        """Return the store's run root."""
        return self._run_root

    def write_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        producer: str,
        toolchain_version: str,
        extension: str,
    ) -> _ArtifactRef:
        """Atomically store non-empty bytes and return their artifact reference."""
        if not data:
            raise ArtifactStoreError("data must not be empty")
        if _EXTENSION_PATTERN.fullmatch(extension) is None:
            raise ArtifactStoreError("invalid artifact extension")

        blake2b256 = _hashlib.blake2b(data, digest_size=32).hexdigest()
        sha256 = _hashlib.sha256(data).hexdigest()
        relative_path = f"artifacts/{blake2b256}.{extension}"
        ref = _ArtifactRef(
            path=relative_path,
            media_type=media_type,
            byte_count=len(data),
            blake2b256=blake2b256,
            sha256=sha256,
            producer=producer,
            toolchain_version=toolchain_version,
        )
        artifact_path = self._run_root / relative_path

        if not artifact_path.exists():
            temporary_path: _Path | None = None
            try:
                with _tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self._artifacts_dir,
                    prefix=".artifact-",
                    delete=False,
                ) as temporary_file:
                    temporary_path = _Path(temporary_file.name)
                    temporary_file.write(data)
                _os.replace(temporary_path, artifact_path)
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass

        return ref

    def resolve_verified(self, ref: _ArtifactRef) -> _Path:
        """Resolve an artifact path after containment and digest verification."""
        relative_path = _validate_artifact_relpath(ref.path)
        full = self._run_root / relative_path
        resolved_root = self._run_root.resolve()
        resolved_full = full.resolve()
        if not resolved_full.is_relative_to(resolved_root):
            raise ArtifactStoreError("path escapes run root")

        try:
            file_stat = _os.lstat(full)
        except (FileNotFoundError, NotADirectoryError):
            raise ArtifactStoreError("missing artifact file") from None
        if _stat.S_ISLNK(file_stat.st_mode):
            raise ArtifactStoreError("symlink artifact rejected")
        if not _stat.S_ISREG(file_stat.st_mode):
            raise ArtifactStoreError("artifact must be a regular file")

        if file_stat.st_size != ref.byte_count:
            raise ArtifactStoreError("size mismatch")

        blake2b256 = _hashlib.blake2b(digest_size=32)
        sha256 = _hashlib.sha256()
        with full.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                blake2b256.update(chunk)
                sha256.update(chunk)
        if (
            blake2b256.hexdigest() != ref.blake2b256
            or sha256.hexdigest() != ref.sha256
        ):
            raise ArtifactStoreError("digest mismatch")

        return full

    def verify_manifest(self, manifest: _ArtifactManifest) -> None:
        """Verify every manifest entry in order."""
        for ref in manifest.entries:
            self.resolve_verified(ref)
