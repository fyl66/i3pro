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
| 切圈算法（本模块） | ``gps_laps`` / ``winding_laps`` / ``figure8_laps`` / ``run_laps``（``detect_laps`` 是它们的**算法**入口） |
| "这一场有哪些圈"（本模块） | ``detect_from_config``——**看侧车**（用户选的信标 / 模式 / 可信圈）；``lap_table`` 不给 laps 时问的就是它 |
| 信标与配置、编辑规则、侧车 | :mod:`i3pro.beacons` |
| 距离轴换算、两圈叠加与圈差 | :mod:`i3pro.axes` |

**两个入口别搞混（A81）**：``detect_laps`` 只回答"按这个算法切出来是什么"，
``detect_from_config`` 回答"这一场有哪些圈"。界面、命令行、报表、导出要的都是后者；
2026-10-14 之前有四条命令行走的是前者，于是同一个 `.ld` 在侧边栏写着 20 圈、
``i3pro track`` 打印 16 行且整列都标着"(进出场/泊车/异常段)"。源码里有一条守卫
钉住"``src/i3pro`` 除了本模块不许再出现 ``detect_laps(``"。

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
from .lapgates import (
    _angle_diff,
    _best_alternative_gate,
    _best_figure8_gate,
    _bounds_from_crossings,
    _crossings_for_gate,
    _cut_quality,
    _distance_on_gps,
    _gate_candidates,
    _heading,
    classify_track,
    turn_direction,
)
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
    "classify_track",
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
        candidates: list[tuple[int, float, float, int]] = []
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
    crossings = [c for c in crossings if c >= start]

    # The point-circle gate is retained as the safe baseline. It is replaced
    # only when its cut is weak and a heading-aligned line gate produces
    # materially more complete, mutually consistent laps.
    if gate is None:
        baseline_bounds = _bounds_from_crossings(start, end, crossings)
        baseline_quality = _cut_quality(
            baseline_bounds,
            t,
            _distance_on_gps(t, speed),
            min_lap_time,
        )
        should_try_alternative = (
            baseline_quality.complete < 2
            or (
                baseline_quality.time_cv + baseline_quality.distance_cv > 0.65
            )
        )
        if should_try_alternative:
            alternative = _best_alternative_gate(
                t,
                x,
                y,
                _distance_on_gps(t, speed),
                segments,
                candidates,
                first_moving,
                span,
                heading_tolerance,
                min_lap_time,
                half_width=max(6.0, min(12.0, 0.08 * diagonal)),
            )
            if alternative is not None:
                alternative_crossings, alternative_quality = alternative
                if alternative_quality.better_than(baseline_quality):
                    crossings = alternative_crossings

    # A brief twitch before the sustained driving phase can register as a
    # crossing; the lap clock starts when the car really gets going.
    return _bounds_from_crossings(start, end, crossings)


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


def _track_speed(log: ldmod.LogFile, track: dict) -> np.ndarray:
    try:
        return np.interp(track["time"], timebase.axis(log), derive.speed_series(log))
    except ValueError:
        # GPS-derived speed is enough for classification and gate selection.
        t = np.asarray(track["time"], dtype=float)
        x = np.asarray(track["x"], dtype=float)
        y = np.asarray(track["y"], dtype=float)
        dt = np.gradient(t)
        dt[dt <= 0] = 1.0 / max(float(track.get("rate", 1.0)), 1.0)
        raw = np.hypot(np.gradient(x) / dt, np.gradient(y) / dt) * 3.6
        window = max(1, int(float(track.get("rate", 1.0)) * 2.0))
        return np.convolve(raw, np.ones(window) / window, mode="same")


def figure8_laps(log: ldmod.LogFile, **kwargs) -> list[tuple[float, float, str, bool]]:
    """Split a figure-of-eight into one segment per loop (left / right).

    On a figure-of-eight the trajectory crosses itself, so a gate placed on a
    loop is met twice per cycle - the plain GPS detector already returns
    roughly one segment per loop on the team's real 八字 log. What it cannot do
    is say which loop a segment is, so ``detect_laps`` labels each one by its
    turn direction.
    """
    min_lap_time = float(kwargs.get("min_lap_time", 8.0))
    fix = kwargs.get("fix")
    track = derive.gps_track(log, fix=gpsfix.resolve(log, fix, "laps"), scope="laps")
    t = np.asarray(track["time"], dtype=float)
    x = np.asarray(track["x"], dtype=float)
    y = np.asarray(track["y"], dtype=float)
    speed = _track_speed(log, track)
    moving = speed > float(kwargs.get("speed_threshold", 8.0))
    hold = max(1, int(round(float(track["rate"]) * 3.0)))
    sustained = np.convolve(moving.astype(int), np.ones(hold, dtype=int), mode="valid") >= hold
    if not sustained.any():
        return gps_laps(log, **kwargs)
    first_moving = int(np.argmax(sustained))
    span = max(1, int(round(float(track["rate"]))))
    gaps = np.flatnonzero(np.diff(t) > 1.5) + 1
    if "breaks" in track:
        gaps = np.union1d(gaps, np.flatnonzero(track["breaks"]))
    segments = [part for part in np.split(np.arange(t.size), gaps) if part.size > 2]
    candidates = _gate_candidates(x, y, speed, float(kwargs.get("speed_threshold", 8.0)))
    found = _best_figure8_gate(
        t, x, y, segments, candidates, first_moving, span, min_lap_time
    )
    return found if found is not None else gps_laps(log, **kwargs)


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
    auto_pattern: str | None = None
    selected_method = method
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
                try:
                    track = derive.gps_track(log, fix=laps_fix, scope="laps")
                    speed = _track_speed(log, track)
                    auto_pattern = classify_track(track["x"], track["y"], speed)
                except ValueError:
                    # No usable GPS is not a reason to fail when the speed
                    # channel can still separate runs.
                    auto_pattern = "runs"
                if auto_pattern == "figure8":
                    selected_method = "figure8"
                    bounds = figure8_laps(log, fix=fix, **kwargs)
                elif auto_pattern == "runs":
                    selected_method = "run"
                    bounds = run_laps(log, **kwargs)
                else:
                    selected_method = "gps"
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
    if selected_method != "run":
        bounds = [b for b in bounds if b[1] - b[0] >= min_time]
    laps = _lap_from_bounds(log, distance, bounds)
    # If a single shape was mistaken for a closed circuit, a healthy GPS pass
    # often produces one all-session "lap". More than one sustained run is a
    # useful analysis unit and is what the driver actually did.
    if auto_pattern == "circuit" and len(laps) <= 2 and not any(l.complete for l in laps):
        runs = run_laps(log, **kwargs)
        if len(runs) >= 2:
            selected_method = "run"
            laps = _lap_from_bounds(log, distance, runs)
    if selected_method == "figure8":
        try:
            track = derive.gps_track(log, fix=laps_fix, scope="laps")
        except ValueError:
            track = None
        if track is not None:
            for lap in laps:
                inside = ((track["time"] >= lap.start_time) & (track["time"] <= lap.end_time))
                turn = turn_direction(track["x"][inside], track["y"][inside])
                lap.turn = {1: "left", -1: "right"}.get(turn)
    # Runs are already delimited by sustained standstill; applying lap-time and
    # lap-distance medians to them would silently mark a valid three-second
    # radiator test as incomplete.
    if selected_method == "run":
        return laps
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
    # 中位数要**跳过算不出来的那些圈**。一条圈的圈距是 NaN（距离通道里有个空样本）
    # 时，`np.median` 给出 NaN，下面两条判据于是对**每一条**圈都为假真——整张表被
    # 整片标成不完整。实测 10.25 两段耐久接起来之后就是这样：50 段、0 个完整圈。
    # 现在只有算不出来的那一条被标掉，其余照旧。
    median_time = float(np.nanmedian(times)) if np.isfinite(times).any() else 0.0
    median_len = float(np.nanmedian(lengths)) if np.isfinite(lengths).any() else 0.0
    for lap in laps:
        if not lap.complete:
            continue
        if not np.isfinite(lap.lap_time) or not np.isfinite(lap.distance):
            lap.complete = False
            continue
        bad_time = not (0.4 * median_time <= lap.lap_time <= 2.5 * median_time)
        bad_length = not (0.35 * median_len <= lap.distance <= 1.6 * median_len)
        if bad_time or bad_length or lap.distance < 50.0:
            lap.complete = False
    return laps


def lap_table(log: ldmod.LogFile, laps: list[Lap] | None = None) -> list[dict]:
    """圈表。不给 ``laps`` 时问的就是"这一场有哪些圈"——**不看侧车的那条路不算答案**
    （2026-10-14 之前它默认走 `detect_laps`，于是同一个 `.ld` 在侧边栏与命令行给出
    两个圈数，见 A81）。
    """
    laps = laps if laps is not None else detect_from_config(log)
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
