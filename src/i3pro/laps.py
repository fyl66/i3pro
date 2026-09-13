"""切圈：把一场数据切成一段段"圈"（ticket #24）。

The team's C125 logs are recorded with the beacon input unwired: ``Beacon``,
``Lap Number`` and ``Distance`` are dead (constant) in every file, so i2 Pro
itself reports "Total Laps = 1". i3pro therefore detects laps from the GPS
trajectory:

* ``gps``     - start/finish gate crossings with heading + direction filtering
                (default, works for any closed course),
* ``winding`` - total angle swept around the track centroid (robust for oval /
                convex courses),
* ``beacon``  - the logger's own lap/beacon channels, used when they are live.

``laps.py`` 原来一个人背四件事，2026-09-14 按 ticket #24 拆开，现在住这样：

| 概念 | 住在哪 |
| --- | --- |
| 切圈算法（本模块） | ``detect_laps`` / ``gps_laps`` / ``winding_laps`` / ``figure8_laps`` / ``run_laps`` / ``lap_table`` |
| 信标与配置、编辑规则、侧车 | :mod:`i3pro.beacons` |
| 距离轴换算、两圈叠加与圈差 | :mod:`i3pro.axes` |

下面仍把 ``Beacon`` / ``LapConfig`` / ``overlay`` 这些名字**转出来**（``__all__`` 里
分了两段），因为界面、命令行、报表早就按 ``laps.X`` 在用了；转出来的就是那两个模块
里的同一个对象，不是第二份实现——``tests`` 里有一条守卫按源码钉住这一点。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np

from . import derive
from . import gpsfix
from . import ld as ldmod
from . import timebase
from .axes import distance_on_master, overlay, time_at_distance, time_delta
from .beacons import (
    Beacon,
    CONFIG_SUFFIX,
    CROSSING_SNAP,
    DEFAULT_BEACON_NAME,
    DEFAULT_CROSSING_NAME,
    LapConfig,
    MAX_BEACON_NAME,
    merge_crossings,
    check_new_crossings,
    clean_name,
    config_path,
    insertion_notice,
    load_config,
    reconcile_edits,
    same_config,
    save_config,
    undo_config,
    unique_name,
)

__all__ = [
    # 本模块自己实现的：切圈
    "Lap",
    "detect_laps",
    "detect_from_config",
    "figure8_laps",
    "gps_laps",
    "lap_table",
    "run_laps",
    "turn_direction",
    # 转出来的：信标与配置（见 i3pro.beacons）
    "Beacon",
    "LapConfig",
    "check_new_crossings",
    "clean_name",
    "config_path",
    "insertion_notice",
    "load_config",
    "reconcile_edits",
    "same_config",
    "save_config",
    "undo_config",
    "unique_name",
    # 转出来的：距离轴与圈差（见 i3pro.axes）
    "distance_on_master",
    "overlay",
    "time_at_distance",
    "time_delta",
]


#: 信标 / 配置 / 名字规则的常量都住在 ``beacons.py``（上面 import 进来了），
#: 这里只留**切圈算法自己**用的两串通道名。
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
            nearest["edges"] = merge_crossings(nearest["edges"], [when])
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
        edges = merge_crossings([lap.start_time for lap in base] + [base[-1].end_time], times)
    elif len(times) >= 2:
        edges = times
    else:
        return []
    bounds = [
        (start, end, str(n), n not in (1, len(edges) - 1))
        for n, (start, end) in enumerate(zip(edges, edges[1:]), start=1)
    ]
    return _flag_implausible(_lap_from_bounds(log, distance, bounds))
