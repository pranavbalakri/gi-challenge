"""Public EnvMaker SDK surface for generated environment programs."""

from __future__ import annotations

from envmaker.sdk.builder import PALETTE_KEYS as PALETTE_KEYS
from envmaker.sdk.builder import SDK_VERSION as SDK_VERSION
from envmaker.sdk.builder import EnvironmentBuilder as EnvironmentBuilder
from envmaker.sdk.compile import compile_environment_model as compile_environment_model
from envmaker.sdk.footprints import Polygon2D as Polygon2D
from envmaker.sdk.kits import CURATED_MATERIALS as CURATED_MATERIALS
from envmaker.sdk.kits import KITS as KITS
from envmaker.sdk.kits import BoxPart as BoxPart
from envmaker.sdk.kits import ConePart as ConePart
from envmaker.sdk.kits import CylinderPart as CylinderPart
from envmaker.sdk.kits import SpherePart as SpherePart
from envmaker.sdk.kits import get_kit as get_kit

__all__ = [
    "SDK_VERSION",
    "PALETTE_KEYS",
    "EnvironmentBuilder",
    "Polygon2D",
    "BoxPart",
    "CylinderPart",
    "ConePart",
    "SpherePart",
    "compile_environment_model",
    "get_kit",
    "KITS",
    "CURATED_MATERIALS",
]
