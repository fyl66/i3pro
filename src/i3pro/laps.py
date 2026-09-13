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
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

import numpy as np

from . import derive
from . import gpsfix
from . import ld as ldmod
from . import timebase

__all__ = [
    "Beacon",
    "Lap",
    "LapConfig",
    "reconcile_edits",
    "check_new_crossings",
    "insertion_notice",
    "time_at_distance",
    "distance_on_master",
    "clean_name",
    "unique_name",
    "detect_laps",
    "detect_from_config",
    "load_config",
    "save_config",
    "config_path",
    "lap_table",
    "overlay",
    "time_delta",
    "gps_laps",
    "run_laps",
    "figure8_laps",
    "turn_direction",
]

#: Sidecar written next to the ``.ld`` file. The ``.ld`` itself stays read-only
#: (see AGENTS.md); everything the user edits about laps lives here.
CONFIG_SUFFIX = ".laps.json"

#: A beacon name becomes the prefix of every lap label in its series
#: (``左环 3``), so it stays short enough for the side panel and a share link.
MAX_BEACON_NAME = 24
DEFAULT_BEACON_NAME = "信标"
DEFAULT_CROSSING_NAME = "手工穿越"
#: A hand-entered crossing this close to a boundary that already exists is that
#: boundary - see ``_merge_crossings``.
CROSSING_SNAP = 0.05

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
    turn: str | None = None   # "left" / "right" for figure-of-eight loops

    @property
    def lap_time(self) -> float:
        return self.end_time - self.start_time

    @property
    def distance(self) -> float:
        return self.end_distance - self.start_distance

    def as_row(self) -> dict:
        return {
            "lap": self.label,
            "turn": self.turn,
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
    gate: tuple[float, float] | None = None,
    fix: "gpsfix.FixConfig | None" = None,
) -> list[tuple[float, float, str, bool]]:
    """Start/finish gate crossing detection on the GPS trajectory.

    ``gate`` is an explicit (lat, lon) start/finish point - the manual beacon the
    user drops on the track map. Without it the gate is chosen automatically.
    ``fix`` 是这一场的 GPS 校正；只有它自己开着 ``enabled`` 且 ``scope_laps``
    打开时才生效（关闭时与旧实现完全一致）。
    """
    config = gpsfix.resolve(log, fix, "laps")
    track = derive.gps_track(log, fix=config, scope="laps")
    t = track["time"]
    x, y = track["x"], track["y"]
    master_t = timebase.axis(log)
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
    if config.enabled and "breaks" in track:
        # 校正打开时，跳点也当成一次断开：一次 214 m 的错位定位不该被认成过门。
        gaps = np.union1d(gaps, np.flatnonzero(track["breaks"]))
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
    if gate is not None:
        lat0, lon0 = track.get("origin", (float(track["lat"][0]), float(track["lon"][0])))
        px = (float(gate[1]) - lon0) * 111_320.0 * math.cos(math.radians(lat0))
        py = (float(gate[0]) - lat0) * 110_540.0
        i0 = int(np.argmin(np.hypot(x - px, y - py)))
    else:
        candidates = _gate_candidates(x, y, speed, speed_threshold)
        chosen = next((c for c in candidates if c[3] >= 3), None)
        if chosen is not None:
            i0, px, py, _passes = chosen
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


def winding_laps(
    log: ldmod.LogFile,
    min_lap_time: float = 8.0,
    fix: "gpsfix.FixConfig | None" = None,
) -> list[tuple[float, float, str, bool]]:
    """Laps from the angle swept around the centroid of the GPS track."""
    track = derive.gps_track(log, fix=gpsfix.resolve(log, fix, "laps"), scope="laps")
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
def turn_direction(x: np.ndarray, y: np.ndarray) -> int:
    """+1 for a predominantly left (counter-clockwise) loop, -1 right, 0 neither.

    The path is measured by how far it winds around its own centroid: a closed
    loop sweeps ±2π, a there-and-back path sweeps ~0. In the local projection
    (x east, y north) a positive sweep is a left-hand loop, which is what we need
    to label the two halves of a figure-of-eight.
    """
    if x.size < 8:
        return 0
    cx, cy = float(np.mean(x)), float(np.mean(y))
    angle = np.unwrap(np.arctan2(y - cy, x - cx))
    sweep = float(angle[-1] - angle[0])
    if abs(sweep) < 2.0 * math.pi * 0.6:
        return 0
    return 1 if sweep > 0 else -1


def run_laps(
    log: ldmod.LogFile,
    stop_speed: float = 3.0,
    gap: float = 3.0,
    min_run: float = 4.0,
) -> list[tuple[float, float, str, bool]]:
    """Split by *run*: sustained movement separated by sustained standstill.

    Figure-of-eight, acceleration and skidpad tests have no laps at all - the
    meaningful unit is one attempt, i.e. from when the car pulls away to when it
    stops again.
    """
    time = timebase.axis(log)
    speed = derive.speed_series(log) / 3.6      # km/h -> m/s
    moving = speed > max(0.0, stop_speed / 3.6)
    index = np.flatnonzero(moving)
    if index.size < 2:
        return []
    edges = np.flatnonzero(np.diff(index) > gap * log.sample_rate)
    bounds: list[tuple[float, float, str, bool]] = []
    for run in np.split(index, edges + 1):
        if run.size < 2:
            continue
        start, end = float(time[run[0]]), float(time[run[-1]])
        if end - start < min_run:
            continue
        bounds.append((start, end, str(len(bounds) + 1), True))
    return bounds


def figure8_laps(log: ldmod.LogFile, **kwargs) -> list[tuple[float, float, str, bool]]:
    """Split a figure-of-eight into one segment per loop (left / right).

    On a figure-of-eight the trajectory crosses itself, so a gate placed on a
    loop is met twice per cycle - the plain GPS detector already returns
    roughly one segment per loop on the team's real 八字 log. What it cannot do
    is say which loop a segment is, so ``detect_laps`` labels each one by its
    turn direction.
    """
    return gps_laps(log, **kwargs)


def detect_laps(
    log: ldmod.LogFile,
    method: str = "auto",
    gate: tuple[float, float] | None = None,
    beacons: list[float] | None = None,
    fix: "gpsfix.FixConfig | None" = None,
    **kwargs,
) -> list[Lap]:
    """Split a log into laps and attach cumulative distance to each one.

    ``gate`` is an explicit start/finish point as (lat, lon) - it replaces the
    automatically chosen one. ``beacons`` is an explicit list of crossing times
    in seconds, which wins over everything else.
    ``fix`` 是这一场的 GPS 校正（``gpsfix.FixConfig``）：距离轴按 ``scope_distance``、
    切圈按 ``scope_laps`` 决定要不要用它。
    """
    laps_fix = gpsfix.resolve(log, fix, "laps")
    try:
        distance = derive.distance_series(log, fix=fix)
    except ValueError:
        # dead speed bus: fall back to the GPS path length
        track = derive.gps_track(log, fix=laps_fix, scope="laps")
        rate = log.sample_rate
        time = timebase.axis(log)
        gps_speed = np.gradient(track["x"]), np.gradient(track["y"])
        gps_speed = np.hypot(*gps_speed) * track["rate"]  # m/s on the GPS grid
        distance = np.interp(time, track["time"], np.cumsum(gps_speed) / track["rate"])
    time = timebase.axis(log)
    if beacons:
        edges = sorted(float(b) for b in beacons)
        bounds = [
            (start, end, str(n), True)
            for n, (start, end) in enumerate(zip(edges, edges[1:]), start=1)
            if end > start
        ]
    elif gate is not None:
        bounds = gps_laps(log, gate=gate, fix=fix, **kwargs)
    elif method in ("auto", "beacon"):
        label_channel = _counter_channel(log, LAP_NUMBER_CHANNELS)
        if label_channel:
            bounds = _bounds_from_labels(time, derive.hold_to_master(log, label_channel))
        else:
            beacon_channel = _counter_channel(log, BEACON_CHANNELS)
            if beacon_channel:
                bounds = _bounds_from_beacon(time, derive.hold_to_master(log, beacon_channel))
            elif method == "beacon":
                raise ValueError(f"{log.path.name}: no live beacon/lap channel")
            else:
                bounds = gps_laps(log, fix=fix, **kwargs)
    elif method == "gps":
        bounds = gps_laps(log, fix=fix, **kwargs)
    elif method == "winding":
        bounds = winding_laps(log, fix=fix, **kwargs)
    elif method == "run":
        bounds = run_laps(log, **kwargs)
    elif method == "figure8":
        bounds = figure8_laps(log, fix=fix, **kwargs)
    else:
        raise ValueError(f"unknown lap detection method: {method!r}")
    min_time = kwargs.get("min_lap_time", 8.0)
    if method != "run":
        bounds = [b for b in bounds if b[1] - b[0] >= min_time]
    laps = _lap_from_bounds(log, distance, bounds)
    if method == "figure8":
        try:
            track = derive.gps_track(log, fix=laps_fix, scope="laps")
        except ValueError:
            track = None
        if track is not None:
            for lap in laps:
                inside = ((track["time"] >= lap.start_time) & (track["time"] <= lap.end_time))
                turn = turn_direction(track["x"][inside], track["y"][inside])
                lap.turn = {1: "left", -1: "right"}.get(turn)
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
    fix: "gpsfix.FixConfig | None" = None,
) -> dict:
    """Resample the given laps onto one shared distance grid."""
    rate = log.sample_rate
    time = timebase.axis(log)
    distance = derive.distance_series(log, fix=fix)
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


# --------------------------------------------------------------- sidecar
@dataclass
class Beacon:
    """**One crossing of the start/finish line** - not the line itself.

    Its position (where the line is drawn) and its time (when the car went
    through) are both attributes of the same thing. Detection gives the time
    from the position; a missed crossing can be entered as a time alone.
    One beacon yields one independent lap series.
    """

    name: str
    lat: float | None = None
    lon: float | None = None
    time: float | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None

    def as_dict(self) -> dict:
        out: dict = {"name": self.name}
        if self.lat is not None:
            out["lat"] = round(float(self.lat), 7)
        if self.lon is not None:
            out["lon"] = round(float(self.lon), 7)
        if self.time is not None:
            out["time"] = round(float(self.time), 4)
        return out


@dataclass
class LapConfig:
    """Everything the user has decided about this session's laps.

    Stored as ``<session>.laps.json`` next to the log. Keeping it out of the
    ``.ld``/``.ldx`` preserves the "logs are read-only" rule and lets the file
    travel with the data folder, be diffed in git, and ride along a share link.
    """

    mode: str = "auto"                       # auto | run | figure8
    #: Beacons in the sense defined above. One beacon = one lap series, so a
    #: figure-of-eight gets two (left loop + right loop) and each is timed
    #: independently - the only reliable way on a self-crossing course.
    beacons: list[Beacon] = field(default_factory=list)
    trusted: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "beacons": [b.as_dict() for b in self.beacons],
            "trusted": dict(self.trusted),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LapConfig":
        """Read the current shape **and** every shape written before the merge.

        Older sidecars stored the line as ``gate`` / ``gates`` and the times as
        a bare list of ``beacons`` floats. Both must keep loading: a user who
        placed beacons before the merge must not lose them.
        """
        beacons: list[Beacon] = []
        for item in data.get("beacons") or []:
            if isinstance(item, dict):
                beacons.append(
                    Beacon(
                        name=str(item.get("name") or f"信标{len(beacons) + 1}"),
                        lat=_opt_float(item.get("lat")),
                        lon=_opt_float(item.get("lon")),
                        time=_opt_float(item.get("time")),
                    )
                )
            else:                                   # old: a bare crossing time
                beacons.append(Beacon(name=f"信标{len(beacons) + 1}", time=float(item)))
        for index, item in enumerate(data.get("gates") or []):   # old: named lines
            if isinstance(item, dict) and item.get("lat") is not None:
                beacons.append(
                    Beacon(name=str(item.get("name") or f"信标{index + 1}"),
                           lat=float(item["lat"]), lon=float(item.get("lon")))
                )
            elif isinstance(item, (list, tuple)) and len(item) == 3:  # (name, lat, lon)
                beacons.append(Beacon(name=str(item[0]), lat=float(item[1]), lon=float(item[2])))
        gate = data.get("gate")                     # old: a single unnamed line
        if gate and len(gate) == 2:
            beacons.append(Beacon(name=f"信标{len(beacons) + 1}",
                                  lat=float(gate[0]), lon=float(gate[1])))
        return cls(
            mode=str(data.get("mode") or "auto"),
            beacons=beacons,
            trusted={str(k): bool(v) for k, v in (data.get("trusted") or {}).items()},
        )


def _opt_float(value) -> float | None:
    return None if value is None else float(value)


# ------------------------------------------------------------ editing rules
def reconcile_edits(old: LapConfig, new: LapConfig) -> LapConfig:
    """Apply the beacon-name rules to a config the user just edited.

    The UI sends the whole config, so the rules live here, once, instead of being
    re-implemented by every caller (and re-implemented slightly differently each
    time): a name is trimmed, an empty name falls back to what the beacon was
    called before, duplicates get a numeric suffix, and over-long names are
    truncated.

    Renaming also carries the ``trusted`` marks over to the new label. Labels are
    ``<beacon name> <lap number>``, so without this a rename would silently lose
    every "this lap is untrusted" decision the user made in the lap table.

    Names that did not change are left exactly as they are - an existing sidecar
    must not be rewritten behind the user's back.
    """
    beacons = list(new.beacons)
    trusted = dict(new.trusted)
    pairs = _pair_beacons(old.beacons, beacons)
    for index, beacon in enumerate(beacons):
        before = pairs[index]
        if before is not None and before.name == beacon.name:
            continue
        fallback = before.name if before is not None else DEFAULT_BEACON_NAME
        name = unique_name(
            clean_name(beacon.name, fallback),
            [b.name for other, b in enumerate(beacons) if other != index],
        )
        beacons[index] = replace(beacon, name=name)
        if before is not None:
            trusted = _migrate_trusted(trusted, before.name, name)
    return replace(new, beacons=beacons, trusted=trusted)


def _pair_beacons(old: list[Beacon], new: list[Beacon]) -> list[Beacon | None]:
    """Say which beacon each edited beacon came from.

    The client sends the whole list, so the pairing rule decides whether an edit
    reads as a rename (marks follow) or as a delete + an insert (marks do not).
    Positions alone get a delete wrong: removing the middle of
    ``[左环, 右环, 手工穿越]`` slides ``手工穿越`` into slot 1 and hands it
    ``右环``'s marks. Names alone get a rename-onto-an-existing-name wrong: two
    beacons called ``右环`` would move the marks sideways to a different line.

    So: an edit that keeps the length is a rename in place (pair by position, the
    physical beacon is what the marks belong to); an edit that changes the length
    inserted or deleted something (pair by name, which is what survives both).
    Leftovers are paired by position only when that is unambiguous. A beacon that
    is simply gone comes back as ``None`` and carries no marks anywhere.
    """
    if len(old) == len(new):
        return [before for before in old]
    pairs: list[Beacon | None] = [None] * len(new)
    claimed: set[int] = set()
    for index, beacon in enumerate(new):
        for other, before in enumerate(old):
            if other not in claimed and before.name == beacon.name:
                pairs[index] = before
                claimed.add(other)
                break
    left_old = [index for index in range(len(old)) if index not in claimed]
    left_new = [index for index in range(len(new)) if pairs[index] is None]
    if len(left_old) == len(left_new):                  # one rename + one insert
        for before_index, index in zip(left_old, left_new):
            pairs[index] = old[before_index]
    return pairs


def check_new_crossings(old: LapConfig, new: LapConfig, duration: float) -> str | None:
    """Reject a hand-entered crossing that is not inside this session.

    Returns a message for the user (in Chinese, saying what to do next) or
    ``None`` when the edit is fine. Only crossings that are *new* in this edit
    are checked: an out-of-range time that already sits in a sidecar must never
    lock the user out of editing that session. "New" is judged by the moment
    itself and not by the name, so renaming such a crossing still works.
    """
    known = _crossing_times(old)
    for beacon in new.beacons:
        if beacon.has_position or beacon.time is None:
            continue
        if round(float(beacon.time), 4) in known:
            continue
        if not math.isfinite(beacon.time):
            return f"信标「{beacon.name}」缺少穿越时刻"
        if not 0.0 <= beacon.time <= duration:
            return (
                f"「{beacon.name}」的穿越时刻 {beacon.time:.3f} s 超出本场时长 "
                f"0–{duration:.1f} s，请把光标放到图上再插入"
            )
    return None


def insertion_notice(
    old: LapConfig, new: LapConfig, laps_before: int, laps_after: int
) -> str | None:
    """Warn when a hand-entered crossing did not split anything.

    A crossing *is* a boundary, so a crossing that lands inside a series always
    changes the lap count. When it does not - the session has no boundary to
    insert into yet, or the moment coincides with one that is already there - the
    user has to be told, because a button that quietly does nothing reads as
    "the program is broken".
    """
    added = _crossing_times(new) - _crossing_times(old)
    if not added:
        return None
    if laps_after > laps_before:
        return None
    return (
        "这次穿越没有切出新圈：它和已有边界重合，或者本场还没有可插入的边界。"
        "请放大到那一圈、把光标放准再插一次。"
    )


def same_config(current: LapConfig, other: LapConfig | None) -> bool:
    """是不是同一版配置。

    「撤销」按钮的判据就在这里：上一版和当前这一版一模一样时，撤销没有东西可撤，
    控件应当是灰的，而不是点下去什么都不发生。比较用 :meth:`LapConfig.as_dict`
    ——那正是落盘与过线的形状，所以"同一版"指的是**边车里同一版**：经纬度比到
    小数第 7 位、时刻比到小数第 4 位（``as_dict`` 的舍入），再细的差别本来也存不下去。
    """
    if other is None:
        return False
    return current.as_dict() == other.as_dict()


def undo_config(current: LapConfig, previous: LapConfig | None) -> LapConfig | None:
    """撤销上一步：把**上一版配置原样交回来**；没有可撤销的一步时给 ``None``。

    撤销不是"反向编辑"，而是"把上一版再提交一次"。**原样**是关键：改名撤销之后
    「可信 / 不可信」标记回来的原因是它们本来就挂在旧名字上（上一版就是这么存的），
    不是有人又迁移了一遍；插入撤销之后那条边界不在上一版里；删掉的信标还在列表里。
    所以调用方拿到它之后只需要落盘，不必再跑 ``reconcile_edits``——上一版正是那些
    规则自己的输出。

    调用方负责保管 ``previous``——按 ticket 的约定只留一版（一个槽，落在服务内存里），
    所以这里是**一级撤销**，撤销完就把它清掉，不做重做。
    """
    if previous is None or same_config(current, previous):
        return None
    return previous


def _crossing_times(config: LapConfig) -> set[float]:
    """The moments of every hand-entered crossing, at sidecar precision."""
    return {
        round(float(b.time), 4)
        for b in config.beacons
        if not b.has_position and b.time is not None
    }


def _merge_crossings(edges: Iterable[float], times: Iterable[float]) -> list[float]:
    """Add hand-entered crossings to the boundaries that are already there.

    A crossing that lands within :data:`CROSSING_SNAP` of an existing boundary
    *is* that boundary: pointing at i2 Pro's own detected crossing and clicking
    would otherwise leave a hair-thin phantom lap (the times sidecars carry are
    rounded, so the two never match bit for bit).
    """
    merged = sorted(float(edge) for edge in edges)
    for when in times:
        if all(abs(float(when) - edge) > CROSSING_SNAP for edge in merged):
            merged.append(float(when))
    return sorted(merged)


def time_at_distance(log: ldmod.LogFile, distance: float) -> float | None:
    """The moment the car was ``distance`` metres into the session.

    The distance axis puts the cursor in metres, but a crossing is a *time*: this
    is the conversion. It walks the cumulative distance series - which is monotone
    by construction - instead of the plotted, bucket-downsampled trace, so the
    answer is good to the sample rate and does not get coarser when the whole
    session is on screen (a 900-bucket overview is ~2.7 s per bucket on a
    40-minute endurance run).

    A parked car holds its distance: the series has flat spots of arbitrary
    length, so "the time at distance D" is ambiguous there. The answer is the
    **first** moment the car reached D - when the crossing happened - not the
    moment it finally moved on, which can be a minute of waiting later.

    Returns ``None`` for a session with no usable distance axis, and for a value
    outside the range the car actually drove.
    """
    try:
        series = np.asarray(distance_on_master(log), dtype=np.float64)
    except ValueError:
        return None
    time = np.asarray(timebase.axis(log), dtype=np.float64)
    count = min(series.size, time.size)
    series, time = series[:count], time[:count]
    if count < 2 or not math.isfinite(distance):
        return None
    if distance < series[0] or distance > series[-1]:
        return None
    series = np.maximum.accumulate(series)          # flat spots and jitter are fine
    if series[-1] <= series[0]:
        return None
    index = int(np.searchsorted(series, float(distance), side="left"))
    return float(time[min(index, count - 1)])


def clean_name(name: object, fallback: str) -> str:
    """Trim the name; empty means "keep whatever it was called before"."""
    return str(name or "").strip() or fallback


def unique_name(stem: str, taken: Iterable[str], limit: int = MAX_BEACON_NAME) -> str:
    """Truncate to ``limit`` characters and add " 2", " 3"… until it is free."""
    used = set(taken)
    name = stem[:limit]
    if name not in used:
        return name
    for number in range(2, 1000):
        suffix = f" {number}"
        candidate = stem[: max(1, limit - len(suffix))] + suffix
        if candidate not in used:
            return candidate
    return name


def _migrate_trusted(trusted: dict[str, bool], old: str, new: str) -> dict[str, bool]:
    """Move ``<old> <n>`` trusted marks onto ``<new> <n>``.

    The remainder after the prefix must be a plain lap number: a beacon that
    legitimately ends in a digit (``左环`` renamed to ``左环 2``) leaves labels
    like ``左环 2 1``, which a rename of ``左环`` must not re-point at itself.
    """
    if old == new:
        return trusted
    prefix = old + " "
    migrated: dict[str, bool] = {}
    for key, value in trusted.items():
        rest = key[len(prefix) :] if key.startswith(prefix) else None
        migrated[f"{new} {rest}" if rest is not None and rest.isdigit() else key] = value
    return migrated


def config_path(ld_path: str | Path) -> Path:
    """``<session>.ld`` -> ``<session>.laps.json``."""
    return Path(ld_path).with_suffix(CONFIG_SUFFIX)


def load_config(ld_path: str | Path) -> LapConfig:
    """Never raises: an unreadable sidecar just means "no manual edits yet"."""
    path = config_path(ld_path)
    if not path.exists():
        return LapConfig()
    try:
        return LapConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return LapConfig()


def save_config(ld_path: str | Path, config: LapConfig) -> Path:
    path = config_path(ld_path)
    path.write_text(
        json.dumps(config.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def detect_from_config(
    log: ldmod.LogFile,
    config: LapConfig | None = None,
    fix: "gpsfix.FixConfig | None" = None,
) -> list[Lap]:
    """Run detection using whatever the user configured for this session."""
    config = config if config is not None else load_config(log.path)
    if config.beacons:
        return _laps_for_beacons(log, config, fix=fix)
    mode = config.mode
    if mode not in ("auto", "run", "figure8"):
        mode = "auto"                     # unknown/legacy value -> automatic
    laps = detect_laps(log, method=mode, fix=fix)
    for lap in laps:
        if lap.label in config.trusted:
            lap.complete = config.trusted[lap.label]
    return laps


def _laps_for_beacons(
    log: ldmod.LogFile,
    config: LapConfig,
    fix: "gpsfix.FixConfig | None" = None,
) -> list[Lap]:
    """One independent lap series per beacon.

    A figure-of-eight gets one beacon per loop, so each loop is timed on its own
    and there is nothing to guess. A beacon that carries only a time is a
    hand-inserted crossing (i2 Pro's "Missed Beacons"): it is inserted into the
    series it is closest to in time, or - when no placed beacon produced a series
    - into the boundaries the session already has.
    """
    distance = distance_on_master(log, fix=fix)
    placed = [b for b in config.beacons if b.has_position]
    timed = sorted(b.time for b in config.beacons if not b.has_position and b.time is not None)

    series = []
    for beacon in placed:
        bounds = [
            b
            for b in gps_laps(log, gate=(beacon.lat, beacon.lon), fix=fix)
            if b[1] - b[0] >= 4.0
        ]
        if not bounds:
            continue
        series.append({
            "beacon": beacon,
            "edges": [b[0] for b in bounds],
            "end": bounds[-1][1],
        })

    if not series:
        laps = _laps_with_inserted_crossings(log, config, distance, fix=fix)
    else:
        for when in timed:                  # merge each missed crossing by time
            nearest = min(series, key=lambda s: min(abs(when - e) for e in s["edges"]))
            nearest["edges"] = _merge_crossings(nearest["edges"], [when])
        laps = []
        for entry in series:
            edges = sorted(entry["edges"] + [entry["end"]])
            bounds = [
                (start, end, f"{entry['beacon'].name} {n}", n not in (1, len(edges) - 1))
                for n, (start, end) in enumerate(zip(edges, edges[1:]), start=1)
                if end > start
            ]
            laps.extend(_lap_from_bounds(log, distance, bounds))
        laps.sort(key=lambda lap: lap.start_time)

    for index, lap in enumerate(laps):
        lap.index = index
        if lap.label in config.trusted:
            lap.complete = config.trusted[lap.label]
    return laps


def _laps_with_inserted_crossings(
    log: ldmod.LogFile,
    config: LapConfig,
    distance: np.ndarray,
    fix: "gpsfix.FixConfig | None" = None,
) -> list[Lap]:
    """Insert hand-entered crossings into the boundaries the session already has.

    A time-only beacon is a *missed* crossing: it adds one boundary. It must
    never replace the lap set, so the boundaries come from whatever cutting
    method the config asks for and the entered times are merged into them. Only
    when there is nothing at all to insert into do the times stand alone.
    """
    times = sorted(
        b.time for b in config.beacons if not b.has_position and b.time is not None
    )
    mode = config.mode if config.mode in ("auto", "run", "figure8") else "auto"
    try:
        base = detect_laps(log, method=mode, fix=fix)
    except ValueError:
        base = []
    if base:
        edges = _merge_crossings([lap.start_time for lap in base] + [base[-1].end_time], times)
    elif len(times) >= 2:
        edges = times
    else:
        return []
    bounds = [
        (start, end, str(n), n not in (1, len(edges) - 1))
        for n, (start, end) in enumerate(zip(edges, edges[1:]), start=1)
    ]
    return _flag_implausible(_lap_from_bounds(log, distance, bounds))


def distance_on_master(
    log: ldmod.LogFile, fix: "gpsfix.FixConfig | None" = None
) -> np.ndarray:
    """Cumulative distance on the master time base, with the GPS fallback."""
    try:
        return derive.distance_series(log, fix=fix)
    except ValueError:
        track = derive.gps_track(log, fix=gpsfix.resolve(log, fix, "distance"), scope="distance")
        time = timebase.axis(log)
        speed = np.hypot(np.gradient(track["x"]), np.gradient(track["y"])) * track["rate"]
        return np.interp(time, track["time"], np.cumsum(speed) / track["rate"])
