"""Content-addressed artifact contracts and canonical fingerprints."""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import math as _math
from typing import Any as _Any

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import field_validator as _field_validator
from pydantic import model_validator as _model_validator

__all__ = [
    "CANON_VERSION",
    "MAX_MANIFEST_ENTRIES",
    "FingerprintError",
    "ArtifactPathError",
    "ManifestError",
    "ManifestLookupError",
    "validate_artifact_relpath",
    "canonical_json",
    "canonical_fingerprint",
    "ArtifactRef",
    "ArtifactManifest",
]

CANON_VERSION: int = 1
MAX_MANIFEST_ENTRIES: int = 4096


class FingerprintError(ValueError):
    """Raised when a value cannot be represented canonically."""


class ArtifactPathError(ValueError):
    """Raised when an artifact path is not a safe relative path."""


class ManifestError(ValueError):
    """Raised when an artifact manifest violates its invariants."""


class ManifestLookupError(KeyError):
    """Raised when an artifact path is absent from a manifest."""


def validate_artifact_relpath(path: str) -> str:
    """Return a valid artifact-relative path unchanged."""
    if not isinstance(path, str):
        raise ArtifactPathError("artifact path must be a string")
    if not path:
        raise ArtifactPathError("artifact path must not be empty")
    if len(path) > 512:
        raise ArtifactPathError("artifact path must be at most 512 characters")
    if "\x00" in path:
        raise ArtifactPathError("artifact path must not contain NUL")
    if "\\" in path:
        raise ArtifactPathError("artifact path must use forward slashes only")
    if ":" in path:
        raise ArtifactPathError("artifact path must not contain ':'")
    if path.startswith("/"):
        raise ArtifactPathError("artifact path must not be absolute")

    segments = path.split("/")
    if any(not segment for segment in segments):
        raise ArtifactPathError("artifact path segments must not be empty")
    if any(segment in {".", ".."} for segment in segments):
        raise ArtifactPathError("artifact path segments must not be '.' or '..'")
    return path


class ArtifactRef(_BaseModel):
    """An immutable content-addressed reference with an engine-side SHA-256 digest."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    path: str
    media_type: str = _Field(min_length=1)
    byte_count: int = _Field(ge=1)
    blake2b256: str = _Field(pattern=r"^[0-9a-f]{64}$")
    sha256: str = _Field(pattern=r"^[0-9a-f]{64}$")
    producer: str = _Field(min_length=1)
    toolchain_version: str = _Field(min_length=1)

    @_field_validator("path")
    @classmethod
    def _validate_path(cls, path: str) -> str:
        return validate_artifact_relpath(path)


class ArtifactManifest(_BaseModel):
    """An immutable ordered collection of artifact references."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    root: str
    entries: tuple[ArtifactRef, ...]

    @_field_validator("root")
    @classmethod
    def _validate_root(cls, root: str) -> str:
        return validate_artifact_relpath(root)

    @_model_validator(mode="after")
    def _validate_entries(self) -> ArtifactManifest:
        paths = [entry.path for entry in self.entries]
        if len(set(paths)) != len(paths):
            raise ManifestError("manifest entry paths must be unique")
        if len(self.entries) > MAX_MANIFEST_ENTRIES:
            raise ManifestError(
                f"manifest must contain at most {MAX_MANIFEST_ENTRIES} entries"
            )
        return self

    def get(self, path: str) -> ArtifactRef:
        """Return the reference at path."""
        for entry in self.entries:
            if entry.path == path:
                return entry
        raise ManifestLookupError(path)

    @property
    def paths(self) -> tuple[str, ...]:
        """Return entry paths in manifest order."""
        return tuple(entry.path for entry in self.entries)


def _canonical_structure(value: _Any) -> _Any:
    if isinstance(value, _BaseModel):
        fields: dict[str, _Any] = {}
        for name, field in type(value).model_fields.items():
            field_value = getattr(value, name)
            extra = field.json_schema_extra
            if (
                isinstance(extra, dict)
                and extra.get("omit_when_none") is True
                and field_value is None
            ):
                continue
            if isinstance(extra, dict) and "precision_places" in extra:
                precision_places = extra["precision_places"]
                if isinstance(field_value, float):
                    field_value = round(field_value, precision_places)
                elif isinstance(field_value, (list, tuple)) and all(
                    isinstance(item, float) for item in field_value
                ):
                    field_value = [
                        round(item, precision_places) for item in field_value
                    ]
            fields[name] = field_value
        return _canonical_structure(fields)

    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise FingerprintError("canonical dictionary keys must be strings")
        return {key: _canonical_structure(item) for key, item in value.items()}

    if isinstance(value, (set, frozenset)):
        items = [_canonical_structure(item) for item in value]
        return sorted(
            items,
            key=lambda item: _json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    if isinstance(value, (list, tuple)):
        return [_canonical_structure(item) for item in value]

    if isinstance(value, float):
        if not _math.isfinite(value):
            raise FingerprintError("canonical floats must be finite")
        if value == 0.0:
            return 0.0
        return value

    if value is None or isinstance(value, (int, str, bool)):
        return value

    raise FingerprintError(
        f"unsupported canonical value type: {type(value).__name__}"
    )


def canonical_json(value: _BaseModel | dict) -> str:
    """Serialize a value using EnvMaker's versioned canonical form."""
    return _json.dumps(
        {
            "canon": CANON_VERSION,
            "payload": _canonical_structure(value),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_fingerprint(value: _BaseModel | dict) -> str:
    """Return the BLAKE2b-256 digest of a value's canonical JSON."""
    encoded = canonical_json(value).encode("utf-8")
    return _hashlib.blake2b(encoded, digest_size=32).hexdigest()
