"""Lap segmentation, distance-axis overlay and time-delta analysis.

The team's C125 logs are recorded with the beacon input unwired: ``Beacon``,
``Lap Number`` and ``Distance`` are dead (constant) in every file, so i2 Pro
itself reports "Total Laps = 1". i3pro therefore detects laps from the GPS
trajectory:

* ``gps``     - start/finish gate crossings with heading + direction filtering
                (default, works for any closed course),
* ``winding`` - total angle swept around the track centroid (robust for oval /
                convex courses),
* ``beacon``  - the logger's own lap/beacon channels, used when they are live.

``overlay()`` is the MoTeC signature feature: two laps resampled onto one
shared *distance* axis so they can be compared corner by corner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

import numpy as np

from . import derive
from . import ld as ldmod

__all__ = [
    "Lap",
    "detect_laps",
    "lap_table",
    "overlay",
    "time_delta",
    "gps_laps",
]

LAP_NUMBER_CHANNELS = ("Lap Number", "Lap counter", "Lap No")
BEACON_CHANNELS = ("Beacon", "Beacon Number")


@dataclass
class Lap:
    index: int           # 0-based position inside the log
    label: str           # human label (1-based)
    start_time: float
    end_time: float
    start_distance: float
    end_distance: float
    complete: bool = True

    @property
    def lap_time(self) -> float:
        return self.end_time - self.start_time

    @property
    def distance(self) -> float:
        return self.end_distance - self.start_distance

    def as_row(self) -> dict:
        return {
            "lap": self.label,
            "lap_time": round(self.lap_time, 3),
            "delta_to_best": None,
            "distance": round(self.distance, 1),
            "start_time": round(self.start_time, 3),
            "end_time": round(self.end_time, 3),
            "complete": self.complete,
        }


# --------------------------------------------------------------- helpers
def _channel_varies(log: ldmod.LogFile, candidates: Iterable[str], spread: float = 0.5) -> str | None:
    for name in candidates:
        if not log.has(name):
            continue
        values = log.values(name)
        if values.size and float(np.nanmax(values) - np.nanmin(values)) > spread:
            return name
    return None


def _counter_channel(
    log: ldmod.LogFile, candidates: Iterable[str], min_steps: int = 3
) -> str | None:
    """Return a channel that really behaves like a lap/beacon counter.

    The C125 in this car has its beacon input unwired, so ``Beacon`` and
    ``Lap Number`` are not constant - they toggle between 0 and -1 with the raw
    CAN byte. Treating that as a lap trigger produces garbage, so a candidate
    must be non-decreasing and actually step at least ``min_steps`` times.
    """
    for name in candidates:
        if not log.has(name):
            continue
        values = np.nan_to_num(log.values(name))
        if values.size < 10:
            continue
        steps = np.diff(values)
        if float(np.mean(steps >= -0.5)) < 0.98:
            continue
        # a real lap counter climbs to at least `min_steps`; the unwired beacon
        # in this car only ever toggles between -1 and 0
        if int(np.sum(steps > 0.5)) >= min_steps and values.max() - values.min() >= min_steps:
            return name
    return None


def _master_time(log: ldmod.LogFile) -> np.ndarray:
    n = int(round(log.duration * log.sample_rate)) + 1
    return np.arange(n, dtype=np.float64) / log.sample_rate


def _lap_from_bounds(
    log: ldmod.LogFile,
    distance: np.ndarray,
    bounds: list[tuple[float, float, str, bool]],
) -> list[Lap]:
    rate = log.sample_rate
    laps: list[Lap] = []
    for start, end, label, complete in bounds:
        i0 = int(round(start * rate))
        i1 = min(int(round(end * rate)), distance.size - 1)
        laps.append(
            Lap(
                index=len(laps),
                label=label,
                start_time=float(start),
                end_time=float(end),
                start_distance=float(distance[i0]) if distance.size else math.nan,
                end_distance=float(distance[i1]) if distance.size else math.nan,
                complete=complete,
            )
        )
    return laps


def _bounds_from_labels(time: np.ndarray, labels: np.ndarray) -> list[tuple[float, float, str, bool]]:
    values = np.nan_to_num(labels)
    edges = [0, *(np.flatnonzero(np.diff(values) != 0) + 1), len(time)]
    out = []
    for n, (a, b) in enumerate(zip(edges, edges[1:]), start=1):
        if b <= a:
            continue
        out.append((float(time[a]), float(time[b - 1]), str(n), True))
    return out


def _bounds_from_beacon(time: np.ndarray, beacon: np.ndarray) -> list[tuple[float, float, str, bool]]:
    crossings = np.flatnonzero(np.diff(np.nan_to_num(beacon)) > 0.5)
    edges = [0, *(crossings + 1), len(time)]
    out = []
    for n, (a, b) in enumerate(zip(edges, edges[1:]), start=1):
        if b <= a:
            continue
        out.append((float(time[a]), float(time[b - 1]), str(n), True))
    return out


# ------------------------------------------------------------------ GPS
def _heading(x: np.ndarray, y: np.ndarray, i: int, span: int) -> float:
    j = min(i + span, len(x) - 1)
    return math.atan2(y[j] - y[i], x[j] - x[i])


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

    Candidates are ordered by first visit, and ``gps_laps`` takes the earliest
    one that is revisited at least three times. That anchors the lap boundary
    at the launch point (where the car really did start) while automatically
    skipping places that are only ever seen once - the pit box, a spin, or the
    access road to the track.

    Each entry is ``(first_index, mean_x, mean_y, entries)``.
    """
    moving = speed > speed_threshold
    index = np.flatnonzero(moving)
    if index.size < 10:
        return []

    bx = np.floor(x[index] / bin_m).astype(np.int64)
    by = np.floor(y[index] / bin_m).astype(np.int64)
    # pack the 2-D bin into one integer so a single np.unique does the counting
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


def _angle_diff(a: float, b: float) -> float:
    return abs((a - b + math.pi) % (2 * math.pi) - math.pi)


def gps_laps(
    log: ldmod.LogFile,
    gate_radius: float = 30.0,
    min_lap_time: float = 8.0,
    speed_threshold: float = 8.0,
    heading_tolerance: float = math.radians(75.0),
) -> list[tuple[float, float, str, bool]]:
    """Start/finish gate crossing detection on the GPS trajectory."""
    track = derive.gps_track(log)
    t = track["time"]
    x, y = track["x"], track["y"]
    master_t = _master_time(log)
    try:
        speed = np.interp(t, master_t, derive.speed_series(log))
    except ValueError:
        # No live speed channel (some logs are recorded parked with a dead
        # speed bus): fall back to the GPS path derivative, smoothed so that
        # receiver jitter while parked does not look like motion.
        dt = np.gradient(t)
        dt[dt <= 0] = 1.0 / max(track["rate"], 1.0)
        raw = np.hypot(np.gradient(x) / dt, np.gradient(y) / dt) * 3.6
        window = max(1, int(track["rate"] * 2))
        kernel = np.ones(window) / window
        speed = np.convolve(raw, kernel, mode="same")
        if float(np.median(speed)) < 10.0:
            raise ValueError(
                f"{log.path.name}: 速度通道全部失效/GPS 无效（中位速度 "
                f"{float(np.median(speed)):.1f} km/h），无法可靠切圈"
            )
    span = max(1, int(round(1.0 * track["rate"])))
    # Split the track where the receiver dropped out: a position jump across a
    # gap must never be mistaken for a start/finish crossing.
    gaps = np.flatnonzero(np.diff(t) > 1.5) + 1
    segments = [s for s in np.split(np.arange(len(t)), gaps) if s.size > 2]

    # Driving phase: first moment the car is *sustainedly* moving.
    moving = speed > speed_threshold
    hold = max(1, int(round(track["rate"] * 3.0)))
    sustained = np.convolve(moving.astype(int), np.ones(hold, dtype=int), mode="valid") >= hold
    if not sustained.any():
        raise ValueError(
            f"{log.path.name}: 车辆全程未行驶（速度通道恒为 0），没有可切分的圈次"
        )
    first_moving = int(np.argmax(sustained))
    # Anchor the gate at the earliest place the car passes *and comes back to*:
    # that is the launch point on a normal test day, and it automatically skips
    # one-off positions such as the pit box. A gate circle that scales with the
    # circuit stops 30 m from swallowing a whole 200 m figure-of-eight.
    diagonal = float(np.hypot(np.ptp(x), np.ptp(y)))
    gate_radius = max(6.0, min(gate_radius, 0.15 * diagonal))
    candidates = _gate_candidates(x, y, speed, speed_threshold)
    gate = next((c for c in candidates if c[3] >= 3), None)
    if gate is not None:
        i0, px, py, _passes = gate
    else:
        i0, px, py = first_moving, float(x[first_moving]), float(y[first_moving])

    crossings = _crossings_for_gate(
        t,
        x,
        y,
        segments,
        px,
        py,
        first_index=i0,
        span=span,
        heading_tolerance=heading_tolerance,
        min_lap_time=min_lap_time,
        gate_radius=gate_radius,
    )

    start = float(t[first_moving])
    end = float(t[-1])
    # A brief twitch before the sustained driving phase can register as a
    # crossing; the lap clock starts when the car really gets going.
    crossings = [c for c in crossings if c >= start]
    edges = [start, *crossings, end]
    bounds: list[tuple[float, float, str, bool]] = []
    for n, (a, b) in enumerate(zip(edges, edges[1:]), start=1):
        if b - a <= 0:
            continue
        complete = n not in (1, len(edges) - 1)
        bounds.append((a, b, str(n), complete))
    return bounds


def winding_laps(log: ldmod.LogFile, min_lap_time: float = 8.0) -> list[tuple[float, float, str, bool]]:
    """Laps from the angle swept around the centroid of the GPS track."""
    track = derive.gps_track(log)
    t, x, y = track["time"], track["x"], track["y"]
    cx, cy = float(np.mean(x)), float(np.mean(y))
    angle = np.unwrap(np.arctan2(y - cy, x - cx))
    turns = (angle - angle[0]) / (2 * math.pi)
    start = t[0]
    crossings: list[float] = []
    last = -math.inf
    for k in range(1, int(np.floor(turns[-1] - turns[0])) + 1):
        idx = int(np.argmax(turns >= turns[0] + k))
        if t[idx] - last > min_lap_time:
            crossings.append(float(t[idx]))
            last = float(t[idx])
    edges = [start, *crossings, float(t[-1])]
    bounds = []
    for n, (a, b) in enumerate(zip(edges, edges[1:]), start=1):
        if b - a <= 0:
            continue
        bounds.append((a, b, str(n), n not in (1, len(edges) - 1)))
    return bounds


# ------------------------------------------------------------------- API
def detect_laps(log: ldmod.LogFile, method: str = "auto", **kwargs) -> list[Lap]:
    """Split a log into laps and attach cumulative distance to each one."""
    try:
        distance = derive.distance_series(log)
    except ValueError:
        # dead speed bus: fall back to the GPS path length
        track = derive.gps_track(log)
        rate = log.sample_rate
        time = _master_time(log)
        gps_speed = np.gradient(track["x"]), np.gradient(track["y"])
        gps_speed = np.hypot(*gps_speed) * track["rate"]  # m/s on the GPS grid
        distance = np.interp(time, track["time"], np.cumsum(gps_speed) / track["rate"])
    if method in ("auto", "beacon"):
        label_channel = _counter_channel(log, LAP_NUMBER_CHANNELS)
        if label_channel:
            time = _master_time(log)
            bounds = _bounds_from_labels(time, derive.hold_to_master(log, label_channel))
        else:
            beacon_channel = _counter_channel(log, BEACON_CHANNELS)
            if beacon_channel:
                time = _master_time(log)
                bounds = _bounds_from_beacon(time, derive.hold_to_master(log, beacon_channel))
            elif method == "beacon":
                raise ValueError(f"{log.path.name}: no live beacon/lap channel")
            else:
                bounds = gps_laps(log, **kwargs)
    elif method == "gps":
        bounds = gps_laps(log, **kwargs)
    elif method == "winding":
        bounds = winding_laps(log, **kwargs)
    else:
        raise ValueError(f"unknown lap detection method: {method!r}")
    min_time = kwargs.get("min_lap_time", 8.0)
    bounds = [b for b in bounds if b[1] - b[0] >= min_time]
    laps = _lap_from_bounds(log, distance, bounds)
    return _flag_implausible(laps)


def _flag_implausible(laps: list[Lap]) -> list[Lap]:
    """Mark segments that cannot be a real lap (pit stop, missed crossing, GPS gap).

    A segment is kept as ``complete`` only when both its duration and its length
    are within a sane band around the session median; outliers stay visible in
    the table but are excluded from fastest-lap statistics.
    """
    if len(laps) < 3:
        return laps
    times = np.array([l.lap_time for l in laps], dtype=float)
    lengths = np.array([l.distance for l in laps], dtype=float)
    median_time = float(np.median(times))
    median_len = float(np.median(lengths))
    for lap in laps:
        if not lap.complete:
            continue
        bad_time = not (0.4 * median_time <= lap.lap_time <= 2.5 * median_time)
        bad_length = not (0.35 * median_len <= lap.distance <= 1.6 * median_len)
        if bad_time or bad_length or lap.distance < 50.0:
            lap.complete = False
    return laps


def lap_table(log: ldmod.LogFile, laps: list[Lap] | None = None) -> list[dict]:
    laps = laps if laps is not None else detect_laps(log)
    complete = [l.lap_time for l in laps if l.complete] or [l.lap_time for l in laps]
    best = min(complete, default=0.0)
    rows = []
    for lap in laps:
        row = lap.as_row()
        row["delta_to_best"] = round(lap.lap_time - best, 3)
        rows.append(row)
    return rows


def overlay(
    log: ldmod.LogFile,
    laps: list[Lap],
    channels: list[str],
    step: float = 1.0,
) -> dict:
    """Resample the given laps onto one shared distance grid."""
    rate = log.sample_rate
    time = _master_time(log)
    distance = derive.distance_series(log)
    distance = distance[: time.size]
    series = {name: derive.hold_to_master(log, name) for name in channels}

    laps_out = []
    for lap in laps:
        i0 = int(round(lap.start_time * rate))
        i1 = min(int(round(lap.end_time * rate)), distance.size - 1)
        d = np.maximum.accumulate(np.asarray(distance[i0 : i1 + 1], dtype=np.float64))
        d = d - d[0]
        length = float(d[-1]) if d.size else 0.0
        grid = np.arange(0.0, length + step, step)
        row = {"lap": lap.label, "lap_time": lap.lap_time, "length": length}
        row["time"] = np.interp(grid, d, time[i0 : i1 + 1] - lap.start_time)
        for name, values in series.items():
            row[name] = np.interp(grid, d, values[i0 : i1 + 1])
        laps_out.append(row)

    grid_len = max((len(l["time"]) for l in laps_out), default=0)
    for row in laps_out:
        for key, value in list(row.items()):
            if isinstance(value, np.ndarray) and value.size < grid_len:
                row[key] = np.pad(value, (0, grid_len - value.size), constant_values=np.nan)
    return {
        "distance": np.arange(0.0, grid_len * step, step),
        "laps": laps_out,
        "step": step,
    }


def time_delta(ref: dict, cmp: dict, distance: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Cumulative time difference (compare - reference) over the distance grid."""
    if distance is None:
        candidate = ref.get("distance")
        distance = candidate if isinstance(candidate, np.ndarray) else np.asarray(
            ref.get("grid", np.arange(len(ref["time"]))), dtype=np.float64
        )
    delta = np.nan_to_num(cmp["time"]) - np.nan_to_num(ref["time"])
    delta[np.isnan(np.asarray(cmp["time"], dtype=np.float64))
          | np.isnan(np.asarray(ref["time"], dtype=np.float64))] = np.nan
    return distance, delta
