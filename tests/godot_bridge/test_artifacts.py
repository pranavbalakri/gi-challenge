import hashlib
import os
from pathlib import Path

import pytest

from envmaker.core.artifacts import ArtifactManifest, ArtifactRef
from envmaker.core.contracts import ArtifactStore, ArtifactStoreError


def _ref_for(data: bytes, path: str) -> ArtifactRef:
    return ArtifactRef(
        path=path,
        media_type="application/octet-stream",
        byte_count=len(data),
        blake2b256=hashlib.blake2b(data, digest_size=32).hexdigest(),
        sha256=hashlib.sha256(data).hexdigest(),
        producer="test",
        toolchain_version="1.0",
    )


def _write(store: ArtifactStore, data: bytes, extension: str = "bin") -> ArtifactRef:
    return store.write_bytes(
        data,
        media_type="application/octet-stream",
        producer="test",
        toolchain_version="1.0",
        extension=extension,
    )


def test_store_write_and_ref(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    data = b"glbdata"
    blake2b256 = hashlib.blake2b(data, digest_size=32).hexdigest()
    sha256 = hashlib.sha256(data).hexdigest()

    ref = _write(store, data, extension="glb")

    assert ref.path == f"artifacts/{blake2b256}.glb"
    assert ref.blake2b256 == blake2b256
    assert ref.sha256 == sha256
    assert ref.byte_count == 7
    assert (store.run_root / ref.path).is_file()

    second = _write(store, data, extension="glb")
    assert second == ref
    assert len(list((store.run_root / "artifacts").iterdir())) == 1


def test_store_rejects_escape_via_symlinked_dir(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    outside = tmp_path / "outside"
    outside.mkdir()
    data = b"outside"
    (outside / "x.bin").write_bytes(data)
    os.symlink(outside, store.run_root / "artifacts" / "link")
    ref = _ref_for(data, "artifacts/link/x.bin")

    with pytest.raises(ArtifactStoreError, match="path escapes run root"):
        store.resolve_verified(ref)


def test_store_rejects_symlink_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    ref = _write(store, b"artifact")
    artifact_path = store.run_root / ref.path
    copy_path = store.run_root / "copy.bin"
    copy_path.write_bytes(artifact_path.read_bytes())
    artifact_path.unlink()
    os.symlink(copy_path, artifact_path)

    with pytest.raises(ArtifactStoreError, match="symlink artifact rejected"):
        store.resolve_verified(ref)


def test_store_missing_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    data = b"never-written"
    ref = _ref_for(
        data,
        f"artifacts/{hashlib.blake2b(data, digest_size=32).hexdigest()}.bin",
    )

    with pytest.raises(ArtifactStoreError, match="missing artifact file"):
        store.resolve_verified(ref)


def test_store_size_mismatch(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    ref = _write(store, b"artifact")
    wrong_size_ref = ArtifactRef.model_validate(
        ref.model_dump() | {"byte_count": ref.byte_count + 1}
    )

    with pytest.raises(ArtifactStoreError, match="size mismatch"):
        store.resolve_verified(wrong_size_ref)


def test_store_digest_mismatch(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    ref = _write(store, b"abcdef")
    (store.run_root / ref.path).write_bytes(b"abcdeg")

    with pytest.raises(ArtifactStoreError, match="digest mismatch"):
        store.resolve_verified(ref)


def test_verify_manifest(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "run")
    first = _write(store, b"first")
    second = _write(store, b"second")
    manifest = ArtifactManifest(root="artifacts", entries=(first, second))

    store.verify_manifest(manifest)

    (store.run_root / second.path).unlink()
    with pytest.raises(ArtifactStoreError, match="missing artifact file"):
        store.verify_manifest(manifest)
