"""Verify the pinned EnvMaker toolchain and print a provenance record.

Exits nonzero if any hard pin is violated. Renderer and physics backend are
recorded as declared project configuration until Task 2 creates the Godot
project that must match them.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GODOT_BIN = REPO_ROOT / "tools" / "godot" / "Godot.app" / "Contents" / "MacOS" / "Godot"

REQUIRED_PYTHON = (3, 12)
REQUIRED_GODOT_PREFIX = "4.7.1.stable"

DECLARED_RENDERER = "forward_plus"
DECLARED_PHYSICS = "godot_physics_3d"


def fail(reason: str) -> None:
    print(f"TOOLCHAIN FAIL: {reason}", file=sys.stderr)
    sys.exit(1)


def godot_version(godot_bin: Path) -> str:
    if not godot_bin.exists():
        fail(f"godot binary not found at {godot_bin}")
    result = subprocess.run(
        [str(godot_bin), "--headless", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        fail(f"godot --version exited {result.returncode}: {result.stderr.strip()}")
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    if not lines:
        fail("godot --version produced no output")
    return lines[-1].strip()


def lockfile_hash() -> str:
    lock = REPO_ROOT / "uv.lock"
    if not lock.exists():
        fail("uv.lock missing")
    return hashlib.blake2b(lock.read_bytes(), digest_size=32).hexdigest()


def main() -> None:
    if sys.version_info[:2] != REQUIRED_PYTHON:
        fail(f"python {platform.python_version()} != required {REQUIRED_PYTHON}")
    godot_bin = Path(os.environ.get("GODOT_BIN", str(DEFAULT_GODOT_BIN)))
    version = godot_version(godot_bin)
    if not version.startswith(REQUIRED_GODOT_PREFIX):
        fail(f"godot version {version!r} does not start with {REQUIRED_GODOT_PREFIX!r}")
    record = {
        "python": platform.python_version(),
        "godot": {"bin": str(godot_bin), "version": version},
        "declared_renderer": DECLARED_RENDERER,
        "declared_physics": DECLARED_PHYSICS,
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "uv_lock_blake2b256": lockfile_hash(),
    }
    print(json.dumps(record, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
