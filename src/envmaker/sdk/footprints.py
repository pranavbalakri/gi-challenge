"""Immutable two-dimensional footprints and pure geometry helpers."""

from __future__ import annotations

from collections.abc import Sequence as _Sequence
import math as _math

from pydantic import BaseModel as _BaseModel
from pydantic import ConfigDict as _ConfigDict
from pydantic import Field as _Field
from pydantic import model_validator as _model_validator

__all__ = [
    "Polygon2D",
    "ObbFit",
    "polygon_area",
    "polygon_centroid",
    "polygon_bounds",
    "polygon_contains",
    "convex_hull",
    "min_area_obb",
]

_Point = tuple[float, float]


def _signed_area(points: _Sequence[_Point]) -> float:
    return 0.5 * sum(
        x1 * z2 - x2 * z1
        for (x1, z1), (x2, z2) in zip(points, (*points[1:], points[0]))
    )


def _orientation(first: _Point, second: _Point, third: _Point) -> int:
    cross = (second[0] - first[0]) * (third[1] - first[1]) - (
        second[1] - first[1]
    ) * (third[0] - first[0])
    if cross > 0.0:
        return 1
    if cross < 0.0:
        return -1
    return 0


def _point_on_segment(start: _Point, end: _Point, point: _Point) -> bool:
    return (
        min(start[0], end[0]) <= point[0] <= max(start[0], end[0])
        and min(start[1], end[1]) <= point[1] <= max(start[1], end[1])
    )


def _segments_intersect(
    first_start: _Point,
    first_end: _Point,
    second_start: _Point,
    second_end: _Point,
) -> bool:
    first_side_start = _orientation(first_start, first_end, second_start)
    first_side_end = _orientation(first_start, first_end, second_end)
    second_side_start = _orientation(second_start, second_end, first_start)
    second_side_end = _orientation(second_start, second_end, first_end)

    if first_side_start * first_side_end < 0 and second_side_start * second_side_end < 0:
        return True
    if first_side_start == 0 and _point_on_segment(
        first_start, first_end, second_start
    ):
        return True
    if first_side_end == 0 and _point_on_segment(first_start, first_end, second_end):
        return True
    if second_side_start == 0 and _point_on_segment(
        second_start, second_end, first_start
    ):
        return True
    return second_side_end == 0 and _point_on_segment(
        second_start, second_end, first_end
    )


def _has_self_intersection(points: tuple[_Point, ...]) -> bool:
    edge_count = len(points)
    for first_index in range(edge_count):
        first_start = points[first_index]
        first_end = points[(first_index + 1) % edge_count]
        for second_index in range(first_index + 1, edge_count):
            if second_index == first_index + 1:
                continue
            if first_index == 0 and second_index == edge_count - 1:
                continue
            second_start = points[second_index]
            second_end = points[(second_index + 1) % edge_count]
            if _segments_intersect(
                first_start,
                first_end,
                second_start,
                second_end,
            ):
                return True
    return False


class Polygon2D(_BaseModel):
    """An immutable simple polygon in the horizontal XZ plane."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    points: tuple[_Point, ...]

    def __init__(
        self,
        points: _Sequence[_Point] | None = None,
        /,
        **data: object,
    ) -> None:
        if points is not None:
            data["points"] = points
        super().__init__(**data)

    @_model_validator(mode="after")
    def _validate_footprint(self) -> Polygon2D:
        if len(self.points) < 3:
            raise ValueError("footprint requires at least 3 points")
        if any(
            not _math.isfinite(coordinate)
            for point in self.points
            for coordinate in point
        ):
            raise ValueError("footprint coordinates must be finite")
        if any(
            self.points[index] == self.points[(index + 1) % len(self.points)]
            for index in range(len(self.points))
        ):
            raise ValueError("footprint edges must not repeat points")
        if _has_self_intersection(self.points):
            raise ValueError("footprint must be a simple polygon")
        if abs(_signed_area(self.points)) == 0.0:
            raise ValueError("footprint area must be positive")
        return self


class ObbFit(_BaseModel):
    """A minimum-area oriented bounding box in the XZ plane."""

    model_config = _ConfigDict(frozen=True, extra="forbid")

    center: _Point
    size_x: float = _Field(gt=0.0)
    size_z: float = _Field(gt=0.0)
    yaw: float


def polygon_area(polygon: Polygon2D) -> float:
    """Return the positive area of a polygon."""

    return abs(_signed_area(polygon.points))


def polygon_centroid(polygon: Polygon2D) -> _Point:
    """Return the area-weighted centroid of a polygon."""

    signed_area = _signed_area(polygon.points)
    weighted_x = 0.0
    weighted_z = 0.0
    for (x1, z1), (x2, z2) in zip(
        polygon.points,
        (*polygon.points[1:], polygon.points[0]),
    ):
        cross = x1 * z2 - x2 * z1
        weighted_x += (x1 + x2) * cross
        weighted_z += (z1 + z2) * cross
    return (
        weighted_x / (6.0 * signed_area),
        weighted_z / (6.0 * signed_area),
    )


def polygon_bounds(polygon: Polygon2D) -> tuple[float, float, float, float]:
    """Return a polygon's axis-aligned XZ bounds."""

    x_coordinates, z_coordinates = zip(*polygon.points)
    return (
        min(x_coordinates),
        min(z_coordinates),
        max(x_coordinates),
        max(z_coordinates),
    )


def polygon_contains(polygon: Polygon2D, x: float, z: float) -> bool:
    """Return whether a point lies inside a polygon by even-odd ray casting."""

    contained = False
    for (x1, z1), (x2, z2) in zip(
        polygon.points,
        (*polygon.points[1:], polygon.points[0]),
    ):
        if (z1 > z) != (z2 > z):
            crossing_x = x1 + (z - z1) * (x2 - x1) / (z2 - z1)
            if crossing_x > x:
                contained = not contained
    return contained


def _cross(origin: _Point, first: _Point, second: _Point) -> float:
    return (first[0] - origin[0]) * (second[1] - origin[1]) - (
        first[1] - origin[1]
    ) * (second[0] - origin[0])


def convex_hull(points: _Sequence[_Point]) -> tuple[_Point, ...]:
    """Return the counter-clockwise monotone-chain hull of a point set."""

    ordered = sorted(set(points))
    if len(ordered) < 3:
        raise ValueError("convex hull requires at least 3 points")

    lower: list[_Point] = []
    for point in ordered:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)

    upper: list[_Point] = []
    for point in reversed(ordered):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)

    hull = tuple(lower[:-1] + upper[:-1])
    if len(hull) < 3:
        raise ValueError("convex hull requires at least 3 points")
    return hull


def _normalize_yaw(yaw: float) -> float:
    return (yaw + _math.pi / 2.0) % _math.pi - _math.pi / 2.0


def min_area_obb(polygon: Polygon2D) -> ObbFit:
    """Return the minimum-area edge-aligned box around a polygon's hull."""

    hull = convex_hull(polygon.points)
    best_area: float | None = None
    best_fit: ObbFit | None = None

    for start, end in zip(hull, (*hull[1:], hull[0])):
        delta_x = end[0] - start[0]
        delta_z = end[1] - start[1]
        edge_length = _math.hypot(delta_x, delta_z)
        axis_x = (delta_x / edge_length, delta_z / edge_length)
        axis_z = (-axis_x[1], axis_x[0])

        local_x = [point[0] * axis_x[0] + point[1] * axis_x[1] for point in hull]
        local_z = [point[0] * axis_z[0] + point[1] * axis_z[1] for point in hull]
        min_x, max_x = min(local_x), max(local_x)
        min_z, max_z = min(local_z), max(local_z)
        size_x = max_x - min_x
        size_z = max_z - min_z
        area = size_x * size_z

        if best_area is None or area < best_area:
            center_x = (min_x + max_x) / 2.0
            center_z = (min_z + max_z) / 2.0
            best_area = area
            best_fit = ObbFit(
                center=(
                    center_x * axis_x[0] + center_z * axis_z[0],
                    center_x * axis_x[1] + center_z * axis_z[1],
                ),
                size_x=size_x,
                size_z=size_z,
                yaw=_normalize_yaw(_math.atan2(axis_x[1], axis_x[0])),
            )

    if best_fit is None:
        raise ValueError("oriented bounding box requires at least 3 hull points")
    return best_fit
