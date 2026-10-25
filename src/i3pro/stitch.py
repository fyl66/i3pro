"""把**接连录下来的几场**接成一场（耐久里换车手那种）。

为什么需要这个模块：耐久的两段是**两次记录**——车手换人时记录仪停了再起，于是
``.ld`` 是两份、合并出来的 CSV 也是两份。实测 ``10.25湾区``：``20261025-cjh耐久12圈``
记 16:05:08–16:29:03，``20261025-jhy耐久12圈`` 记 16:29:20–16:47:00，中间那 17 s
是换人。整场要看的东西（整场电耗、连着数的圈速表、通道报告）要的是**一场**。

这个模块只回答一件事：**把几场按给定顺序接在一条时间轴上**。

接缝怎么算（这里唯一需要做决定的地方）：后一场的 0 秒落在"前一场的最后一个采样点
**+ 一个采样间隔 + ``gap_s``**"上。

* ``gap_s=0``（默认）**丢掉**两次记录之间的停表时间，两段首尾相接——10.18 的
  ``20261018-耐久合并`` 就是这么做的（实测两段 1457.99 s + 1023.99 s = 2481.98 s，
  产物末点 2481.99 s、248,200 行，中间那 140 s 换人停顿不在里面）。
* ``gap_s>0`` 把停顿**按给定的秒数**留下。真实停表时间是墙钟算的（两次记录的
  ``ld_window`` 相减），但记录仪的钟本身不准，所以这个数由调用方给，模块不猜。

合并产物的**列 = 各场通道的并集**：第一场的名字与顺序在前，后面的场次里多出来的
名字接在后面（按名字排序，输出稳定）。同一个名字的两种写法（``Vx KF`` /
``Vx_KF``）算**同一条**，保留**第一场**那个写法——判据是 :func:`pairing.name_key`，
与 :func:`pairing.merge_sources` 是同一个答案。某一场没有的列留空（NaN）而不是补 0：
"这一场没记这条通道"和"这条通道读数是 0"不是一回事。

每一场的列都搬到**那一场自己的时间轴**上：CSV 合并产物本来就在同一条轴上，逐点原样
搬过去；``.ld`` 里有比主时间基慢的通道（实测高避金标准 437 条里有 147 条比 50 Hz 慢，
``Gear`` 只有 464 个样本 / 464 s），这些按零阶保持搬——与 :func:`pairing.hold_sample`
同一个语义（右端是**开**的：最后一个样本不往后带）。

纯函数：这里不读盘、不写盘、不 import 服务层。落盘走 :func:`write`，它落到
:func:`pairing.write_merged`——合并宽表的形状（首列 ``time_s``、第二行单位、UTF-8
带 BOM）只在一处定义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import pairing, timebase

__all__ = [
    "Plan",
    "StitchError",
    "axis_of",
    "plan",
    "stack",
    "step_of",
    "write",
]


class StitchError(ValueError):
    """接续参数或数据不对。消息给队友看：说清哪里不对 + 下一步怎么改。"""


def axis_of(session) -> np.ndarray:
    """这一场自己的时间轴（相对秒）。

    CSV / 文本 / Excel 场次给的是**它表里那一列**（``session.time``）——实测两段
    10.25 耐久与 ``timebase.axis`` 逐点相同，这里按它自己的来，免得给非均匀采样的
    导入表挪位置；``.ld`` 场次没有那一列，给主时间基（``timebase.axis``，与
    :func:`pairing.master_axis` 同一条）。
    """
    times = getattr(session, "time", None)
    if times is not None:
        return np.asarray(times, dtype=np.float64)
    return timebase.axis(session)


def step_of(session, axis: np.ndarray | None = None) -> float:
    """这一场的采样间隔（秒）。多档采样率的场次取**中位数**间隔。

    看不出来（只有一个采样点 / 间隔不是正数）就按 ``sample_rate`` 反推，再不行给 0
    ——那说明这场没有可用的时间轴，接出来的接缝也就不往后挪。
    """
    times = axis_of(session) if axis is None else np.asarray(axis, dtype=np.float64)
    if times.size > 1:
        step = float(np.median(np.diff(times)))
        if math.isfinite(step) and step > 0:
            return step
    rate = float(getattr(session, "sample_rate", 0.0) or 0.0)
    return 1.0 / rate if rate > 0 else 0.0


@dataclass(frozen=True)
class Plan:
    """接续方案：每场从合并时间轴的哪一秒开始、各占多少行、并掉了哪些写法。"""

    #: 每场在合并时间轴上的**偏移**（秒，加到它自己的时间轴上）。
    starts: tuple[float, ...]
    #: 每场自己时间轴的第一个 / 最后一个时刻（秒）。导入表的时间列可能不从 0 起，
    #: 所以"这一场从哪到哪"必须按它自己的轴算，不能拿 ``步长 × 行数`` 凑。
    heads: tuple[float, ...]
    tails: tuple[float, ...]
    #: 每场占多少行。
    rows: tuple[int, ...]
    #: 每场自己的采样间隔（秒）。
    steps: tuple[float, ...]
    #: 接缝上额外留出的秒数（``0`` = 首尾相接）。
    gap_s: float
    #: 被并成同一条的写法：``(丢弃的写法, 保留的写法)``。
    aliases: tuple[tuple[str, str], ...] = ()

    @property
    def total(self) -> int:
        """合并后一共多少行。"""
        return int(sum(self.rows))

    def seam(self, index: int) -> tuple[float, float]:
        """第 ``index`` 个接缝：``(前一场的末点, 后一场的首点)``（秒）。

        ``index`` 从 0 起，指的是"第 index 场与第 index+1 场之间那道缝"。
        """
        return (self.starts[index] + self.tails[index],
                self.starts[index + 1] + self.heads[index + 1])


def plan(sessions, gap_s: float = 0.0) -> Plan:
    """把接续方案算出来而不动数据：谁从哪一秒开始、几行、并掉了哪些写法。

    命令行"不给 ``--out`` 只打印方案"那一步与 :func:`stack` 用的是同一个函数，
    所以打印出来的接缝与真写出来的文件**必然是同一个**。
    """
    sessions = list(sessions)
    if len(sessions) < 2:
        raise StitchError(
            "接续至少要两场。只有一场的话不用接——直接用那份文件即可。"
        )
    gap_s = _checked_gap(gap_s)

    axes: list[np.ndarray] = []
    for session in sessions:
        axis = axis_of(session)
        if axis.size == 0:
            raise StitchError(f"{_label(session)} 一个采样点都没有，接不起来。")
        axes.append(axis)

    starts: list[float] = [0.0]
    steps: list[float] = []
    for index, (session, axis) in enumerate(zip(sessions, axes)):
        step = step_of(session, axis)
        steps.append(step)
        if index + 1 < len(sessions):
            # 后一场的**首点**落在"这一场末点 + 一个采样间隔 + 间隔"上；首点自己
            # 的偏移在下面减掉（CSV 场次的首点就是 0，不一定是）。
            wanted = starts[index] + float(axis[-1]) + step + gap_s
            starts.append(wanted - float(axes[index + 1][0]))

    _names, _by_key, aliases = _merged_names(sessions)
    return Plan(
        starts=tuple(starts),
        heads=tuple(float(axis[0]) for axis in axes),
        tails=tuple(float(axis[-1]) for axis in axes),
        rows=tuple(int(axis.size) for axis in axes),
        steps=tuple(steps),
        gap_s=gap_s,
        aliases=tuple(aliases),
    )


def stack(sessions, gap_s: float = 0.0):
    """把几场接成一场：返回 ``(time, columns, units, Plan)``。

    ``time`` 是合并后的时间轴（相对秒）；``columns`` 是列名 -> 浮点列，列名与顺序
    见模块开头那段；``units`` 是列名 -> 单位（第一场那个写法的单位优先，空的话拿
    后面场次的补上）。
    """
    sessions = list(sessions)
    scheme = plan(sessions, gap_s)

    names, by_key, _aliases = _merged_names(sessions)
    units: dict[str, str] = {}
    #: 每一场：它自己的通道名 -> 合并后的列名。
    mapped: list[dict[str, str]] = []
    for session in sessions:
        table: dict[str, str] = {}
        for channel in session.channels:
            kept = names[by_key[pairing.name_key(channel.name)]]
            table[channel.name] = kept
            unit = str(getattr(channel, "unit", "") or "")
            if unit and not units.get(kept):
                units[kept] = unit
        mapped.append(table)

    time = np.concatenate([
        axis_of(session) + start for session, start in zip(sessions, scheme.starts)
    ])
    columns = {name: np.full(time.size, np.nan) for name in names}

    at = 0
    for session, table in zip(sessions, mapped):
        axis = axis_of(session)
        stop = at + axis.size
        for channel in session.channels:
            values = np.asarray(session.values(channel.name), dtype=np.float64)
            if values.size == 0:
                continue                      # 整场没有样本：这一列留空
            if values.size == axis.size:
                placed = values               # CSV 合并产物：本来就在同一条轴上
            else:
                rate = float(getattr(channel, "sample_rate", 0.0) or 0.0)
                if rate <= 0:
                    raise StitchError(
                        f"{_label(session)} 的通道「{channel.name}」有 "
                        f"{values.size} 个样本、时间轴上有 {axis.size} 个位置，"
                        "而且读不到它自己的采样率——这条通道搬不到主时间基上。"
                        "下一步：把这一份重新导入一次（`i3pro info <文件>` 能看到"
                        "每条通道的采样率），再接。"
                    )
                grid = np.arange(values.size, dtype=np.float64) / rate
                placed = timebase.hold_sample(grid, values, axis)
            columns[table[channel.name]][at:stop] = placed
        at = stop

    return time, columns, units, scheme


def write(
    path: str | Path,
    time: np.ndarray,
    columns: dict[str, np.ndarray],
    *,
    units: dict[str, str] | None = None,
    progress=None,
) -> dict:
    """把接好的宽表写成 CSV（形状与合并场次完全一样，见模块开头）。"""
    return pairing.write_merged(
        path, time, columns, units=units, progress=progress,
    )


def _checked_gap(gap_s) -> float:
    gap = float(gap_s)
    if not math.isfinite(gap) or gap < 0:
        raise StitchError(
            f"接缝间隔要 ≥ 0 秒，给的是 {gap_s!r}。下一步：默认 0 就是两段首尾相接；"
            "要留住换人的停表时间，就给一个正数（秒）。"
        )
    return gap


def _merged_names(sessions) -> tuple[list[str], dict[str, int], list[tuple[str, str]]]:
    """合并后的列名、名字键 -> 列序号、被并成同一条的写法。

    第一场的名字与顺序在前；后面几场里**没出现过的**名字接在后面（按名字排序）。
    同一趟里出现两种写法（``Vx KF`` / ``Vx_KF``）时保留先来的那个。
    """
    names: list[str] = []
    by_key: dict[str, int] = {}
    aliases: list[tuple[str, str]] = []
    extra: list[str] = []
    for index, session in enumerate(sessions):
        for channel in session.channels:
            key = pairing.name_key(channel.name)
            if key in by_key:
                kept = names[by_key[key]]
                if kept != channel.name and (channel.name, kept) not in aliases:
                    aliases.append((channel.name, kept))
                continue
            by_key[key] = len(names)
            names.append(channel.name)
            if index > 0:
                extra.append(channel.name)
    if not names:
        raise StitchError(
            "这几场都没有通道，接起来是一张空表。下一步：先确认这两份文件读对了"
            "（`i3pro info <文件>` 看通道数）。"
        )
    # 后面几场多出来的名字按名字排序接在后面——与 `pairing.merge_sources` 同一条
    # 纪律：同样的输入永远给同样的列序，产物能 diff。
    if extra:
        ordered = (names[:len(names) - len(extra)]
                   + sorted(extra, key=lambda item: (pairing.name_key(item), item)))
        if ordered != names:
            names = ordered
            by_key = {pairing.name_key(name): position
                      for position, name in enumerate(names)}
    return names, by_key, aliases


def _label(session) -> str:
    path = getattr(session, "path", None)
    return Path(path).name if path else "(没有名字的一场)"
