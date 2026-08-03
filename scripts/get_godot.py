"""Download the pinned Godot 4.7.1-stable build for this platform.

Stdlib-only, reviewer-facing: a fresh clone has no vendored binary
(`tools/godot/` is gitignored), so this fetches the official build from
godotengine/godot-builds into the layout `resolve_godot_binary()` expects.

Usage: uv run python scripts/get_godot.py [--force]
Alternative: set GODOT_BIN=<path> to an existing Godot 4.7.1 binary instead.
"""

from __future__ import annotations

import platform
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS_GODOT = REPO_ROOT / "tools" / "godot"
RELEASE_BASE = (
    "https://github.com/godotengine/godot-builds/releases/download/4.7.1-stable"
)


def build_asset_name(system: str, machine: str) -> str:
    """Map (platform.system(), platform.machine()) to the official asset."""

    if system == "Darwin":
        return "Godot_v4.7.1-stable_macos.universal.zip"
    if system == "Windows":
        return "Godot_v4.7.1-stable_win64.exe.zip"
    if system == "Linux":
        arch = machine.lower()
        if arch in {"arm64", "aarch64"}:
            return "Godot_v4.7.1-stable_linux.arm64.zip"
        return "Godot_v4.7.1-stable_linux.x86_64.zip"
    raise SystemExit(f"unsupported platform: {system}/{machine}")


def expected_binary(system: str) -> Path:
    if system == "Darwin":
        return TOOLS_GODOT / "Godot.app" / "Contents" / "MacOS" / "Godot"
    if system == "Windows":
        return TOOLS_GODOT / "godot.exe"
    return TOOLS_GODOT / "godot"


def _extract_zip_preserving_modes(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = destination / info.filename
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            mode = (info.external_attr >> 16) & 0o7777
            if mode:
                target.chmod(mode)


def open_release_url(url: str):
    """Open the release URL with working certificate verification.

    python.org framework builds on macOS do not use the system trust store
    and fail every stdlib HTTPS call until their 'Install Certificates'
    step has been run. The project venv ships certifi (via the OpenAI
    client), so fall back to certifi's CA bundle — never to disabled
    verification, since this downloads an executable.
    """

    try:
        return urllib.request.urlopen(url)
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
    try:
        import certifi
    except ImportError:
        raise SystemExit(
            "TLS certificate verification failed and certifi is not "
            "installed. Run your Python's 'Install Certificates.command' "
            "(python.org builds), or run this script via `uv run python "
            "scripts/get_godot.py`, or set GODOT_BIN=<path> to an existing "
            "Godot 4.7.1 binary."
        )
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(url, context=context)


def main() -> int:
    force = "--force" in sys.argv
    system = platform.system()
    machine = platform.machine()
    binary = expected_binary(system)

    if binary.is_file() and not force:
        print(f"already present: {binary}")
        print("(use --force to re-download)")
        return 0

    asset = build_asset_name(system, machine)
    url = f"{RELEASE_BASE}/{asset}"
    TOOLS_GODOT.mkdir(parents=True, exist_ok=True)

    print(f"downloading {url}")
    with tempfile.TemporaryDirectory() as scratch:
        archive = Path(scratch) / asset
        with open_release_url(url) as response, archive.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            copied = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                copied += len(chunk)
                if total:
                    print(
                        f"\r  {copied / (1 << 20):.0f} / {total / (1 << 20):.0f} MiB",
                        end="",
                        flush=True,
                    )
        print()

        extracted = Path(scratch) / "extracted"
        _extract_zip_preserving_modes(archive, extracted)

        if system == "Darwin":
            app = extracted / "Godot.app"
            if not app.is_dir():
                raise SystemExit("archive did not contain Godot.app")
            destination = TOOLS_GODOT / "Godot.app"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.move(str(app), str(destination))
        else:
            candidates = [
                p
                for p in extracted.rglob("*")
                if p.is_file() and p.name.startswith("Godot_v4.7.1")
            ]
            if not candidates:
                raise SystemExit("archive did not contain a Godot binary")
            if binary.exists():
                binary.unlink()
            shutil.move(str(candidates[0]), str(binary))
            binary.chmod(0o755)

    print(f"installed: {binary}")
    print("verifying toolchain...")
    import subprocess

    verify = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "verify_toolchain.py")],
        check=False,
    )
    return verify.returncode


if __name__ == "__main__":
    raise SystemExit(main())
