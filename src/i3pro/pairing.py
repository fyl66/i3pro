"""把一份 CAN 记录仪帧表对到它对应的 ``.ld`` 场次，并把两边的通道合成一场。

为什么需要这个模块：C125 记的东西和记录仪记的东西**从来不在同一份文件里**。
实测 ``10.5湾区``：``.ld`` 有 459-474 条通道（电机 / AMK / MTI / GPS），
就是没有 IVT 的 ``I_Data`` / ``U1_Data``；帧表解出来只有 114 条（``Vx_KF``、
``latitude_MTI``、``I_Data``...），没有电机通道。想知道"电机电流推母线电流"，
两边必须在同一个场次里。

这个模块回答两个问题，各自只回答一次：

* **对到哪一场**（``ld_window`` / ``recording_window`` / ``candidates``）——
  ``.ld`` 头里的 ``log_time`` 是**记录结束时刻**（不是开始），
  ``begin = log_time - duration``。帧表那边用文件名里的日期 + 首/末墙钟。
  墙钟只做到"挑出候选"，真正的偏移交给 :func:`align`。
* **偏移是多少**（``align``）——墙钟在两边各有各的零点（记录仪与 C125 的钟差
  实测在几秒量级），所以偏移要在**相对时钟**上用一条两边都有的通道
  （``Vx KF`` / ``Vx_KF``）互相关求出来，不看墙钟。

合并的时间轴**以 ``.ld`` 为准**（用户确认）：时长、时间零点、采样率都是
``.ld`` 的，CAN 通道按零阶保持重采样上去，覆盖不到的地方留空。

纯函数：这里不读盘、不写盘、不 import 服务层。``merge_columns`` 只把列算出来，
落盘由 :func:`write_merged` 在文件末尾管。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from . import channelref, timebase

__all__ = [
    "Alignment",
    "Candidate",
    "Pairing",
    "align",
    "candidates",
    "expected_offset",
    "frame_groups",
    "hold_into",
    "hold_sample",
    "ld_window",
    "merge_columns",
    "merge_sources",
    "overlap_seconds",
    "recording_window",
    "write_merged",
]

#: 配对用的共同通道：两边都有速度（``.ld`` 叫 ``Vx KF``，帧表解码叫 ``Vx_KF``，
#: 归一化之后是同一个键）。
SPEED_CANDIDATES = ("Vx KF", "Vx_KF", "VxKF")
#: 第二判据用的加速度通道。速度在急加速/急减速之外几乎是平的，短窗口上很容易
#: 撞出假的高相关（实测：窗口里只有几段直线时，错偏移上也能给出 r=1.0000）。
#: 加速度一直有起伏，拿它复核同一条偏移，假配对就掉下来了。
ACCEL_CANDIDATES = ("accY MTI", "accY_MTI", "AccY_MTI", "AccY MTI")
#: 复核用的相关系数门槛。加速度两边是同一个传感器（``.ld`` 走 CAN、帧表走
#: 原始帧），对齐之后实测真配对 >= 0.9；给 0.6 是给两边的量程/符号留余量。
MIN_ACCEL_R = 0.6
#: 墙钟重叠少于这个秒数的记录不参加配对。
MIN_OVERLAP_S = 20.0
#: 允许的钟差上限：墙钟给出的偏移与互相关搜出来的偏移相差超过它就不认。
#: 这个差值来自两处——两边的"零点"取法不同（记录仪取首帧、``.ld`` 取第一个采样
#: 点），以及 ``.ld`` 头的时刻只精到秒。实测：``10.5湾区`` 最大 41 s
#: （``102857``），``10.4湾区测试`` 最大 105 s（``133436``，那天记录稀疏、每段很短，
#: 墙钟窗口与 ``.ld`` 的内容窗口差得多）。给到 120 s 把这批都罩住；真正的判据
#: 始终是**两个通道的相关系数**，这个只挡"峰跳到自己搜索范围的边上"那类。
MAX_LAG_S = 120.0
#: 互相关在"墙钟估出来的偏移"附近搜多少秒。
#:
#: 墙钟那个估计只当搜索中心，不当结论：它的误差来自两处——``.ld`` 头里的时刻只到
#: 秒，而两边的"零点"取法不同（记录仪取首帧、``.ld`` 取第一个采样点，两边各自
#: 还有一段"开始记之前"的启动时间）。实测同一个共享上差到 41 s（``102857``：
#: 墙钟说 -200.1，真正的峰在 -241.0）。120 s 把这批全罩住，同时**不足以**把
#: 同一天相邻的另一趟记录（实测最近的两趟相隔 37 s，多数相隔几分钟）搜进来。
#: 真正的判据始终是相关系数本身，不是这个搜索宽度。
SEARCH_S = 120.0
#: 大范围搜索的步长 [s]。120 s x 2 / 0.2 = 1200 次比较，每次几百个点，很便宜；
#: 峰顶再按 :data:`LAG_STEP_S` 精修一次。
SEARCH_STEP_S = 0.2
#: 偏移搜索的步长。100 Hz 的轴，0.01 s 已经比任何一次记录仪启动抖动都细。
LAG_STEP_S = 0.01
#: 相关系数低于这个值就不认（正确配对的实测都在 0.99 以上）。
MIN_CORRELATION = 0.9
#: 互相关的比较窗口上限：最多比这么多秒的同一条时间线。窗口越长越稳，但要把
#: "整段反复跑同一个八字"那种周期性算进去——300 s 在实测数据上给的是同一段驾驶。
COMPARE_WINDOW_S = 300.0
#: 比较窗口里的采样率。速度信号在 1 Hz 上已经足够认人。
COMPARE_RATE = 1.0


def _normal(text: str) -> str:
    """去掉空格 / 下划线 / 大小写——"同一个名字的两种写法"只认这一条。"""
    return channelref.normalise(text)


def _same_name(left: str, right: str) -> bool:
    """两个通道名是不是"同一个名字的两种写法"（``accX MTI`` / ``accX_MTI``）。

    这里**不能**用 :func:`_normal` 那种"只留字母数字"的归并：``AngVelX_body`` 与
    ``AngVelX_hor`` 归并之后都是 ``angvelx``，两条真的不同的通道会被当成一条丢掉
    （实测 CAN 那侧就是这么少了十几条通道）。所以只在"两边都只差空格 / 下划线 /
    大小写"时才认为同名。
    """
    return _key(left) == _key(right)


def _key(name: str) -> str:
    """:func:`_same_name` 的键形式（两侧都只差空格 / 下划线 / 大小写时相等）。"""
    return name.replace("_", "").replace(" ", "").lower()


#: 判断"这一列是空的"的容差。C125 里没接上的模板通道不是**精确**的 0：实测
#: ``传杰八字`` 的 ``I Data`` 是 ``-0.0015 … 0``（记录仪自己的量化底噪），
#: 严格 `== 0` 会判成"有数据"，于是帧表里真的有数的 ``I_Data`` 被丢掉。
#: 反过来，IVT 的量程到 1500 A、分辨率 1 mA，小于 10 mA 的读数本来就等于没有。
ZERO_TOLERANCE = 0.01


def _empty(values: np.ndarray) -> bool:
    """这一列是不是"占位空列"：全是 NaN，或者全都是 0（含量化底噪）。

    C125 的工程里挂着 IVT 的模板通道（``I Data`` / ``U1 Data`` …：单位 A / V，
    名字对得上，但传感器没接上，整段都是 0）。合并时如果让这种空列把帧表里
    **真的有数**的 ``I_Data`` 顶掉，用户拿到的是一个全 0 的母线电流——正是他想
    要的那条通道反而没了。所以空列可以被对面替换掉。
    """
    if values.size == 0:
        return True
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return True
    return bool(np.all(np.abs(finite) < ZERO_TOLERANCE))


# --------------------------------------------------------------------- 时间窗
def ld_window(log) -> tuple[datetime, datetime]:
    """``.ld`` 的绝对墙钟窗口 ``(开始, 结束)``。

    头里的 ``log_time`` 是记录**结束**时刻：实测 9 份 10.5 数据，
    ``log_time - duration`` 才是开始时刻（``cjh阶跃+脉冲.ld`` = 17:51:15 -> 18:13:50，
    与帧表的首/末墙钟对上）。日期是 ``dd/mm/yyyy``。
    """
    day, month, year = str(log.log_date).split("/")
    end = datetime.strptime(f"{year}-{month}-{day} {log.log_time}", "%Y-%m-%d %H:%M:%S")
    start = end - timedelta(seconds=float(log.duration))
    return start, end


def recording_window(items: list[dict]) -> tuple[datetime, datetime]:
    """一次记录（可能由几卷拼成）的绝对墙钟窗口。

    ``items`` 是 ``canlog.group_recordings`` 里那种 ``{"path", "first_wall",
    "last_wall", ...}``；墙钟列只有时刻，日期从文件名拿。
    """
    if not items:
        raise ValueError("空的一次记录，算不出时间窗。")
    ordered = sorted(items, key=lambda item: item["first_wall"])
    return (
        _absolute(ordered[0]["path"], ordered[0]["first_wall"]),
        _absolute(ordered[-1]["path"], ordered[-1]["last_wall"]),
    )


def _absolute(path, wall: float) -> datetime:
    """``.csv`` 文件名里的日期 + 墙钟时刻 -> 绝对时间。"""
    name = Path(path).name
    parts = name.split("_")
    day = "-".join(parts[:3]) if len(parts) >= 3 else "1970-01-01"
    base = datetime.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    return base + timedelta(seconds=float(wall))


@dataclass
class Candidate:
    """一个"时间上挨得着"的帧表记录。是不是真的同一段驾驶还没验。"""

    sources: list[Path]
    window: tuple[datetime, datetime]
    overlap_s: float

    @property
    def main(self) -> Path:
        return self.sources[0]

    @property
    def label(self) -> str:
        name = self.main.name
        return name + (f" +{len(self.sources) - 1}" if len(self.sources) > 1 else "")


def candidates(ld_start, ld_end, groups, *, min_overlap_s: float = MIN_OVERLAP_S) -> list[Candidate]:
    """墙钟重叠 >= ``min_overlap_s`` 的帧表记录，按重叠从大到小排。"""
    out: list[Candidate] = []
    for group in groups:
        items = group["items"] if isinstance(group, dict) else group
        if not items:
            continue
        start, end = recording_window(items)
        overlap = (min(ld_end, end) - max(ld_start, start)).total_seconds()
        if overlap >= min_overlap_s:
            ordered = sorted(items, key=lambda item: item["first_wall"])
            out.append(Candidate(
                sources=[Path(item["path"]) for item in ordered],
                window=(start, end),
                overlap_s=overlap,
            ))
    out.sort(key=lambda item: -item.overlap_s)
    return out


def frame_groups(frames) -> list[dict]:
    """把帧表按"一次记录"分组（``canlog.group_recordings`` 的那套判据）。"""
    from . import canlog

    paths = [Path(path) for path in frames]
    return canlog.group_recordings(paths) if paths else []


def expected_offset(ld_start: datetime, candidate: Candidate) -> float:
    """墙钟给出的偏移估计 [s]：``.ld`` 的 ``t`` 秒对应记录仪的 ``t + offset`` 秒。

    ``.ld`` 的 0 秒 = 它的窗口起点 ``B_ld``；记录仪的 0 秒 = 它首帧的墙钟
    ``B_can``（``read_can_session`` 把时间轴按第一帧归零）。同一个绝对时刻在
    两边分别是 ``B_ld + t`` 与 ``B_can + t_can``，所以

        ``t_can = t + (B_ld - B_can)``，即 ``offset = B_ld - B_can``。

    记录仪先开机时 ``B_can < B_ld``，偏移是**正的**。墙钟只精到秒级（``.ld``
    头里没有毫秒），所以这个值只当搜索中心，精确值由互相关给。
    """
    return (ld_start - candidate.window[0]).total_seconds()


def overlap_seconds(ld_start: datetime, ld_end: datetime, candidate: Candidate) -> float:
    """两边墙钟上都真的有内容的那一段，有多少秒。"""
    begin = max(ld_start, candidate.window[0])
    end = min(ld_end, candidate.window[1])
    return max(0.0, (end - begin).total_seconds())


# ---------------------------------------------------------------------- 对齐
@dataclass
class Alignment:
    """两条速度曲线互相关的结果。"""

    offset_s: float
    r: float
    points: int
    ok: bool
    reason: str = ""
    #: 复核用的加速度相关系数（``None`` = 有一边没有这条通道，没复核）。
    accel_r: float | None = None

    def as_dict(self) -> dict:
        return {
            "offset_s": round(self.offset_s, 4),
            "vx_r": round(self.r, 4),
            "vx_points": self.points,
            "accel_r": None if self.accel_r is None else round(self.accel_r, 4),
            "ok": self.ok,
            "reason": self.reason,
        }


def hold_sample(times: np.ndarray, values: np.ndarray, at: np.ndarray) -> np.ndarray:
    """零阶保持重采样：``at`` 落在两个采样点之间时取前一个（与 canlog 一致）。"""
    at = np.asarray(at, dtype=np.float64)
    if times.size == 0 or values.size == 0:
        return np.full(at.size, np.nan)
    inside = (at >= times[0]) & (at < times[-1])
    index = np.clip(np.searchsorted(times, at, side="right") - 1, 0, values.size - 1)
    return np.where(inside, values[index], np.nan)


def hold_into(times: np.ndarray, values: np.ndarray, at: np.ndarray, into: np.ndarray) -> None:
    """同 :func:`hold_sample`，但写进 ``into``，而且**只填还没有值的位置**。

    一个 ``.ld`` 对上不止一次记录时（实测 ``10.4湾区测试``：同一场高避的两段分别
    被两次记录录到），两次记录各自覆盖一段时间轴。先来的先填，后来的只补空档——
    同一时刻不互相盖，合出来的场次哪一段来自哪一次记录是可解释的。
    """
    at = np.asarray(at, dtype=np.float64)
    if times.size == 0 or values.size == 0:
        return
    inside = (at >= times[0]) & (at < times[-1])
    if not inside.any():
        return
    index = np.clip(np.searchsorted(times, at, side="right") - 1, 0, values.size - 1)
    fill = inside & ~np.isfinite(into)
    into[fill] = values[index[fill]]
def _speed_of(session, names) -> tuple[np.ndarray, np.ndarray]:
    """会话里第一条存在的候选通道 ``(time, values)``；都没有就抛 ``KeyError``。"""
    known = {channel.name for channel in session.channels}
    for name in names:
        hit = channelref.lookup(known, name)
        if hit is None:
            continue
        channel = session.channel(hit)
        times = np.asarray(channel.time(), dtype=np.float64)
        values = np.asarray(session.values(hit), dtype=np.float64)
        if times.size and values.size:
            size = min(times.size, values.size)
            return times[:size], values[:size]
    raise KeyError(" / ".join(names))


def _pearson(left: np.ndarray, right: np.ndarray) -> float:
    """两个数组各自的均值去掉之后的皮尔森相关；常数向量给 -2（认不出来）。"""
    left = left - left.mean()
    right = right - right.mean()
    denom = float(np.sqrt((left * left).sum() * (right * right).sum()))
    if denom <= 0.0:
        return -2.0
    return float((left * right).sum() / denom)


def _accel_correlation(log, session, at: np.ndarray, lag: float) -> float | None:
    """同一条偏移上复核一次加速度；有一边没有这条通道就给 ``None``（没复核）。"""
    try:
        ref_t, ref_v = _speed_of(log, ACCEL_CANDIDATES)
        can_t, can_v = _speed_of(session, ACCEL_CANDIDATES)
    except KeyError:
        return None
    ref = hold_sample(ref_t, ref_v, at)
    other = hold_sample(can_t, can_v, at + float(lag))
    mask = np.isfinite(ref) & np.isfinite(other)
    if int(mask.sum()) < 10:
        return None
    return _pearson(ref[mask], other[mask])


def align(
    log,
    session,
    *,
    ld_start: datetime | None = None,
    can_window: tuple[datetime, datetime] | None = None,
    expect_s: float = 0.0,
    max_lag_s: float = MAX_LAG_S,
    step_s: float = LAG_STEP_S,
    window_s: float = COMPARE_WINDOW_S,
    rate: float = COMPARE_RATE,
    search_s: float = SEARCH_S,
) -> Alignment:
    """在相对时钟上互相关求偏移：``log.values(t)`` 对应 ``session.values(t + offset)``。

    两边的钟各有各的零点，所以"偏移"是记录仪相对 ``.ld`` 的零点差。求法分两步：

    1. 墙钟先给一个估计 ``expect_s``——两边都记了绝对时间，差的量级就是
       "记录仪先开机了多少秒"；
    2. 在它附近 ``±search_s`` 里、**只比两边墙钟上真的重叠的那一段**，用相对时钟
       上的速度互相关找精确值。

    第 2 步的窗口必须是墙钟算出来的那段重叠：``.ld`` 的 0 秒与记录仪的第一帧
    各有各的零点，偏移大的时候"从头开始比 300 s"会比到记录仪还没开始（或已经
    结束）的地方去，那是在比两条不相干的曲线。
    """
    try:
        ref_t, ref_v = _speed_of(log, SPEED_CANDIDATES)
        can_t, can_v = _speed_of(session, SPEED_CANDIDATES)
    except KeyError as exc:
        return Alignment(0.0, 0.0, 0, False, f"两边都找不到速度通道（{exc}）")

    # 墙钟重叠段 -> ``.ld`` 的秒数。``.ld`` 的 0 秒就是 ``ld_start``。
    # 再夹到记录仪**真的有帧**的那一段上：有时候墙钟重叠很长，但偏移一摆，能对上的
    # ``.ld`` 秒数只剩一小截（记录仪比 ``.ld`` 短、又被推到一头去），那一截之外
    # 拿去比就是在比一条空曲线。
    can_lo = float(can_t[0])
    can_hi = float(can_t[-1])
    if ld_start is not None and can_window is not None:
        # 墙钟交集 -> 分别换成两边的相对秒数，再换成"记录仪在读的那一段 ``.ld``"。
        begin = max(ld_start, can_window[0])
        end = min(ld_start + timedelta(seconds=float(log.duration)), can_window[1])
        ld_lo = (begin - ld_start).total_seconds()
        ld_hi = (end - ld_start).total_seconds()
        can_begin = (begin - can_window[0]).total_seconds()
        can_end = (end - can_window[0]).total_seconds()
        lo = max(ld_lo, can_begin - float(expect_s), can_lo - float(expect_s))
        hi = min(ld_hi, can_end - float(expect_s), can_hi - float(expect_s))
    else:
        lo = 0.0
        hi = float(log.duration)
    span = min(float(window_s), hi - lo)
    if span < 10.0:
        return Alignment(0.0, 0.0, 0, False, "两份数据实际重叠的时间太短，做不了互相关")
    step = 1.0 / float(rate)
    at = lo + np.arange(0.0, span, step)
    ref = hold_sample(ref_t, ref_v, at)
    usable = np.isfinite(ref)
    if int(usable.sum()) < 10:
        return Alignment(0.0, 0.0, 0, False, "这一场没有可用的速度样本")

    center = float(expect_s)
    reach = float(search_s) if search_s else float(max_lag_s)

    def score(lag: float) -> tuple[float, int]:
        other = hold_sample(can_t, can_v, at + lag)
        mask = usable & np.isfinite(other)
        count = int(mask.sum())
        if count < 10:
            return -2.0, count
        return _pearson(ref[mask], other[mask]), count

    # 粗搜（覆盖墙钟估计的整个不确定范围）之后，在峰顶附近按 step_s 精修一遍。
    best_lag, best_r, best_count = 0.0, -2.0, 0
    for lag in np.arange(center - reach, center + reach + 1e-9, float(SEARCH_STEP_S)):
        value, count = score(float(lag))
        if value > best_r:
            best_lag, best_r, best_count = float(lag), value, count
    if best_r > -2.0:
        fine = np.arange(
            best_lag - float(SEARCH_STEP_S), best_lag + float(SEARCH_STEP_S) + 1e-9,
            float(step_s),
        )
        for lag in fine:
            value, count = score(float(lag))
            if value > best_r:
                best_lag, best_r, best_count = float(lag), value, count

    # 第二判据：同一条偏移上再核一遍加速度。速度在短窗口上会撞出假的高相关
    # （实测有 r=1.0000 落在错的偏移上、只覆盖几十秒直线的例子），加速度一直有
    # 起伏，配上它才是"两条曲线真的重合"。
    accel_r = _accel_correlation(log, session, at, best_lag)
    drift = abs(best_lag - center)
    ok = bool(best_r >= MIN_CORRELATION and best_count >= 10 and drift <= float(max_lag_s))
    reason = ""
    if not ok:
        if best_r < MIN_CORRELATION:
            reason = f"互相关只有 r={best_r:.3f}（要 >= {MIN_CORRELATION}）"
        elif drift > float(max_lag_s):
            reason = f"互相关说偏移是 {best_lag:+.2f} s，跟墙钟差的 {drift:.1f} s 对不上"
    elif accel_r is not None and accel_r < MIN_ACCEL_R:
        ok = False
        reason = f"加速度复核只有 r={accel_r:.3f}（要 >= {MIN_ACCEL_R}）——速度像，别的通道不像"
    if ok and accel_r is not None:
        reason = ""
    return Alignment(best_lag, max(best_r, -1.0), best_count, ok, reason, accel_r)


# ---------------------------------------------------------------------- 合并
@dataclass
class Pairing:
    """一场 ``.ld`` 与它对应的帧表记录（含偏移与证据）。"""

    ld_path: Path
    candidate: Candidate
    alignment: Alignment
    session: object = None
    #: 因为重名（归一化之后同键）没有写进合并场的 CAN 通道名。
    skipped: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.ld_path.stem

    @property
    def ok(self) -> bool:
        return self.alignment.ok

    def as_dict(self) -> dict:
        return {
            "ld": self.name,
            "sources": [path.name for path in self.candidate.sources],
            "overlap_s": round(self.candidate.overlap_s, 1),
            **self.alignment.as_dict(),
        }


def master_axis(log) -> np.ndarray:
    """合并场次的时间轴 = ``.ld`` 自己的那条（时长 / 零点 / 采样率都是它的）。"""
    return timebase.axis(log)


def merge_columns(log, session, offset_s: float):
    """把两边的列并起来：``(time, columns, units, skipped)``。

    这是"一次记录"的便捷写法（回归与探查脚本用得多）；一个 ``.ld`` 对上不止一次
    记录时走 :func:`merge_sources`。
    """
    return merge_sources(log, [(session, offset_s)])


def merge_sources(log, sources):
    """把 ``.ld`` 与若干次记录并起来：``(time, columns, units, skipped)``。

    ``sources`` 是 ``[(对应帧表的场次, 偏移秒数), ...]``。``time`` 是 ``.ld`` 的
    主时间基（相对秒）；``.ld`` 的通道原序在前，帧表里**本场次没有的**通道接在
    后面（按名字排序，输出稳定）。

    同一个名字的两种写法（``Vx KF`` / ``Vx_KF``、``accX MTI`` / ``accX_MTI``）算
    同一条——保留 ``.ld`` 的名字，CAN 那份不写第二列，名字进 ``skipped``。注意
    **不能**用"只留字母数字"的归并：``AngVelX_body`` 与 ``AngVelX_hor`` 会撞成
    一条（见 :func:`_same_name`）。

    多次记录之间按传入顺序填：先来的先写，后来的只补空档（:func:`hold_into`）。
    """
    from . import channels as channelsmod

    axis = master_axis(log)
    columns: dict[str, np.ndarray] = {}
    units: dict[str, str] = {}
    skipped: list[str] = []

    # 先按 .ld 的通道把列摆好（顺序 = .ld 里的顺序）。空占位列要**在原值上**判，
    # 不能等重采样完再判——重采样会把"整段 0"变成"全 NaN"（或在轴外留 NaN），
    # 那时候判出来的是另一个问题。
    lds: dict[str, np.ndarray] = {}
    placeholder: set[str] = set()
    for channel in log.channels:
        values = np.asarray(log.values(channel), dtype=np.float64)
        channel_rate = channelsmod.sample_rate(log, channel)
        if channel_rate <= 0:
            channel_rate = float(log.sample_rate) or 1.0
        grid = np.arange(values.size, dtype=np.float64) / channel_rate
        units[channel.name] = str(getattr(channel, "unit", "") or "")
        lds[channel.name] = values
        if _empty(values):
            placeholder.add(channel.name)
            # 占位列整段是 0（C125 里没接上的模板通道）。它的 0 **不是数据**，
            # 所以合并场次里这一列从"空"开始，让帧表那条真的把它填满；一条都
            # 填不到时给的是 NaN（"没有数据"），而不是一排看起来像读数的 0。
            columns[channel.name] = np.full(axis.size, np.nan)
        else:
            columns[channel.name] = hold_sample(grid, values, axis)

    taken = {_key(name) for name in columns}
    # 帧表那边本场次没有的通道：按名字排序接在后面（输出稳定、可 diff）。
    seen_can: dict[str, str] = {}
    for session, _offset in sources:
        for channel in session.channels:
            if _key(channel.name) in taken or channel.name in seen_can:
                continue
            seen_can[channel.name] = str(getattr(channel, "unit", "") or "")
    for name in sorted(seen_can):
        columns[name] = np.full(axis.size, np.nan)
        units[name] = seen_can[name]

    for session, offset_s in sources:
        can_t = np.asarray(session.time, dtype=np.float64)
        for channel in session.channels:
            values = np.asarray(session.columns[channel.name], dtype=np.float64)
            unit = str(getattr(channel, "unit", "") or "")
            key = _key(channel.name)
            if key in taken:
                # 同名：对面是空的（C125 里没接上的 IVT 模板通道，整段 0）就换成真的
                # 有数的这一条；真的有数就不动它，把 CAN 的这条记进 skipped。
                existing = next(name for name in lds if _same_name(name, channel.name))
                if existing not in placeholder:
                    if channel.name not in skipped:
                        skipped.append(channel.name)
                    continue
                hold_into(can_t, values, axis + float(offset_s), columns[existing])
                if unit:
                    units[existing] = unit
                continue
            hold_into(can_t, values, axis + float(offset_s), columns[channel.name])
    return axis, columns, units, skipped


#: 合并宽表里浮点写多少位有效数字（与 ``export.FLOAT_FORMAT`` 同一档）。
FLOAT_FORMAT = "%.10g"
#: 一次写多少行：480 列 x 20000 行 = 960 万个格子，峰值几十 MB。
CHUNK_ROWS = 20_000


def write_merged(
    path: str | Path,
    time: np.ndarray,
    columns: dict[str, np.ndarray],
    *,
    units: dict[str, str] | None = None,
    index_name: str = "time_s",
    chunk_rows: int = CHUNK_ROWS,
    progress=None,
) -> dict:
    """把合并后的宽表写成 CSV（UTF-8 带 BOM，Excel 双击就能开）。

    表头第一列是 ``time_s``，第二行是单位；其余列是通道名。那是 i3pro 自己
    导出宽表的形状（``csvlog`` 读得回来，``looks_like_frames`` 不会把它当帧表）。
    缺失的格子写空字符串。返回 ``{"path", "rows", "columns", "bytes"}``。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    names = list(columns)
    units = units or {}
    total = int(time.size)
    step = max(1, int(chunk_rows))
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow([index_name, *names])
        writer.writerow([""] + [units.get(name, "") for name in names])
        for start in range(0, total, step):
            stop = min(total, start + step)
            rows: list[list[str]] = []
            for row in range(start, stop):
                cells = [FLOAT_FORMAT % float(time[row])]
                for name in names:
                    value = columns[name][row]
                    cells.append("" if not np.isfinite(value) else FLOAT_FORMAT % float(value))
                rows.append(cells)
            writer.writerows(rows)
            if progress is not None:
                progress(stop, total)
    return {
        "path": str(target),
        "rows": total,
        "columns": len(names),
        "bytes": target.stat().st_size,
    }
#: 复核用的相关系数门槛。加速度两边是同一个传感器（``.ld`` 走 CAN、帧表走
#: 原始帧），对齐之后实测真配对 >= 0.9；给 0.6 是给两边的量程/符号留余量。
MIN_ACCEL_R = 0.6
