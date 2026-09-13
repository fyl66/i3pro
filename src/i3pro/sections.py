"""Track sections: split one lap into corners and straights, by distance.

A **section** is an interval of the track *measured in metres along the lap* —
it is neither a lap (:mod:`i3pro.laps`) nor a loop. The split does not depend on
time, so the same section definition can be laid over every lap of the session
and be shown, compared and reported per section.

Two bases, both measured on the session's own lap:

* ``curvature`` — turning per metre travelled, computed from the GPS trajectory
  (``d(heading)/d(distance)``). Speed independent, which is what "is this a
  corner" means.
* ``lateral_g`` — ``|G Force Lat|``. What the tyres feel; a slow hairpin shows
  up weaker here than a fast sweeper.

The session channels *named* ``Curvature`` / ``Radius`` are deliberately **not**
used: in both golden sessions ``Curvature`` is zero for the whole outing and
``Radius`` is zero in 耐久正赛 (see ``docs/ACCEPTANCE.md`` A30). A basis that is
silently a flat line would split the lap into one straight and look like it
worked.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from . import derive, laps as lapsmod

__all__ = [
    "BASIS_LABELS",
    "KIND_LABELS",
    "DEFAULT_SENSITIVITY",
    "DEFAULT_MIN_SECTION_M",
    "SectionConfig",
    "available_bases",
    "auto_config",
    "auto_for_log",
    "auto_names",
    "config_path",
    "dedupe_names",
    "effective_config",
    "bands",
    "lap_distance",
    "lap_frame",
    "lap_marks",
    "load_config",
    "measure_series",
    "measure_unit",
    "normalize",
    "reference_lap",
    "same_layout",
    "save_config",
    "summarize",
]

#: 判据的可读名字（界面上的下拉框直接用这一份）。
BASIS_LABELS = {"curvature": "曲率", "lateral_g": "横向加速度"}
KIND_LABELS = {"corner": "弯", "straight": "直"}

DEFAULT_SENSITIVITY = 1.0
DEFAULT_MIN_SECTION_M = 25.0
#: 自动切分在 1 m 的均匀距离网格上做——按时间采样的话，慢弯里样本挤成一团，
#: 阈值判出来的"边界"会全挤在同一个地方。
GRID_STEP_M = 1.0
#: 平滑窗口（米）。GPS 算出来的曲率逐点噪声很大，不平滑会切出一堆 2 米的假弯。
SMOOTH_M = 8.0
#: 阈值 = 10 分位 + (90 分位 − 10 分位) × CORNER_FRACTION ÷ 灵敏度。
#: 用分位数而不是绝对值：不同赛道、不同车的曲率量级差很多，而分位数是自适应的。
CORNER_FRACTION = 0.35
CORNER_PERCENTILE = 90.0
STRAIGHT_PERCENTILE = 10.0

SIDE_SUFFIX = ".sections.json"


@dataclass(frozen=True)
class SectionConfig:
    """This session's section split.

    ``boundaries`` are distances in metres **along the reference lap** (ascending,
    first 0, last the lap length); ``kinds`` / ``names`` have one entry per span
    between two boundaries, so ``len(kinds) == len(boundaries) - 1``.

    ``edited`` is the flag ticket #7 asks for: once a human has moved a boundary
    or renamed a span, the automatic split must not quietly overwrite it.
    """

    basis: str = "lateral_g"
    sensitivity: float = DEFAULT_SENSITIVITY
    min_length_m: float = DEFAULT_MIN_SECTION_M
    boundaries: tuple[float, ...] = ()
    kinds: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    edited: bool = False
    #: Which lap this split was made on, and how long it was. When the fastest
    #: lap changes (a beacon moved), the stored boundaries no longer sit on the
    #: same distances, and the user is told rather than silently re-mapped.
    reference_label: str = ""
    length_m: float = 0.0

    @property
    def spans(self) -> list[dict]:
        out = []
        for index, kind in enumerate(self.kinds):
            start = float(self.boundaries[index])
            end = float(self.boundaries[index + 1])
            out.append(
                {
                    "index": index,
                    "kind": kind,
                    "kind_label": KIND_LABELS.get(kind, kind),
                    "name": self.names[index] if index < len(self.names) else "",
                    "start_distance": round(start, 1),
                    "end_distance": round(end, 1),
                    "length_m": round(end - start, 1),
                }
            )
        return out

    def as_dict(self) -> dict:
        return {
            "basis": self.basis,
            "sensitivity": self.sensitivity,
            "min_length_m": self.min_length_m,
            "boundaries": [round(float(value), 1) for value in self.boundaries],
            "kinds": list(self.kinds),
            "names": list(self.names),
            "edited": bool(self.edited),
            "reference_lap": self.reference_label,
            "length_m": round(float(self.length_m), 1),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SectionConfig":
        """Read a sidecar. Unknown bases/kinds are refused, not silently kept."""
        raw_basis = str(data.get("basis") or "lateral_g")
        if raw_basis not in BASIS_LABELS:
            raise ValueError(
                f"判据只认 {sorted(BASIS_LABELS)} 之一，读到的是 {raw_basis!r}；"
                f"改掉 <场次>{SIDE_SUFFIX} 里的 basis，或者删掉这个文件重来。"
            )
        boundaries = _float_tuple(data.get("boundaries"))
        raw_kinds = data.get("kinds") or []
        kinds = tuple(str(item) for item in raw_kinds)
        bad = sorted({item for item in kinds if item not in KIND_LABELS})
        if bad:
            raise ValueError(
                f"区段种类只认 corner / straight，读到的是 {bad}；"
                f"改掉 <场次>{SIDE_SUFFIX} 里的 kinds，或者删掉这个文件重来。"
            )
        names = tuple(str(item) for item in (data.get("names") or []))
        return cls(
            basis=raw_basis,
            sensitivity=float(data.get("sensitivity") or DEFAULT_SENSITIVITY),
            min_length_m=float(data.get("min_length_m") or DEFAULT_MIN_SECTION_M),
            boundaries=boundaries,
            kinds=kinds,
            names=names,
            edited=bool(data.get("edited")),
            reference_label=str(data.get("reference_lap") or ""),
            length_m=float(data.get("length_m") or 0.0),
        )


def _float_tuple(raw) -> tuple[float, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError("boundaries 应该是一个距离（米）的列表。")
    return tuple(float(item) for item in raw)


# ------------------------------------------------------------------ 测度


def available_bases(log) -> list[str]:
    """这个场次能按什么切。曲率要 GPS 轨迹，横向加速度要 ``G Force Lat``。"""
    bases: list[str] = []
    try:
        derive.gps_track(log)
    except ValueError:
        pass
    else:
        bases.append("curvature")
    if log.has("G Force Lat"):
        bases.append("lateral_g")
    return bases


def measure_unit(basis: str) -> str:
    return "1/m" if basis == "curvature" else "G"


def measure_series(log, basis: str) -> np.ndarray:
    """主时间基上的测度（非负）。切分的唯一输入，纯列函数。"""
    rate = float(log.sample_rate) or 1.0
    size = int(round(log.duration * rate)) + 1
    if basis == "curvature":
        return _gps_curvature(log, size, rate)
    if basis == "lateral_g":
        if not log.has("G Force Lat"):
            raise ValueError(
                f"{log.path.name}: 没有 `G Force Lat` 通道，不能按横向加速度切；"
                f"换成曲率（要有 GPS 轨迹）或者把这个判据存进侧车换掉。"
            )
        return np.abs(derive.hold_to_master(log, "G Force Lat")[:size].astype(np.float64))
    raise ValueError(f"判据只认 {sorted(BASIS_LABELS)} 之一，收到的是 {basis!r}")


def _gps_curvature(log, size: int, rate: float) -> np.ndarray:
    """``|d(heading)/d(distance)|``（1/m），插值到主时间基上。

    GPS 是 10 Hz 左右的点；车几乎不动时相邻点的位移只有几厘米，方位角的抖动
    除以那个小位移会炸出天文数字的曲率，所以位移太小的点直接记为 0（直行），
    后面在距离网格上还会再平滑一次。
    """
    track = derive.gps_track(log)
    x = np.asarray(track["x"], dtype=np.float64)
    y = np.asarray(track["y"], dtype=np.float64)
    time = np.asarray(track["time"], dtype=np.float64)
    if x.size < 4:
        raise ValueError(f"{log.path.name}: GPS 轨迹点太少（{x.size} 个），算不出曲率。")
    dx = np.gradient(x)
    dy = np.gradient(y)
    step = np.hypot(dx, dy)
    heading = np.unwrap(np.arctan2(dy, dx))
    turn = np.gradient(heading)
    kappa = np.where(step > 0.05, turn / np.maximum(step, 1e-6), 0.0)
    values = np.interp(
        np.arange(size, dtype=np.float64) / rate, time, kappa, left=kappa[0], right=kappa[-1]
    )
    return np.abs(values)


# ------------------------------------------------------------------ 参考圈


def reference_lap(laps) -> object | None:
    """区段切在哪条圈上：最快的**完整**圈；没有完整圈就用最长的那条。"""
    complete = [lap for lap in laps if lap.complete]
    if complete:
        return min(complete, key=lambda lap: lap.lap_time)
    if not laps:
        return None
    return max(laps, key=lambda lap: lap.distance)


def lap_distance(log, lap) -> np.ndarray:
    """一条圈自己的累计距离（从 0 起算），与 ``laps`` 算圈长用的是同一条序列。"""
    distance = np.asarray(lapsmod.distance_on_master(log), dtype=np.float64)
    rate = float(log.sample_rate) or 1.0
    i0 = max(0, int(round(lap.start_time * rate)))
    i1 = min(distance.size - 1, int(round(lap.end_time * rate)))
    if i1 <= i0:
        raise ValueError(f"第 {lap.label} 圈的时间跨度里没有样本，切不了区段。")
    return distance[i0 : i1 + 1] - float(distance[i0])


def lap_frame(log, lap, basis: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """一条圈上的 ``(时刻, 距离, 测度)``，距离从 0 起算。"""
    rate = float(log.sample_rate) or 1.0
    i0 = max(0, int(round(lap.start_time * rate)))
    i1 = min(int(round(lap.end_time * rate)), int(round(log.duration * rate)))
    distance = lap_distance(log, lap)
    measure = measure_series(log, basis)[i0 : i1 + 1]
    time = np.arange(i0, i1 + 1, dtype=np.float64) / rate
    size = min(distance.size, measure.size, time.size)
    return time[:size], distance[:size], measure[:size]


# ------------------------------------------------------------------ 自动切分


def _grid(distance: np.ndarray, measure: np.ndarray, step: float) -> tuple[np.ndarray, np.ndarray]:
    """把测度搬到均匀距离网格上；距离不是严格递增时取**首次到达**的那个样本。"""
    length = float(distance[-1])
    if not np.isfinite(length) or length <= 0:
        raise ValueError("这一圈的距离轴是空的（车没动过？），切不了区段。")
    count = max(2, int(round(length / max(step, 0.05))) + 1)
    grid = np.linspace(0.0, length, count)
    monotone = np.maximum.accumulate(np.asarray(distance, dtype=np.float64))
    index = np.searchsorted(monotone, grid, side="left")
    index = np.clip(index, 0, measure.size - 1)
    values = np.asarray(measure, dtype=np.float64)[index]
    bad = ~np.isfinite(values)
    if bad.any():
        values = np.where(bad, 0.0, values)
    return grid, values


def _smooth_on_grid(values: np.ndarray, grid: np.ndarray, window_m: float) -> np.ndarray:
    if values.size < 3 or window_m <= 0:
        return values
    step = float(grid[1] - grid[0])
    half = max(1, int(round(window_m / max(step, 1e-6) / 2)))
    kernel = np.ones(2 * half + 1) / (2 * half + 1)
    return np.convolve(values, kernel, mode="same")


def _spans_from_mask(mask: np.ndarray) -> list[tuple[str, int, int]]:
    """掩码 → 交替的 (种类, 起点下标, 终点下标) 列表，首尾相接覆盖整条。"""
    spans: list[tuple[str, int, int]] = []
    start = 0
    for index in range(1, mask.size + 1):
        if index == mask.size or mask[index] != mask[start]:
            kind = "corner" if mask[start] else "straight"
            spans.append((kind, start, index))
            start = index
    return spans


def _absorb_short(spans: list[tuple[str, int, int]], min_points: int) -> list[tuple[str, int, int]]:
    """把短于 ``min_points`` 的段并进邻居。

    段永远是交替的，所以一段的两侧邻居一定同类：删掉中间这一段、让两侧接上，
    就等于"这一段其实是邻居那种"。这就是迟滞，不需要另设两个阈值。
    """
    spans = list(spans)
    while len(spans) > 1:
        shortest = min(range(len(spans)), key=lambda i: spans[i][2] - spans[i][1])
        if spans[shortest][2] - spans[shortest][1] >= min_points:
            break
        first = spans.pop(shortest)
        if not spans:
            spans = [first]
            break
        if shortest == 0:
            head = spans[0]
            spans[0] = (head[0], first[1], head[2])
        elif shortest >= len(spans):
            tail = spans[-1]
            spans[-1] = (tail[0], tail[1], first[2])
        else:
            left = spans[shortest - 1]
            right = spans[shortest]
            spans[shortest - 1] = (left[0], left[1], right[2])
            spans.pop(shortest)
    return spans


def auto_config(
    distance: np.ndarray,
    measure: np.ndarray,
    basis: str = "lateral_g",
    sensitivity: float = DEFAULT_SENSITIVITY,
    min_length_m: float = DEFAULT_MIN_SECTION_M,
    reference_label: str = "",
    step: float = GRID_STEP_M,
    smooth_m: float = SMOOTH_M,
) -> SectionConfig:
    """按曲率／横向加速度切出一圈的弯道与直道。

    阈值取"本圈测度 10 分位 + (90 分位 − 10 分位) × 0.35 ÷ 灵敏度"：分位数让
    不同赛道自动量到自己的尺度，灵敏度是用户唯一要调的旋钮（越大越容易判成弯）。
    ``measure`` 全平（通道是坏的就长这样）时不给假弯，老实交回"整圈一条直道"。
    """
    if sensitivity <= 0:
        raise ValueError("灵敏度要大于 0（默认 1.0，越大越容易判成弯）。")
    if min_length_m <= 0:
        raise ValueError("最短段长要大于 0 米。")
    if basis not in BASIS_LABELS:
        raise ValueError(
            f"判据只认 {sorted(BASIS_LABELS)} 之一，收到的是 {basis!r}；"
            f"曲率要 GPS 轨迹，横向加速度要有 `G Force Lat` 通道。"
        )
    grid, values = _grid(distance, measure, step)
    length = float(grid[-1])
    if min_length_m >= length / 2:
        raise ValueError(
            f"最短段长 {min_length_m:g} m 比半圈（{length / 2:.0f} m）还长，切不出两段；"
            f"调到 {length / 4:.0f} m 左右再试。"
        )
    smooth = _smooth_on_grid(values, grid, smooth_m)
    low = float(np.percentile(smooth, STRAIGHT_PERCENTILE))
    high = float(np.percentile(smooth, CORNER_PERCENTILE))
    spread = high - low
    if not np.isfinite(spread) or spread <= 1e-9:
        # 测度整场一个值：没有"弯"可言，别硬切
        threshold = float("inf")
    else:
        threshold = low + spread * CORNER_FRACTION / sensitivity
    mask = smooth >= threshold
    min_points = max(1, int(round(min_length_m / max(grid[1] - grid[0], 1e-6))))
    spans = _absorb_short(_spans_from_mask(mask), min_points)
    boundaries = [float(grid[start]) for _kind, start, _end in spans]
    boundaries.append(length)
    kinds = [kind for kind, _start, _end in spans]
    return SectionConfig(
        basis=basis,
        sensitivity=float(sensitivity),
        min_length_m=float(min_length_m),
        boundaries=tuple(boundaries),
        kinds=tuple(kinds),
        names=auto_names(kinds),
        edited=False,
        reference_label=reference_label,
        length_m=length,
    )


def auto_names(kinds) -> tuple[str, ...]:
    """自动编号按**同类里的顺序**：``弯 1``、``直 1``、``弯 2``…"""
    counters = {"corner": 0, "straight": 0}
    out = []
    for kind in kinds:
        counters[kind] = counters.get(kind, 0) + 1
        out.append(f"{KIND_LABELS.get(kind, kind)} {counters[kind]}")
    return tuple(out)


# ------------------------------------------------------------------ 手工编辑


def normalize(config: SectionConfig, length_m: float) -> tuple[SectionConfig, str | None]:
    """手工改过之后把边界收拾干净：排序、去重、夹在 ``[0, 圈长]`` 里。

    边界必须首 0 尾圈长（否则会出现"没覆盖到的赛道"或者两段重叠——ticket #7
    的验收就盯着这两条）。名字/种类按段的个数补齐或截断，缺的按测度补一个
    中性的默认值，不猜用户想叫什么。
    """
    if length_m <= 0:
        raise ValueError("这一圈的距离轴是空的，改不了区段。")
    raw = sorted(float(value) for value in config.boundaries)
    cleaned: list[float] = []
    for value in raw:
        value = min(max(value, 0.0), length_m)
        if cleaned and value - cleaned[-1] < 1.0:
            continue
        cleaned.append(value)
    if not cleaned or cleaned[0] > 1.0:
        cleaned.insert(0, 0.0)
    else:
        cleaned[0] = 0.0
    if cleaned[-1] < length_m - 1.0:
        cleaned.append(length_m)
    else:
        cleaned[-1] = length_m
    if len(cleaned) < 2:
        raise ValueError("区段至少要两条边界：一条在 0，一条在本圈长度上。")
    spans = len(cleaned) - 1
    kinds = list(config.kinds[:spans])
    kinds += ["straight"] * (spans - len(kinds))
    names = list(config.names[:spans])
    defaults = auto_names(kinds)
    for index in range(len(names), spans):
        names.append(defaults[index])
    for index, name in enumerate(names):
        names[index] = str(name).strip() or defaults[index]
    notice = None
    if len(cleaned) != len(config.boundaries) or list(config.boundaries) != cleaned:
        notice = "边界已按本圈长度整理：去掉挤在一起的、补齐 0 与本圈长度。"
    edited = replace(
        config,
        boundaries=tuple(cleaned),
        kinds=tuple(kinds),
        names=tuple(names),
        length_m=float(length_m),
        edited=True,
    )
    return edited, notice


def same_layout(a: SectionConfig, b: SectionConfig) -> bool:
    """两份区段的**内容**是否一样（边界 / 种类 / 名字）。

    用来判断一次"手工保存"到底改没改东西：用户只是把同一份列表再发一次（比如
    改了又改回去），不该把 ``edited`` 立起来——那会让"重切会覆盖手工改动"的提醒
    对着一份根本没被手工改过的配置发火。
    """
    return (tuple(a.boundaries), tuple(a.kinds), tuple(a.names)) == (
        tuple(b.boundaries),
        tuple(b.kinds),
        tuple(b.names),
    )


def dedupe_names(config: SectionConfig) -> SectionConfig:
    """同名段按 ``弯 1 2`` 的规则错开——名字是报表的键，不能重。"""
    taken: set[str] = set()
    names: list[str] = []
    for index in range(len(config.kinds)):
        base = (config.names[index] if index < len(config.names) else "").strip()
        base = base or f"段 {index + 1}"
        name = base
        suffix = 2
        while name in taken:
            name = f"{base} {suffix}"
            suffix += 1
        taken.add(name)
        names.append(name)
    return replace(config, names=tuple(names))


# ------------------------------------------------------------------ 落到每条圈


def bands(log, lap, config: SectionConfig) -> list[dict]:
    """把区段套到某一条圈上，给出每段的距离与时刻。

    参考圈自己的边界就是距离；其它圈的赛道长度略有不同（走线不一样），所以按
    **同一比例**落到那条圈的距离轴上，取该距离**首次到达**的时刻——和
    ``laps.time_at_distance`` 一个口径。
    """
    distance = np.maximum.accumulate(lap_distance(log, lap))
    times = _boundary_times(log, lap, distance, config)
    rows = []
    for index, kind in enumerate(config.kinds):
        start_m = float(config.boundaries[index])
        end_m = float(config.boundaries[index + 1])
        start_time, end_time = times[index], times[index + 1]
        rows.append(
            {
                "index": index,
                "kind": kind,
                "kind_label": KIND_LABELS.get(kind, kind),
                "name": config.names[index] if index < len(config.names) else "",
                "start_distance": round(start_m, 1),
                "end_distance": round(end_m, 1),
                "length_m": round(end_m - start_m, 1),
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
                "duration": round(max(0.0, end_time - start_time), 3),
            }
        )
    return rows


def lap_marks(log, laps, config: SectionConfig) -> list[dict]:
    """每条圈上的边界**绝对时刻**：时间轴上画带子要用它。

    时间轴上一条圈一个区间，跨圈的带子必须按每条圈自己的速度重新定位——直接拿
    参考圈的秒数平移会画到隔壁圈去（这条是 ticket #7 里最容易画错的地方）。
    """
    out = []
    for lap in laps:
        try:
            distance = np.maximum.accumulate(lap_distance(log, lap))
        except ValueError:
            continue
        times = [round(value, 3) for value in _boundary_times(log, lap, distance, config)]
        out.append({"label": lap.label, "complete": bool(lap.complete), "times": times})
    return out


def band_at_time(marks: list[dict], time_s: float) -> dict | None:
    """时间轴上的一点落在哪一段里（ticket #8 的语义定义）。

    ``marks`` 是 :func:`lap_marks` 的输出：每条圈自己的边界**绝对时刻**。每条圈的
    速度不一样，所以必须按各圈自己的时刻找，**不能拿参考圈的秒数平移**。

    返回 ``{"lap", "index", "start_time", "end_time", "duration"}``；落在圈与圈之间的
    空档、或者那条圈的时刻不全时返回 ``None``——界面拿到 ``None`` 就照旧做"原地放大
    2 倍"，而不是猜一个段给用户。最后一条圈的最后一段**包含终点那一瞬间**，否则双击
    终点线没有任何反应。

    界面里的 ``sectionAtTime()`` 是同一套规则的薄移植（浏览器要能在快照里离线回答，
    不能回头问服务端）；两边用同一批边界情况钉住：``TestSections`` 与
    ``tools/smoke_viewer.js`` 第 23 组。
    """
    if not marks or time_s is None or not np.isfinite(time_s):
        return None
    last_lap = len(marks) - 1
    for position, mark in enumerate(marks):
        times = list(mark.get("times") or [])
        for index in range(len(times) - 1):
            start, end = float(times[index]), float(times[index + 1])
            if not end > start:
                continue
            closes = position == last_lap and index + 2 == len(times)
            if time_s >= start and (time_s <= end if closes else time_s < end):
                return {
                    "lap": mark.get("label"),
                    "index": index,
                    "start_time": start,
                    "end_time": end,
                    "duration": end - start,
                }
    return None


def band_window(rows: list[dict], index: int) -> tuple[float, float] | None:
    """区段表（:func:`bands` 的输出）里第 ``index`` 段的 ``(起, 止)`` 时刻，单位秒。

    越界、或者这一段的时刻不成立（止 ≤ 起）时返回 ``None``：界面拿到 ``None`` 会说
    清"这一段没有能用的时间范围"，而不是把视图缩成一个点。
    """
    if index is None or index < 0 or index >= len(rows):
        return None
    row = rows[int(index)]
    start, end = float(row["start_time"]), float(row["end_time"])
    if not end > start:
        return None
    return start, end


def _boundary_times(log, lap, distance: np.ndarray, config: SectionConfig) -> list[float]:
    """一条圈上每个边界的**绝对时刻**。

    首尾两个边界直接用这条圈自己的起止时刻：它们不是"某个距离第一次到达"，就是
    这条圈的起终线——车在终点线前停住时（耐久赛换人、回维修区），按"首次到达"
    算会把最后一截甩在带子外面，带子就跟圈速表对不上了。中间那些边界仍然按
    "首次到达那个距离"算，和 ``laps.time_at_distance`` 一个口径。
    """
    rate = float(log.sample_rate) or 1.0
    span = float(distance[-1]) if distance.size else 0.0
    last = len(config.boundaries) - 1
    times: list[float] = []
    for index, value in enumerate(config.boundaries):
        if index == 0:
            times.append(float(lap.start_time))
            continue
        if index == last:
            times.append(float(lap.end_time))
            continue
        frac = 0.0 if config.length_m <= 0 else float(value) / config.length_m
        target = min(max(frac, 0.0), 1.0) * span
        at = int(np.searchsorted(distance, target, side="left"))
        times.append(lap.start_time + min(at, max(distance.size - 1, 0)) / rate)
    return times


# ------------------------------------------------------------------ 侧车


def config_path(session_path: str | Path) -> Path:
    """``<场次>.ld`` / ``<场次>.csv`` -> ``<场次>.sections.json``。"""
    return Path(session_path).with_suffix(SIDE_SUFFIX)


def load_config(session_path: str | Path) -> SectionConfig | None:
    """读侧车；没存过就给 ``None``（"还没切过"，不是错误）。"""
    path = config_path(session_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"{path.name} 读不出来（{type(exc).__name__}: {exc}）。"
            f"修好这个 JSON，或者直接删掉它让区段回到自动切分。"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 的顶层应该是一个 JSON 对象。")
    return SectionConfig.from_dict(data)


def save_config(session_path: str | Path, config: SectionConfig) -> Path:
    path = config_path(session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def effective_config(log, laps) -> tuple[SectionConfig | None, str | None]:
    """这个场次现在生效的区段：侧车优先，没存过就按缺省参数自动切一份**不落盘**。

    返回 ``(config, notice)``；``config`` 为 ``None`` 表示"连一圈都没有，切不了"。
    """
    lap = reference_lap(laps)
    if lap is None:
        return None, "本场还没有圈，先放一个信标再来分区段"
    stored = load_config(log.path)
    if stored is not None and len(stored.boundaries) >= 2:
        notice = None
        if stored.reference_label and stored.reference_label != lap.label:
            notice = (
                f"这份区段是按第 {stored.reference_label} 圈切的，当前参考圈是第 {lap.label} 圈"
                f"（圈长差 {abs(stored.length_m - lap.distance):.0f} m）；"
                f"要按当前参考圈重切就点「重切」"
            )
        return stored, notice
    basis = (stored.basis if stored else None) or (available_bases(log) or ["lateral_g"])[0]
    return _auto_for_log(log, lap, basis, DEFAULT_SENSITIVITY, DEFAULT_MIN_SECTION_M), None


def _auto_for_log(
    log, lap, basis: str, sensitivity: float, min_length_m: float
) -> SectionConfig:
    _time, distance, measure = lap_frame(log, lap, basis)
    return auto_config(
        distance,
        measure,
        basis=basis,
        sensitivity=sensitivity,
        min_length_m=min_length_m,
        reference_label=lap.label,
    )


def auto_for_log(
    log, lap, basis: str, sensitivity: float = DEFAULT_SENSITIVITY,
    min_length_m: float = DEFAULT_MIN_SECTION_M,
) -> SectionConfig:
    """给一条圈自动切一份（服务端与报表共用这一个入口）。"""
    if basis not in BASIS_LABELS:
        raise ValueError(f"判据只认 {sorted(BASIS_LABELS)} 之一，收到的是 {basis!r}")
    if basis not in available_bases(log):
        raise ValueError(
            f"{log.path.name}: 这个场次切不了「{BASIS_LABELS[basis]}」——"
            f"曲率要 GPS 轨迹，横向加速度要有 `G Force Lat` 通道。"
        )
    return _auto_for_log(log, lap, basis, float(sensitivity), float(min_length_m))


def summarize(log, lap, config: SectionConfig) -> dict:
    """一句话总结：几条弯、几条直、各占多少米——界面和验收都用它当判据。"""
    rows = bands(log, lap, config)
    corners = [row for row in rows if row["kind"] == "corner"]
    straights = [row for row in rows if row["kind"] == "straight"]
    return {
        "corners": len(corners),
        "straights": len(straights),
        "corner_m": round(sum(row["length_m"] for row in corners), 1),
        "straight_m": round(sum(row["length_m"] for row in straights), 1),
        "lap_length_m": round(float(config.length_m), 1),
    }
