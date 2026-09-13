"""时间报告与通道报告（i2 Pro 的 Time Report / Channel Report）。

两张表回答两个不同的追问：

* **时间报告**：一条圈里的每个区段各用了多久、谁最快、把各段的最快值加起来
  （**理论最快圈**）是多少。表是「区段 × 圈」的矩阵，每格是那一段的用时，按
  「离该段最快有多远」分档，供界面着色。
* **通道报告**：选几条通道，按圈或按区段列出统计量——最小 / 最大 / 绝对最大 /
  均值 / 起值 / 终值 / 变化量 / 标准差。

两张表都能按区段类型过滤（只看弯 / 只看直），都能导出 CSV。CSV 的列**就是**
``columns`` 里的标签，一行不多一行不少：快照与服务两种模式拿的是同一份
``columns`` + ``cells``，所以导出来的东西不会因为换了个模式就变了样。

所有数值都在这里算完（界面只负责排版与着色），因为"这条通道这一段的均值是多少"
必须能从命令行复现，不能只在浏览器里成立。

口径（写进 CONTEXT.md，改这里就要改那里）：

* 区段的时间窗取自 :func:`i3pro.sections.lap_marks`——每条圈按**自己**的边界时刻，
  不是拿参考圈的秒数平移。
* 起值 / 终值 = 窗口内**第一个 / 最后一个有效样本**（NaN 不算数）；没有有效样本
  给 ``None``，给 0 会被当成一次真实测量。
* 标准差是总体标准差（``ddof=0``）。
* 理论最快圈按**完整圈**取每段最快再求和；一条完整圈都没有时退回全部圈，并在
  ``summary.based_on`` 里说明用了哪些。
* 连续最快圈（Rolling Minimum）= 在**连续行驶**的数据上滑动一个"一圈长度"的窗口，
  取用时最短的那个。窗口两端都用**首次到达**那个距离的时刻，和
  :func:`i3pro.laps.time_at_distance` 一个口径：车停在原地时同一个距离会持续很久，
  窗口定义在距离上，车在这段距离里停着不动的那几十秒就是这段距离的一部分。
  （实测：这条规矩与"每个样本都当起点"的写法在**两份金标准上给出同一个答案**
  ——39.75 s 与 54.45 s；两种写法只在"窗口的一头正好压在停车段上"时才分岔，
  ``TestReport`` 里有一条合成用例把那个分岔钉住了。）
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np

from . import derive, laps as lapsmod, sections as sectionsmod, timebase

__all__ = [
    "STAT_LABELS",
    "STAT_KEYS",
    "BANDS",
    "band_for",
    "stats",
    "master_time",
    "lap_windows",
    "section_windows",
    "time_report",
    "channel_report",
    "format_cell",
    "to_csv",
]

#: 统计量的顺序就是表里的列序，也是 CSV 的列序。
STAT_LABELS: tuple[tuple[str, str], ...] = (
    ("min", "最小"),
    ("max", "最大"),
    ("abs_max", "绝对最大"),
    ("mean", "均值"),
    ("start", "起值"),
    ("end", "终值"),
    ("change", "变化量"),
    ("std_dev", "标准差"),
)
STAT_KEYS: tuple[str, ...] = tuple(key for key, _ in STAT_LABELS)

#: 「接近最快」的分档：一格用时 ÷ 该段最快 ≤ ``max_ratio`` 就落进这一档。
#: 阈值是相对量而不是绝对秒数——直道差 0.05 s 和发夹弯差 0.05 s 不是一回事。
BANDS: tuple[dict, ...] = (
    {"key": "best", "label": "最快", "max_ratio": 1.005},
    {"key": "close", "label": "接近", "max_ratio": 1.03},
    {"key": "fair", "label": "一般", "max_ratio": 1.08},
)

THEORETICAL_NOTE = (
    "理论最快圈 = 每个区段各自的最快用时相加，现实中不可能每段同时最快，"
    "它是一个参考下限，不是一条真跑出来的圈。"
)


# ------------------------------------------------------------------ 基础量


def master_time(log) -> np.ndarray:
    """主时间基（秒），和载荷、轨迹、距离轴用的是同一条。

    这条轴的定义只在 `timebase` 里（ticket #22）；这里留个名字是为了不破坏
    已经导出的接口，它自己不再算一遍。
    """
    return timebase.axis(log)


def _finite(values) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        return arr
    return arr[np.isfinite(arr)]


def stats(values) -> dict[str, float | None]:
    """一个窗口里的八项统计量；窗口里没有有效样本时每一项都是 ``None``。

    起值 / 终值是**有效样本**里的第一个和最后一个，所以窗口开头那段 NaN
    （比如传感器还没上电）不会把起值变成 NaN。
    """
    arr = _finite(values)
    if arr.size == 0:
        return {key: None for key in STAT_KEYS}
    return {
        "min": float(arr.min()),
        "max": float(arr.max()),
        "abs_max": float(np.abs(arr).max()),
        "mean": float(arr.mean()),
        "start": float(arr[0]),
        "end": float(arr[-1]),
        "change": float(arr[-1] - arr[0]),
        "std_dev": float(arr.std()),
    }


def band_for(ratio: float | None) -> str:
    """一格用时相对该段最快的比值落在哪一档；比值不可用时给空串。"""
    if ratio is None or not np.isfinite(ratio):
        return ""
    for band in BANDS:
        if ratio <= band["max_ratio"]:
            return str(band["key"])
    return ""


def _round(value: float | None, digits: int = 3):
    if value is None:
        return None
    value = float(value)
    return None if not np.isfinite(value) else round(value, digits)


def _kind_label(kind: str | None) -> str:
    return sectionsmod.KIND_LABELS.get(kind or "", kind or "")


# ------------------------------------------------------------------ 时间窗


def lap_windows(laps) -> list[dict]:
    """每条圈一个窗口，**不**切区段（通道报告"按圈"分组用）。"""
    out = []
    for lap in laps:
        out.append(
            {
                "lap": lap.label,
                "section": None,
                "kind": None,
                "kind_label": "",
                "name": "",
                "start": float(lap.start_time),
                "end": float(lap.end_time),
                "duration": float(lap.lap_time),
                "complete": bool(lap.complete),
            }
        )
    return out


def section_windows(
    log,
    laps,
    config: sectionsmod.SectionConfig,
    kind: str | None = None,
    lap_label: str | None = None,
) -> list[dict]:
    """每条圈 × 每个区段一个窗口。

    ``kind`` 只看弯或只看直；``lap_label`` 只留一条圈（通道报告"按区段"分组时
    默认只看参考圈——区段本来就是定义在参考圈上的）。距离轴坏掉的圈会被
    :func:`i3pro.sections.lap_marks` 跳过，这里跟着跳过，不猜时刻。
    """
    marks = sectionsmod.lap_marks(log, laps, config)
    if lap_label is not None:
        marks = [mark for mark in marks if str(mark.get("label")) == str(lap_label)]
    out: list[dict] = []
    for mark in marks:
        label = mark.get("label")
        times = list(mark.get("times") or [])
        complete = bool(mark.get("complete"))
        for index, section_kind in enumerate(config.kinds):
            if index + 1 >= len(times):
                break
            if kind is not None and section_kind != kind:
                continue
            start, end = float(times[index]), float(times[index + 1])
            if not end > start:
                continue
            name = config.names[index] if index < len(config.names) else ""
            out.append(
                {
                    "lap": label,
                    "section": index,
                    "kind": section_kind,
                    "kind_label": _kind_label(section_kind),
                    "name": name or f"{_kind_label(section_kind)} {index + 1}",
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "complete": complete,
                }
            )
    return out


# ------------------------------------------------------------------ 连续最快圈


def rolling_best(log, lap_length_m: float) -> dict | None:
    """连续最快圈：滑动一个"一圈长度"的窗口，取用时最短的那个。

    窗口是**沿距离**滑的，所以它可以跨过起点线——这正是它和"最快圈"的区别：
    最快圈是两条信标之间的那段，连续最快圈是把整场连续数据里任意一段一圈长度
    的行驶挑出来。两端都用首次到达该距离的时刻（见模块文档）。
    """
    length = float(lap_length_m or 0.0)
    if length <= 0:
        return None
    try:
        distance = derive.distance_series(log)
    except ValueError:
        return None
    time = master_time(log)
    size = min(distance.size, time.size)
    if size < 3:
        return None
    distance, time = distance[:size], time[:size]
    travelled = np.maximum.accumulate(distance)
    total = float(travelled[-1] - travelled[0])
    if total < length:
        return None

    # 每个**新**距离值只留一个起点（首次到达它的那一刻）。车停着不动时同一个
    # 距离会持续几分钟，若把停车的尾端也算作起点，窗口会从那儿开始计时，停车
    # 的秒数就被抹掉了——见 ``TestReport`` 里那条"停车段算不算数"的用例。
    steps = np.flatnonzero(np.diff(travelled, prepend=travelled[0] - 1.0) > 0.0)
    if steps.size < 2:
        return None
    reach = np.searchsorted(travelled, travelled[steps] + length, side="left")
    keep = reach < travelled.size
    if not np.any(keep):
        return None
    starts = steps[keep]
    ends = reach[keep]
    durations = time[ends] - time[starts]
    position = int(np.argmin(durations))
    start_index, end_index = int(starts[position]), int(ends[position])
    return {
        "duration": round(float(durations[position]), 3),
        "start_time": round(float(time[start_index]), 3),
        "end_time": round(float(time[end_index]), 3),
        "start_distance": round(float(travelled[start_index]), 1),
        "end_distance": round(float(travelled[end_index]), 1),
        "lap_length_m": round(length, 1),
    }


# ------------------------------------------------------------------ 时间报告


def time_report(
    log,
    laps,
    config: sectionsmod.SectionConfig,
    kind: str | None = None,
) -> dict:
    """「区段 × 圈」的分段计时矩阵 + 理论最快圈 + 连续最快圈。

    行 = 区段（按距离顺序，和赛道区段面板里的序号一致），列 = 每条圈，最后一列
    是那一列的段最快与它出自哪条圈。列宽随圈数变化，所以 ``columns`` 是数据，
    不是写死的表头。
    """
    windows = section_windows(log, laps, config, kind=kind)
    lap_labels = [lap.label for lap in laps]
    complete = {lap.label: bool(lap.complete) for lap in laps}

    # 只算真正跑完整的圈：分段计时里混进一条被截断的圈（进站、回维修区），
    # 会把"理论最快圈"拉到一个跑不出来的值。
    trusted = {label for label, done in complete.items() if done} or set(lap_labels)

    order: list[int] = []
    table: dict[int, dict[str, float]] = {}
    names: dict[int, str] = {}
    kinds: dict[int, str] = {}
    for window in windows:
        index = int(window["section"])
        if index not in table:
            order.append(index)
            table[index] = {}
            names[index] = window["name"]
            kinds[index] = window["kind"]
        table[index][window["lap"]] = float(window["duration"])
    order.sort()

    columns: list[dict] = [
        {"key": "section", "label": "区段", "type": "text"},
        {"key": "kind", "label": "类型", "type": "text"},
    ]
    columns += [
        {
            "key": f"lap:{label}",
            # 没跑完的圈（进站、被截断）在表头就标出来：不标的话，一格 1.6 s 的
            # "直道"会被当成一条神一样的走线。
            "label": f"圈 {label}" if complete.get(label, True) else f"圈 {label}（未完）",
            "type": "time",
            "decimals": 3,
        }
        for label in lap_labels
    ]
    columns += [
        {"key": "best", "label": "段最快", "type": "time", "decimals": 3},
        {"key": "best_lap", "label": "出自", "type": "text"},
        {"key": "spread", "label": "快慢差", "type": "time", "decimals": 3},
    ]

    rows: list[list] = []
    row_kinds: list[str] = []
    row_labels: list[str] = []
    theoretical = 0.0
    theoretical_from: dict[str, str] = {}
    for index in order:
        durations = table[index]
        candidates = {label: value for label, value in durations.items() if label in trusted}
        if not candidates:
            candidates = dict(durations)
        best_lap = min(candidates, key=lambda label: candidates[label])
        best = float(candidates[best_lap])
        worst = float(max(candidates.values()))
        label = f"{index + 1}. {names[index]}"
        cells: list = [label, _kind_label(kinds[index])]
        cells += [_round(durations.get(name)) for name in lap_labels]
        cells += [_round(best), str(best_lap), _round(worst - best)]
        rows.append(cells)
        row_kinds.append(kinds[index])
        row_labels.append(names[index])
        theoretical += best
        theoretical_from[str(index)] = str(best_lap)

    best_lap_row = None
    finished = [lap for lap in laps if lap.complete] or list(laps)
    if finished:
        quickest = min(finished, key=lambda lap: lap.lap_time)
        best_lap_row = {"lap": quickest.label, "lap_time": _round(quickest.lap_time, 3)}

    based_on = "完整圈" if any(complete.values()) else "全部圈（本场没有完整圈）"
    return {
        "kind": "time",
        "columns": columns,
        "rows": rows,
        "row_kinds": row_kinds,
        "row_labels": row_labels,
        "lap_labels": [str(label) for label in lap_labels],
        "complete_laps": [str(label) for label in lap_labels if complete.get(label)],
        "bands": [dict(band) for band in BANDS],
        "section_count": len(order),
        "filter": kind or "all",
        "summary": {
            "theoretical": _round(theoretical) if order else None,
            "theoretical_from": theoretical_from,
            "based_on": based_on,
            "best_lap": best_lap_row,
            "rolling": rolling_best(log, float(config.length_m)),
            "corners": sum(1 for index in order if kinds[index] == "corner"),
            "straights": sum(1 for index in order if kinds[index] == "straight"),
            "note": THEORETICAL_NOTE,
        },
    }


# ------------------------------------------------------------------ 通道报告


def channel_report(
    log,
    laps,
    config: sectionsmod.SectionConfig,
    channels: Sequence[str],
    by: str = "lap",
    kind: str | None = None,
    lap_label: str | None = None,
) -> dict:
    """通道统计量表：一行 = 一个分组条目 × 一条通道，列 = 八项统计量。

    ``by="lap"`` 每行是"某条圈里的某条通道"；``by="section"`` 每行是"某个区段
    里的某条通道"，窗口取 ``lap_label``（默认参考圈）那一条圈上的区段时刻——
    区段本身是定义在参考圈上的，拿别的圈算要用它自己的边界时刻。
    """
    if by not in ("lap", "section"):
        raise ValueError(f"分组只认 lap / section，收到的是 {by!r}")

    reference = sectionsmod.reference_lap(laps)
    if by == "lap":
        windows = lap_windows(laps)
    else:
        label = lap_label or (reference.label if reference is not None else None)
        if label is None:
            windows = []
        else:
            windows = section_windows(log, laps, config, kind=kind, lap_label=label)

    chosen = [name for name in channels if name and log.has(name)]
    columns: list[dict] = [
        {"key": "group", "label": "分组", "type": "text"},
        {"key": "lap", "label": "圈", "type": "text"},
        {"key": "section", "label": "区段", "type": "text"},
        {"key": "kind", "label": "类型", "type": "text"},
        {"key": "channel", "label": "通道", "type": "text"},
        {"key": "unit", "label": "单位", "type": "text"},
    ]
    columns += [
        {"key": key, "label": label, "type": "stat", "decimals": 5}
        for key, label in STAT_LABELS
    ]

    time = master_time(log)
    size = time.size
    series: dict[str, np.ndarray] = {}
    for name in chosen:
        values = derive.hold_to_master(log, name)
        if values.size < size:
            pad = values[-1] if values.size else 0.0
            values = np.concatenate([values, np.full(size - values.size, pad)])
        series[name] = values[:size]

    rows: list[list] = []
    for window in windows:
        start, end = float(window["start"]), float(window["end"])
        lo = int(np.searchsorted(time, start, side="left"))
        hi = int(np.searchsorted(time, end, side="left"))
        hi = max(hi, lo)
        section_label = "" if window["section"] is None else f"{window['section'] + 1}. {window['name']}"
        group = window["lap"] if window["section"] is None else f"{window['lap']} · {window['name']}"
        for name in chosen:
            values = series[name][lo:hi]
            found = stats(values)
            rows.append(
                [
                    group,
                    str(window["lap"]),
                    section_label,
                    _kind_label(window["kind"]),
                    name,
                    log.channel(name).unit,
                    *[_round(found[key], 5) for key in STAT_KEYS],
                ]
            )

    units = {name: log.channel(name).unit for name in chosen}
    missing = [name for name in channels if name and not log.has(name)]
    return {
        "kind": "channels",
        "columns": columns,
        "rows": rows,
        "by": by,
        "filter": kind or "all",
        "channels": chosen,
        "units": units,
        "missing": missing,
        "lap": None if by == "lap" else (
            None if reference is None and lap_label is None else str(lap_label or reference.label)
        ),
        "reference_lap": None if reference is None else str(reference.label),
        "lap_labels": [str(lap.label) for lap in laps],
        "stat_keys": list(STAT_KEYS),
        "notes": [
            # 这些是**纯文本**（界面直接显示，不当 Markdown 渲染），不要写 ** 强调
            "统计量算在原始采样上，不是图上抽稀后的点。",
            "起值 / 终值取窗口里第一个 / 最后一个有效样本；标准差是总体标准差。",
            (
                "按区段分组时窗口取参考圈（或下拉里选的那条圈）自己的区段时刻。"
                if by == "section"
                else "按圈分组时窗口就是整条圈。"
            ),
        ],
    }


# ------------------------------------------------------------------ 导出


def format_cell(value, column: dict | None = None) -> str:
    """一格写成 CSV 里的样子：数字按该列的小数位，含逗号/引号/换行的加引号。"""
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not np.isfinite(number):
            return ""
        digits = (column or {}).get("decimals")
        text = f"{number:.{int(digits)}f}" if digits is not None else repr(number)
    else:
        text = str(value)
    if any(char in text for char in ',"\n'):
        text = '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns: Iterable[dict], rows: Iterable[Sequence]) -> str:
    """表格 → CSV 文本。表头就是 ``columns`` 里的标签，和界面/快照同源。"""
    columns = list(columns)
    lines = [",".join(format_cell(column["label"], None) for column in columns)]
    for row in rows:
        lines.append(
            ",".join(
                format_cell(value, columns[index] if index < len(columns) else None)
                for index, value in enumerate(row)
            )
        )
    return "\n".join(lines) + "\n"


def report_payload(
    log,
    laps,
    config: sectionsmod.SectionConfig,
    channels: Sequence[str],
    kind: str | None = None,
    by: str = "lap",
    lap_label: str | None = None,
) -> dict:
    """一次把两张表都算出来——快照与 ``/api/.../report`` 用同一个出口。"""
    return {
        "time": time_report(log, laps, config, kind=kind),
        "channels": channel_report(
            log, laps, config, channels, by=by, kind=kind, lap_label=lap_label
        ),
    }
