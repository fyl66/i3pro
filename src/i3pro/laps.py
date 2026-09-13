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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from . import derive
from . import ld as ldmod

__all__ = [
    "Lap",
    "LapConfig",
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
    gate: tuple[float, float] | None = None,
) -> list[tuple[float, float, str, bool]]:
    """Start/finish gate crossing detection on the GPS trajectory.

    ``gate`` is an explicit (lat, lon) start/finish point - the manual beacon the
    user drops on the track map. Without it the gate is chosen automatically.
    """
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
    time = _master_time(log)
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
    **kwargs,
) -> list[Lap]:
    """Split a log into laps and attach cumulative distance to each one.

    ``gate`` is an explicit start/finish point as (lat, lon) - it replaces the
    automatically chosen one. ``beacons`` is an explicit list of crossing times
    in seconds, which wins over everything else.
    """
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
    time = _master_time(log)
    if beacons:
        edges = sorted(float(b) for b in beacons)
        bounds = [
            (start, end, str(n), True)
            for n, (start, end) in enumerate(zip(edges, edges[1:]), start=1)
            if end > start
        ]
    elif gate is not None:
        bounds = gps_laps(log, gate=gate, **kwargs)
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
                bounds = gps_laps(log, **kwargs)
    elif method == "gps":
        bounds = gps_laps(log, **kwargs)
    elif method == "winding":
        bounds = winding_laps(log, **kwargs)
    elif method == "run":
        bounds = run_laps(log, **kwargs)
    elif method == "figure8":
        bounds = figure8_laps(log, **kwargs)
    else:
        raise ValueError(f"unknown lap detection method: {method!r}")
    min_time = kwargs.get("min_lap_time", 8.0)
    if method != "run":
        bounds = [b for b in bounds if b[1] - b[0] >= min_time]
    laps = _lap_from_bounds(log, distance, bounds)
    if method == "figure8":
        try:
            track = derive.gps_track(log)
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


# --------------------------------------------------------------- sidecar
@dataclass
class LapConfig:
    """Everything the user has decided about this session's laps.

    Stored as ``<session>.laps.json`` next to the log. Keeping it out of the
    ``.ld``/``.ldx`` preserves the "logs are read-only" rule and lets the file
    travel with the data folder, be diffed in git, and ride along a share link.
    """

    mode: str = "auto"                       # auto | run | figure8 | beacons
    gate: tuple[float, float] | None = None  # manual start/finish as (lat, lon)
    #: Named beacons. One gate = one lap series, so a figure-of-eight gets two
    #: (left loop + right loop) and each is timed independently - which is the
    #: only reliable way to tell the two loops apart on a self-crossing course.
    gates: list[tuple[str, float, float]] = field(default_factory=list)
    beacons: list[float] = field(default_factory=list)   # explicit crossing times
    trusted: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "gate": list(self.gate) if self.gate else None,
            "gates": [{"name": n, "lat": lat, "lon": lon} for n, lat, lon in self.gates],
            "beacons": [round(float(b), 4) for b in self.beacons],
            "trusted": dict(self.trusted),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LapConfig":
        gate = data.get("gate")
        gates = [
            (str(g.get("name") or f"信标{i + 1}"), float(g["lat"]), float(g["lon"]))
            for i, g in enumerate(data.get("gates") or [])
            if g.get("lat") is not None and g.get("lon") is not None
        ]
        return cls(
            mode=str(data.get("mode") or "auto"),
            gate=(float(gate[0]), float(gate[1])) if gate else None,
            gates=gates,
            beacons=[float(b) for b in (data.get("beacons") or [])],
            trusted={str(k): bool(v) for k, v in (data.get("trusted") or {}).items()},
        )


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


def detect_from_config(log: ldmod.LogFile, config: LapConfig | None = None) -> list[Lap]:
    """Run detection using whatever the user configured for this session."""
    config = config if config is not None else load_config(log.path)
    if config.gates:
        return _laps_for_gates(log, config)
    mode = config.mode
    if mode == "beacons" and not config.beacons:
        mode = "auto"                     # nothing hand-placed yet
    laps = detect_laps(
        log,
        method=mode,
        gate=config.gate,
        beacons=config.beacons or None,
    )
    for lap in laps:
        if lap.label in config.trusted:
            lap.complete = config.trusted[lap.label]
    return laps


def _laps_for_gates(log: ldmod.LogFile, config: LapConfig) -> list[Lap]:
    """One independent lap series per named beacon.

    This is how a figure-of-eight gets split per loop: put one beacon on each
    loop and each loop is timed on its own, so there is nothing to guess. It is
    also i2 Pro's model - laps are created between beacon crossings, and a
    session may have several beacons (sector splits).
    """
    distance = _distance_series(log)
    laps: list[Lap] = []
    for name, lat, lon in config.gates:
        bounds = gps_laps(log, gate=(lat, lon))
        bounds = [b for b in bounds if b[1] - b[0] >= 4.0]
        labelled = [
            (start, end, f"{name} {n}", complete)
            for n, (start, end, _label, complete) in enumerate(bounds, start=1)
        ]
        laps.extend(_lap_from_bounds(log, distance, labelled))
    laps.sort(key=lambda lap: lap.start_time)
    for index, lap in enumerate(laps):
        lap.index = index
        if lap.label in config.trusted:
            lap.complete = config.trusted[lap.label]
    return laps


def _distance_series(log: ldmod.LogFile) -> np.ndarray:
    """Cumulative distance on the master time base, with the GPS fallback."""
    try:
        return derive.distance_series(log)
    except ValueError:
        track = derive.gps_track(log)
        time = _master_time(log)
        speed = np.hypot(np.gradient(track["x"]), np.gradient(track["y"])) * track["rate"]
        return np.interp(time, track["time"], np.cumsum(speed) / track["rate"])
