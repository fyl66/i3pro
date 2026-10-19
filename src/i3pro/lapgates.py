"""Pure GPS geometry for choosing lap gates and test figures.

``laps.py`` keeps the public workflow (read a session, choose a method, attach
distance). This module only answers geometric questions about arrays: which
points were revisited, which line gate cuts complete and consistent laps,
whether the path looks like a circuit/figure-eight/run, and which direction a
segment turned.

Keeping these functions here leaves the domain orchestration in ``laps.py``
under the repository's 800-line module guard and makes the scoring testable
without a logger.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np


def _heading(x: np.ndarray, y: np.ndarray, i: int, span: int) -> float:
    j = min(i + span, len(x) - 1)
    return math.atan2(y[j] - y[i], x[j] - x[i])


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def _gate_candidates(
    x: np.ndarray,
    y: np.ndarray,
    speed: np.ndarray,
    speed_threshold: float = 8.0,
    bin_m: float = 10.0,
) -> list[tuple[int, float, float, int]]:
    """Candidate start/finish gates, earliest first.

    The trajectory is quantised into 10 m bins and, for every bin, we count how
    many separate times the car *entered* it across the whole session - that
    count is the number of times the car came back around, i.e. the number of
    laps a gate placed there would produce.

    Candidates are ordered by first visit. ``gps_laps`` separately scores the
    cuts produced by candidate lines because "visited most often" is not the
    same as "best start/finish": a tight figure-of-eight can revisit one point
    dozens of times while a paddock access road can be the first point seen
    three times.

    Each entry is ``(first_index, mean_x, mean_y, entries)``.
    """
    moving = speed > speed_threshold
    index = np.flatnonzero(moving)
    if index.size < 10:
        return []

    bx = np.floor(x[index] / bin_m).astype(np.int64)
    by = np.floor(y[index] / bin_m).astype(np.int64)
    keys = (bx - bx.min()) * (by.max() - by.min() + 2) + (by - by.min())

    enters = np.empty(keys.size, dtype=bool)
    enters[0] = True
    np.not_equal(keys[1:], keys[:-1], out=enters[1:])

    entry_keys = keys[enters]
    entry_rows = index[enters]
    unique_keys, counts = np.unique(entry_keys, return_counts=True)
    count_of = dict(zip(unique_keys.tolist(), counts.tolist()))

    out: list[tuple[int, float, float, int]] = []
    seen: set[int] = set()
    for row, key in zip(entry_rows.tolist(), entry_keys.tolist()):
        if key in seen:
            continue
        seen.add(key)
        members = index[keys == key]
        if members.size == 0:
            continue
        out.append(
            (row, float(np.mean(x[members])), float(np.mean(y[members])), count_of[key])
        )
    return out


@dataclass(frozen=True)
class CutQuality:
    """How convincing one candidate gate looks as a lap boundary."""

    complete: int
    segments: int
    median_time: float
    median_distance: float
    time_cv: float
    distance_cv: float

    def better_than(self, other: "CutQuality") -> bool:
        """Require a real improvement, not another equally shaky cut.

        The legacy gate is deliberately sticky: it is replaced only when it
        produced a weak result and a line gate nearly doubles the number of
        plausible complete laps. This preserves sessions such as the 2026-10-05
        high-avoidance run where "most revisited" was demonstrably worse.
        """
        needed = 2 if other.complete == 0 else other.complete + 2
        if self.complete < max(2, needed):
            return False
        if self.complete != other.complete:
            return True
        return (self.time_cv + self.distance_cv) + 0.15 < (
            other.time_cv + other.distance_cv
        )


def _bounds_from_crossings(
    start: float, end: float, crossings: Iterable[float]
) -> list[tuple[float, float, str, bool]]:
    edges = [start, *[float(c) for c in crossings if start <= float(c) <= end], end]
    return [
        (a, b, str(n), n not in (1, len(edges) - 1))
        for n, (a, b) in enumerate(zip(edges, edges[1:]), start=1)
        if b > a
    ]


def _distance_on_gps(t: np.ndarray, speed_kmh: np.ndarray) -> np.ndarray:
    """Cumulative speed integral on the GPS grid, used only for gate scoring."""
    if t.size < 2:
        return np.zeros(t.size, dtype=float)
    dt = np.diff(t)
    dt[dt <= 0.0] = 0.0
    # A receiver hole is unknown distance, not a straight-line sprint.
    dt[dt > 1.5] = 0.0
    metres_per_second = (speed_kmh[:-1] + speed_kmh[1:]) / 7.2
    return np.concatenate(([0.0], np.cumsum(metres_per_second * dt)))


def _cut_quality(
    bounds: list[tuple[float, float, str, bool]],
    t: np.ndarray,
    distance: np.ndarray,
    min_lap_time: float,
) -> CutQuality:
    """Score complete intervals with the same outlier rule used by the UI."""
    rows: list[tuple[float, float, bool]] = []
    for start, end, _label, complete in bounds:
        if end - start < min_lap_time:
            continue
        i0 = min(int(np.searchsorted(t, start, side="left")), distance.size - 1)
        i1 = min(int(np.searchsorted(t, end, side="right") - 1), distance.size - 1)
        i0 = max(0, i0)
        rows.append((end - start, abs(float(distance[i1] - distance[i0])), complete))
    if not rows:
        return CutQuality(0, 0, 0.0, 0.0, math.inf, math.inf)

    durations = np.asarray([row[0] for row in rows], dtype=float)
    lengths = np.asarray([row[1] for row in rows], dtype=float)
    median_time = float(np.median(durations))
    median_distance = float(np.median(lengths))
    plausible: list[tuple[float, float]] = []
    for duration, length, complete in rows:
        if not complete:
            continue
        if not (0.4 * median_time <= duration <= 2.5 * median_time):
            continue
        if not (0.35 * median_distance <= length <= 1.6 * median_distance):
            continue
        if length < 50.0:
            continue
        plausible.append((duration, length))
    if not plausible:
        return CutQuality(0, len(rows), median_time, median_distance, math.inf, math.inf)
    good_times = np.asarray([row[0] for row in plausible], dtype=float)
    good_lengths = np.asarray([row[1] for row in plausible], dtype=float)
    return CutQuality(
        complete=len(plausible),
        segments=len(rows),
        median_time=float(np.median(good_times)),
        median_distance=float(np.median(good_lengths)),
        time_cv=float(np.std(good_times) / max(np.mean(good_times), 1e-9)),
        distance_cv=float(np.std(good_lengths) / max(np.mean(good_lengths), 1e-9)),
    )


def _crossings_for_gate(
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    segments: list[np.ndarray],
    px: float,
    py: float,
    first_index: int,
    span: int,
    heading_tolerance: float,
    min_lap_time: float,
    gate_radius: float,
) -> list[float]:
    dist = np.hypot(x - px, y - py)
    inside = dist < gate_radius
    heading0 = _heading(x, y, first_index, span)
    crossings: list[float] = []
    last = -math.inf
    for segment in segments:
        segment = segment[segment >= first_index]
        if segment.size < 3:
            continue
        armed = not inside[segment[0]]
        i = 0
        while i < segment.size:
            idx = segment[i]
            if not inside[idx]:
                armed = True
                i += 1
                continue
            if not armed:
                i += 1
                continue
            j = i
            while j + 1 < segment.size and inside[segment[j + 1]]:
                j += 1
            window = segment[i : j + 1]
            k = int(window[int(np.argmin(dist[window]))])
            if (
                _angle_diff(_heading(x, y, k, span), heading0) <= heading_tolerance
                and t[k] - last > min_lap_time
            ):
                crossings.append(float(t[k]))
                last = float(t[k])
            armed = False
            i = j + 1
    return crossings


def _crossings_for_line(
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    segments: list[np.ndarray],
    px: float,
    py: float,
    first_index: int,
    span: int,
    heading_tolerance: float,
    min_lap_time: float,
    half_width: float,
    *,
    directionless: bool = False,
) -> list[float]:
    """Crossings of a short line through a candidate gate."""
    heading0 = _heading(x, y, first_index, span)
    nx, ny = -math.sin(heading0), math.cos(heading0)
    tx, ty = math.cos(heading0), math.sin(heading0)
    normal = (x - px) * nx + (y - py) * ny
    along = (x - px) * tx + (y - py) * ty
    crossings: list[float] = []
    last = -math.inf
    for segment in segments:
        segment = segment[segment >= first_index]
        if segment.size < 2:
            continue
        before = normal[segment[:-1]]
        after = normal[segment[1:]]
        if directionless:
            hits = ((before <= 0.0) & (after > 0.0)) | (
                (before >= 0.0) & (after < 0.0)
            )
        else:
            hits = (before <= 0.0) & (after > 0.0)
        for offset in np.flatnonzero(hits):
            i0 = int(segment[int(offset)])
            i1 = int(segment[int(offset) + 1])
            delta = float(normal[i1] - normal[i0])
            fraction = 0.0 if abs(delta) < 1e-12 else -float(normal[i0]) / delta
            fraction = min(1.0, max(0.0, fraction))
            crossing_along = float(along[i0] + fraction * (along[i1] - along[i0]))
            if abs(crossing_along) > half_width:
                continue
            when = float(t[i0] + fraction * (t[i1] - t[i0]))
            if (
                not directionless
                and _angle_diff(_heading(x, y, i1, span), heading0) > heading_tolerance
            ):
                continue
            if when - last >= min_lap_time:
                crossings.append(when)
                last = when
    return crossings


def _best_alternative_gate(
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    distance: np.ndarray,
    segments: list[np.ndarray],
    candidates: list[tuple[int, float, float, int]],
    first_moving: int,
    span: int,
    heading_tolerance: float,
    min_lap_time: float,
    half_width: float,
) -> tuple[list[float], CutQuality] | None:
    """Return the best directed line gate and its quality."""
    start = float(t[first_moving])
    end = float(t[-1])
    best: tuple[tuple[int, float, int], list[float], CutQuality] | None = None
    for first_index, px, py, entries in candidates:
        if entries < 2:
            continue
        crossings = [
            when
            for when in _crossings_for_line(
                t,
                x,
                y,
                segments,
                px,
                py,
                first_index,
                span,
                heading_tolerance,
                min_lap_time,
                half_width,
            )
            if when >= start
        ]
        if not crossings:
            continue
        bounds = _bounds_from_crossings(start, end, crossings)
        quality = _cut_quality(bounds, t, distance, min_lap_time)
        rank = (
            quality.complete,
            -(quality.distance_cv + quality.time_cv),
            entries,
        )
        if best is None or rank > best[0]:
            best = (rank, crossings, quality)
    if best is None:
        return None
    return best[1], best[2]


def turn_direction(x: np.ndarray, y: np.ndarray) -> int:
    """+1 for a counter-clockwise loop, -1 clockwise, 0 neither."""
    if x.size < 8:
        return 0
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    area = 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))
    footprint = float(np.ptp(x) * np.ptp(y))
    if footprint > 1e-9 and abs(area) >= 0.08 * footprint:
        return 1 if area > 0 else -1
    cx, cy = float(np.mean(x)), float(np.mean(y))
    angle = np.unwrap(np.arctan2(y - cy, x - cx))
    sweep = float(angle[-1] - angle[0])
    if abs(sweep) < 2.0 * math.pi * 0.6:
        return 0
    return 1 if sweep > 0 else -1


def classify_track(x: np.ndarray, y: np.ndarray, speed: np.ndarray) -> str:
    """Classify the driving figure before choosing a segmentation method."""
    moving = np.asarray(speed, dtype=float) > 8.0
    if int(moving.sum()) < 20:
        return "runs"
    xm = np.asarray(x, dtype=float)[moving]
    ym = np.asarray(y, dtype=float)[moving]
    span_x = float(np.ptp(xm))
    span_y = float(np.ptp(ym))
    diagonal = math.hypot(span_x, span_y)
    if diagonal < 15.0:
        return "runs"
    path = float(np.sum(np.hypot(np.diff(xm), np.diff(ym))))
    repeat = path / diagonal
    aspect = min(span_x, span_y) / max(span_x, span_y, 1e-9)
    if repeat < 3.0:
        return "runs"
    if diagonal <= 140.0 and repeat >= 35.0 and aspect >= 0.65:
        return "figure8"
    if aspect < 0.5 and repeat >= 18.0:
        return "runs"
    return "circuit"


def _figure8_quality(
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    bounds: list[tuple[float, float, str, bool]],
    min_lap_time: float,
) -> tuple[int, float, float]:
    """Rank a figure-eight cut by lobe count, alternation and duration stability."""
    complete = [
        (start, end)
        for start, end, _label, done in bounds
        if done and end - start >= min_lap_time
    ]
    if len(complete) < 2:
        return (0, 0.0, math.inf)
    durations = np.asarray([end - start for start, end in complete], dtype=float)
    median = float(np.median(durations))
    labels: list[int] = []
    kept: list[float] = []
    for start, end in complete:
        if not (0.4 * median <= end - start <= 2.5 * median):
            continue
        inside = (t >= start) & (t <= end)
        direction = turn_direction(x[inside], y[inside])
        if direction == 0:
            continue
        labels.append(direction)
        kept.append(end - start)
    if len(labels) < 3:
        return (max(0, len(labels)), 0.0, math.inf)
    alternation = sum(a != b for a, b in zip(labels, labels[1:])) / (len(labels) - 1)
    kept_array = np.asarray(kept, dtype=float)
    cv = float(np.std(kept_array) / max(np.mean(kept_array), 1e-9))
    return (len(labels), alternation, cv)


def _best_figure8_gate(
    t: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    segments: list[np.ndarray],
    candidates: list[tuple[int, float, float, int]],
    first_moving: int,
    span: int,
    min_lap_time: float,
) -> list[tuple[float, float, str, bool]] | None:
    """Find a center gate that cuts a figure-of-eight into alternating lobes."""
    start, end = float(t[first_moving]), float(t[-1])
    best: tuple[tuple[int, float, float], list[tuple[float, float, str, bool]]] | None = None
    for first_index, px, py, entries in candidates:
        if entries < 3:
            continue
        crossings = [
            when
            for when in _crossings_for_line(
                t,
                x,
                y,
                segments,
                px,
                py,
                first_index,
                span,
                math.radians(180.0),
                min_lap_time,
                12.0,
                directionless=True,
            )
            if when >= start
        ]
        if len(crossings) < 4:
            continue
        bounds = _bounds_from_crossings(start, end, crossings)
        quality = _figure8_quality(t, x, y, bounds, min_lap_time)
        rank = (quality[0], quality[1], -quality[2])
        if best is None or rank > best[0]:
            best = (rank, bounds)
    if best is None or best[0][0] < 6:
        return None
    return best[1]
