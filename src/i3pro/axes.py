"""距离轴与圈差：这一场跑了多远、两圈差多少（ticket #24）。

从 ``laps.py`` 分出来的第三件事。共同点是它们**都不切圈**，只回答两个问题：

* ``distance_on_master`` / ``time_at_distance``：距离轴与时间轴怎么互相换算。
  车停着不动时同一个距离会持续几分钟，答案是**首次到达**那个时刻；
* ``overlay`` / ``time_delta``：i2 Pro 的看家功能——两圈放到同一条距离轴上比，
  再算累计时间差。

切圈在 :mod:`i3pro.laps`，信标与配置在 :mod:`i3pro.beacons`。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from . import derive, gpsfix, timebase
from . import ld as ldmod

if TYPE_CHECKING:  # 只是标注：真 import 会绕回 laps -> axes 的环
    from .laps import Lap

__all__ = [
    "distance_on_master",
    "overlay",
    "time_at_distance",
    "time_delta",
]


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
