"""Immutable curated kits for structures, landmarks, and vegetation."""

from __future__ import annotations

import math as _math
import re as _re
from typing import Literal as _Literal

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import model_validator as _model_validator

__all__ = ["CURATED_MATERIALS", "KitPart", "Kit", "KITS", "get_kit"]

CURATED_MATERIALS: frozenset[str] = frozenset(
    {
        "default",
        "grass",
        "dirt",
        "stone",
        "rock",
        "wood",
        "water",
        "snow",
    }
)


class KitPart(_BaseModel):
    """One immutable primitive within a curated kit."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    shape: _Literal["box", "cylinder"]
    offset: tuple[float, float, float]
    size: tuple[float, float, float] | None = None
    radius: float | None = None
    height: float | None = None
    material: str

    @_model_validator(mode="after")
    def _validate_part(self) -> KitPart:
        if self.material not in CURATED_MATERIALS:
            raise ValueError(f"unknown material: {self.material}")

        if self.shape == "box":
            size = self.size
            if size is None or self.radius is not None or self.height is not None:
                raise ValueError("box parts take size only")
            dimensions = size
        else:
            radius = self.radius
            height = self.height
            if radius is None or height is None or self.size is not None:
                raise ValueError("cylinder parts take radius and height only")
            dimensions = (radius, height)

        if any(
            not _math.isfinite(dimension) or dimension <= 0.0
            for dimension in dimensions
        ):
            raise ValueError("part dimensions must be finite and positive")
        if any(not _math.isfinite(coordinate) for coordinate in self.offset):
            raise ValueError("part offset components must be finite")
        return self


class Kit(_BaseModel):
    """An immutable named assembly from the curated kit catalog."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    name: str
    category: _Literal["structure", "landmark", "vegetation"]
    blocking: bool
    parts: tuple[KitPart, ...]

    @_model_validator(mode="after")
    def _validate_kit(self) -> Kit:
        if _re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) is None:
            raise ValueError(f"invalid kit name: {self.name}")
        if not self.parts:
            raise ValueError("kit requires at least one part")
        return self


KITS: dict[str, Kit] = {
    "stone_ruin": Kit(
        name="stone_ruin",
        category="structure",
        blocking=True,
        parts=(
            KitPart(
                shape="box",
                offset=(0.0, 0.4, -0.45),
                size=(1.0, 0.8, 0.1),
                material="stone",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 0.4, 0.45),
                size=(1.0, 0.8, 0.1),
                material="stone",
            ),
            KitPart(
                shape="box",
                offset=(0.45, 0.4, 0.0),
                size=(0.1, 0.8, 0.8),
                material="stone",
            ),
            KitPart(
                shape="box",
                offset=(-0.25, 0.1, 0.1),
                size=(0.25, 0.2, 0.25),
                material="rock",
            ),
        ),
    ),
    "timber_hut": Kit(
        name="timber_hut",
        category="structure",
        blocking=True,
        parts=(
            KitPart(
                shape="box",
                offset=(-0.46, 0.45, 0.0),
                size=(0.08, 0.9, 0.92),
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(0.46, 0.45, 0.0),
                size=(0.08, 0.9, 0.92),
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(-0.34, 0.45, 0.46),
                size=(0.32, 0.9, 0.08),
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(0.34, 0.45, 0.46),
                size=(0.32, 0.9, 0.08),
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 0.95, 0.0),
                size=(1.0, 0.1, 1.0),
                material="wood",
            ),
        ),
    ),
    "watchtower": Kit(
        name="watchtower",
        category="structure",
        blocking=True,
        parts=(
            KitPart(
                shape="cylinder",
                offset=(-0.4, 0.35, -0.4),
                radius=0.05,
                height=0.7,
                material="wood",
            ),
            KitPart(
                shape="cylinder",
                offset=(-0.4, 0.35, 0.4),
                radius=0.05,
                height=0.7,
                material="wood",
            ),
            KitPart(
                shape="cylinder",
                offset=(0.4, 0.35, -0.4),
                radius=0.05,
                height=0.7,
                material="wood",
            ),
            KitPart(
                shape="cylinder",
                offset=(0.4, 0.35, 0.4),
                radius=0.05,
                height=0.7,
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 0.7, 0.0),
                size=(0.9, 0.1, 0.9),
                material="stone",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 0.85, -0.4),
                size=(0.8, 0.2, 0.1),
                material="stone",
            ),
        ),
    ),
    "obelisk": Kit(
        name="obelisk",
        category="landmark",
        blocking=False,
        parts=(
            KitPart(
                shape="cylinder",
                offset=(0.0, 1.65, 0.0),
                radius=0.4,
                height=2.5,
                material="snow",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 0.2, 0.0),
                size=(1.2, 0.4, 1.2),
                material="stone",
            ),
        ),
    ),
    "banner": Kit(
        name="banner",
        category="landmark",
        blocking=False,
        parts=(
            KitPart(
                shape="cylinder",
                offset=(0.0, 1.5, 0.0),
                radius=0.1,
                height=3.0,
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(0.6, 2.6, 0.0),
                size=(1.0, 0.5, 0.08),
                material="rock",
            ),
        ),
    ),
    "pine": Kit(
        name="pine",
        category="vegetation",
        blocking=True,
        parts=(
            KitPart(
                shape="cylinder",
                offset=(0.0, 0.7, 0.0),
                radius=0.18,
                height=1.4,
                material="wood",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 1.6, 0.0),
                size=(1.6, 1.0, 1.6),
                material="grass",
            ),
            KitPart(
                shape="box",
                offset=(0.0, 2.45, 0.0),
                size=(1.1, 0.9, 1.1),
                material="grass",
            ),
        ),
    ),
    "shrub": Kit(
        name="shrub",
        category="vegetation",
        blocking=False,
        parts=(
            KitPart(
                shape="box",
                offset=(0.0, 0.4, 0.0),
                size=(0.8, 0.8, 0.8),
                material="grass",
            ),
        ),
    ),
}


def get_kit(name: str) -> Kit:
    """Return a curated kit by name."""

    try:
        return KITS[name]
    except KeyError:
        raise ValueError(f"unknown kit: {name}") from None
