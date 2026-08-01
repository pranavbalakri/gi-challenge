import pytest
from pydantic import BaseModel, Field, ValidationError

from envmaker.core.artifacts import (
    MAX_MANIFEST_ENTRIES,
    ArtifactManifest,
    ArtifactPathError,
    ArtifactRef,
    FingerprintError,
    ManifestError,
    ManifestLookupError,
    canonical_fingerprint,
    canonical_json,
)


def _artifact_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "path": "assets/model.glb",
        "media_type": "model/gltf-binary",
        "byte_count": 1,
        "blake2b256": "0" * 64,
        "sha256": "cd" * 32,
        "producer": "test",
        "toolchain_version": "1.0",
    }
    data.update(overrides)
    return data


def _artifact_ref(**overrides: object) -> ArtifactRef:
    return ArtifactRef.model_validate(_artifact_data(**overrides))


def test_artifactref_requires_all_fields() -> None:
    without_byte_count = _artifact_data()
    del without_byte_count["byte_count"]
    with pytest.raises(ValidationError):
        ArtifactRef.model_validate(without_byte_count)

    without_digest = _artifact_data()
    del without_digest["blake2b256"]
    with pytest.raises(ValidationError):
        ArtifactRef.model_validate(without_digest)

    without_sha256 = _artifact_data()
    del without_sha256["sha256"]
    with pytest.raises(ValidationError):
        ArtifactRef.model_validate(without_sha256)


def test_artifactref_rejects_absolute_path() -> None:
    with pytest.raises(ValidationError, match="must not be absolute") as exc_info:
        _artifact_ref(path="/abs/x.glb")

    assert isinstance(exc_info.value.errors()[0]["ctx"]["error"], ArtifactPathError)


def test_artifactref_rejects_traversal_and_dot_segments() -> None:
    for path in ("a/../x.glb", "..", "a/./b.glb"):
        with pytest.raises(ValidationError, match="segments must not be") as exc_info:
            _artifact_ref(path=path)

        assert isinstance(
            exc_info.value.errors()[0]["ctx"]["error"], ArtifactPathError
        )


def test_artifactref_rejects_backslash_empty_and_colon() -> None:
    cases = (
        ("a\\\\b.glb", "must use forward slashes"),
        ("", "must not be empty"),
        ("c:/x.glb", "must not contain ':'"),
    )
    for path, message in cases:
        with pytest.raises(ValidationError, match=message) as exc_info:
            _artifact_ref(path=path)

        assert isinstance(
            exc_info.value.errors()[0]["ctx"]["error"], ArtifactPathError
        )


def test_artifactref_digest_format() -> None:
    for digest in ("0" * 63, "A" * 64, "g" * 64):
        with pytest.raises(ValidationError):
            _artifact_ref(blake2b256=digest)

    for digest in ("0" * 63, "A" * 64):
        with pytest.raises(ValidationError):
            _artifact_ref(sha256=digest)


def test_artifactref_byte_count_positive() -> None:
    with pytest.raises(ValidationError):
        _artifact_ref(byte_count=0)

    assert _artifact_ref(byte_count=1).byte_count == 1


def test_artifactref_frozen() -> None:
    ref = _artifact_ref()

    with pytest.raises(ValidationError, match="frozen"):
        ref.path = "assets/other.glb"


def test_manifest_rejects_duplicate_paths() -> None:
    first = _artifact_ref(path="assets/shared.glb")
    second = _artifact_ref(path="assets/shared.glb", producer="other")

    with pytest.raises(ValidationError, match="entry paths must be unique") as exc_info:
        ArtifactManifest(root="run", entries=(first, second))

    assert isinstance(exc_info.value.errors()[0]["ctx"]["error"], ManifestError)


def test_manifest_get_and_paths() -> None:
    first = _artifact_ref(path="assets/first.glb")
    second = _artifact_ref(path="assets/second.glb")
    manifest = ArtifactManifest(root="run", entries=(first, second))

    assert manifest.get("assets/second.glb") is second
    assert manifest.paths == ("assets/first.glb", "assets/second.glb")
    with pytest.raises(ManifestLookupError):
        manifest.get("missing.glb")


def test_manifest_bounded() -> None:
    refs = tuple(
        _artifact_ref(path=f"a/{index}.bin")
        for index in range(MAX_MANIFEST_ENTRIES + 1)
    )

    with pytest.raises(ValidationError, match="at most 4096 entries") as exc_info:
        ArtifactManifest(root="run", entries=refs)

    assert isinstance(exc_info.value.errors()[0]["ctx"]["error"], ManifestError)
    assert len(ArtifactManifest(root="run", entries=refs[:-1]).entries) == 4096


def test_fingerprint_mapping_order_invariant() -> None:
    forward = {"alpha": 1, "beta": {"x": 2, "y": 3}}
    reverse = {"beta": {"y": 3, "x": 2}, "alpha": 1}

    assert canonical_fingerprint(forward) == canonical_fingerprint(reverse)


def test_fingerprint_set_order_invariant() -> None:
    assert canonical_fingerprint({"ids": {"b", "a", "c"}}) == canonical_fingerprint(
        {"ids": {"c", "a", "b"}}
    )
    assert canonical_fingerprint({"ids": {"b", "a", "c"}}) != canonical_fingerprint(
        {"ids": ["b", "a", "c"]}
    )

    # Sets canonicalize to sorted lists, so a set and an already-sorted list collapse.
    assert canonical_fingerprint({"ids": {"b", "a"}}) == canonical_fingerprint(
        {"ids": ["a", "b"]}
    )


def test_fingerprint_rejects_nan_inf() -> None:
    for value in (float("nan"), float("inf")):
        with pytest.raises(FingerprintError, match="must be finite"):
            canonical_fingerprint({"x": value})


def test_fingerprint_rejects_bytes_and_nonstr_keys() -> None:
    with pytest.raises(FingerprintError, match="unsupported canonical value type"):
        canonical_fingerprint({"x": b"raw"})

    with pytest.raises(FingerprintError, match="dictionary keys must be strings"):
        canonical_fingerprint({1: "x"})


def test_fingerprint_quantization() -> None:
    class Q(BaseModel):
        v: float = Field(json_schema_extra={"precision_places": 3})
        vs: list[float] = Field(
            default_factory=list,
            json_schema_extra={"precision_places": 2},
        )

    assert canonical_fingerprint(Q(v=1.23449999)) == canonical_fingerprint(
        Q(v=1.2340000001)
    )
    assert canonical_fingerprint(Q(v=1.23449999)) != canonical_fingerprint(Q(v=1.236))

    assert canonical_fingerprint(Q(v=0.0, vs=[1.234, 5.678])) == canonical_fingerprint(
        Q(v=0.0, vs=[1.2301, 5.6801])
    )
    assert canonical_fingerprint(Q(v=0.0, vs=[1.234, 5.678])) != canonical_fingerprint(
        Q(v=0.0, vs=[1.24, 5.68])
    )


def test_fingerprint_envelope_and_determinism() -> None:
    value = {"b": 1, "a": [1.5, 2]}

    assert canonical_json(value).startswith('{"canon":1,')
    first = canonical_fingerprint(value)
    second = canonical_fingerprint(value)
    assert first == second
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_canonical_omit_when_none_marker() -> None:
    from pydantic import BaseModel, ConfigDict, Field

    class _Marked(BaseModel):
        model_config = ConfigDict(frozen=True)

        keep: int = 1
        maybe: str | None = Field(
            default=None,
            json_schema_extra={"omit_when_none": True},
        )
        plain: str | None = None

    absent = canonical_json(_Marked())
    assert '"maybe"' not in absent
    assert '"plain":null' in absent
    assert canonical_fingerprint(_Marked()) == canonical_fingerprint(
        {"keep": 1, "plain": None}
    )

    present = canonical_json(_Marked(maybe="x"))
    assert '"maybe":"x"' in present
