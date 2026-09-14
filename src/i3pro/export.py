"""数据导出：把场次的原始采样按**范围 / 通道 / 采样**落成 CSV 或 Excel。

用它的地方有两处，语义必须是同一个（都从这里取数，不许各写一遍）：

* 界面上的「导出数据」面板 → ``GET /api/session/<场次>/export``（``server.py``）
* 命令行 → ``i3pro export``（``cli.py``）

三件最容易出歧义的事，这里各钉一次：

1. **范围左闭右闭。** ``from`` / ``to`` 落在样本上时那个样本要在结果里。
2. **绝对时间只在服务端解释一次。** ``.ld`` 头里有 ``log_date``（``dd/mm/yyyy``）与
   ``log_time``（``hh:mm:ss``）；``2026-09-14 12:34:56.789`` 减去它就是相对秒。
   场次头没有日期时间时**不猜**：报错并说"改用相对秒"。
3. **``rate=auto`` 不重采样。** 各通道保留自己的采样点，宽表以并集为索引、缺失留空；
   ``rate=<hz>`` 时所有列等长，方法由 ``resample`` 决定（``mean`` 只对降采样有意义，
   升采样时与 ``linear`` 等价——文档、界面、实现三处说同一句话）。
4. **主索引有三种写法。** ``time_s``（相对秒）/ ``timestamp``（场次起点 + 相对秒）/
   ``distance_m``（米）。``timestamp`` 只是时间轴的另一种**写法**、不是第三种轴，所以
   ``axis=distance`` 配它是参数错误（400 并说下一步），不是悄悄退回米。

内存：整场 445 通道 × 19.4 万点不能整份端进内存，所以宽表**分块**（每块约 400 万个
格子）；``rate=auto`` 的并集也不会无条件 ``np.unique(np.concatenate(全部通道))``
（那是 6.9 亿个 double），而是"主时间基上的点 + 不在主时间基上的点"，后者超过
100 万条就报错让人改用统一采样率。
"""

from __future__ import annotations

import json
import math
import zipfile
from dataclasses import dataclass, replace as dataclasses_replace
from datetime import datetime
from pathlib import Path

import numpy as np

from . import channels as channelsmod
from . import derive, ld as ldmod, timebase, xlsx

__all__ = [
    "RATES",
    "RESAMPLE_METHODS",
    "ExportError",
    "Request",
    "epoch_of",
    "filename",
    "metadata",
    "parse_request",
    "plan",
    "write",
]

#: 界面上那个下拉里列出来的采样率；自定义值是任意正数，不限于这张表。
RATES = (1, 5, 10, 20, 50, 100, 200, 500)
#: 统一采样率时的重采样方法。
RESAMPLE_METHODS = ("linear", "hold", "nearest", "mean")
LAYOUTS = ("wide", "long")
FORMATS = ("csv", "xlsx")
AXES = ("time", "distance")
#: 主索引列：相对秒 / 绝对时间戳 / 米。``timestamp`` 只配时间轴（见 ``parse_request``）。
INDEXES = ("time_s", "timestamp", "distance_m")

#: 宽表每块装多少个格子：19.4 万行 × 445 列整份端进内存是 690 MB，
#: 分块之后峰值只跟"一块 × 通道数"有关（约 32 MB）。
_CHUNK_CELLS = 4_000_000
#: ``rate=auto`` 时允许多少个"不在主时间基上"的采样点。
_MAX_OFF_GRID = 1_000_000
#: 判断采样点是否落在主时间基格子上的容差（取 1e-9 太紧，浮点除法会误判）。
_GRID_TOL = 1e-6
#: CSV 里浮点写成多少位有效数字。10 位已经**远超**任何传感器的真实精度，
#: 但比 ``repr`` 的 17 位少三成体积、也快一些；精确到位的原始值请用 Parquet
#: （``i3pro convert``）或者把这一行改成 ``%.17g``。
FLOAT_FORMAT = "%.10g"
#: 预估体积：每个格子 / 长表每行占多少字节。**实测标定**，不是拍的——
#: 高避5圈 4641×438 的宽表实测 CSV 3.36 B/格、xlsx 3.80 B/格、原始采样
#: （慢通道大半是空格）2.51 B/格；长表 26.67 B/行。取整数略偏保守，
#: 这样"预计文件大小"与真文件一般在一个量级内（数字见 ACCEPTANCE A45）。
_EST_BYTES_PER_CELL = 4
_EST_BYTES_PER_LONG_ROW = 28
#: 估体积时先真写这么多行，再按比例外推。200 行足够让"每行多少字节"稳定下来，
#: 又不至于为了一个预估把整份数据算两遍。
_EST_SAMPLE_ROWS = 200


class ExportError(ValueError):
    """导出参数不对。消息给队友看：说清哪里不对 + 下一步怎么改。"""


@dataclass(frozen=True)
class Request:
    """一次导出请求（参数解析完的结果，后续只认它）。"""

    channels: tuple[str, ...]
    axis: str
    start: float
    end: float
    rate: float | None          # None = auto（原始采样）
    resample: str
    layout: str
    fmt: str
    bundle: bool
    metadata: bool
    range_label: str
    channel_source: str
    maths: bool
    #: 主索引列名，也是它的写法：``time_s`` / ``timestamp`` / ``distance_m``。
    index: str = "time_s"
    #: 内部用的**时间**窗口（秒）：距离轴上它就是"这段距离对应的那段时刻"，
    #: 由 ``_time_window`` 从距离序列上定位（取首次到达，和 ``/at`` 一个语义）。
    t_start: float = 0.0
    t_end: float = 0.0

    def rate_label(self) -> str:
        return "auto" if self.rate is None else f"{self.rate:g}Hz"

    def axis_unit(self) -> str:
        return "s" if self.axis == "time" else "m"

    def index_name(self) -> str:
        return self.index


# ------------------------------------------------------------------ 参数解析
def _text(params: dict, key: str):
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _flag(params: dict, key: str, default: bool) -> bool:
    text = _text(params, key)
    if text is None:
        return default
    return text.lower() not in ("0", "false", "no", "off")


def _number(params: dict, key: str, label: str, default=None):
    text = _text(params, key)
    if text is None:
        return default
    if text.lower() == "auto":
        return None
    try:
        return float(text)
    except ValueError:
        raise ExportError(
            f"{label}要一个数字，收到 {text!r}。时间用秒（例如 12.5），距离用米（例如 1200）。"
        ) from None


def epoch_of(log: ldmod.LogFile) -> float | None:
    """场次起点在本地时钟上的时刻（epoch 秒）；头里没有日期时间就返回 ``None``。

    MoTeC 写的是本地时间、只到秒，所以绝对时间的小数位是"我们补的零点几秒"，
    真实精度只有一秒——这条要写在验收条目里，别当成微秒级对齐。
    """
    date = (getattr(log, "log_date", "") or "").strip()
    clock = (getattr(log, "log_time", "") or "").strip()
    if not date or not clock:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(f"{date} {clock}", fmt).timestamp()
        except ValueError:
            continue
    return None


def _moment(text: str, epoch: float | None, label: str) -> float:
    """把 ``12.5`` / ``2026-09-14 12:34:56.789`` / 裸时钟 ``12:35:10.123`` 变成相对秒。

    裸时钟（只有时分秒）的**日期沿用场次那一天**——需求里"从 12:34:56.789 导到
    12:35:10.123"就是这么写的：第二个端点几乎不可能跨天，重打一遍日期没有意义。
    日期直接从 ``epoch`` 还原（它就是 ``log_date + log_time`` 的本地时刻），所以
    这条分支不需要额外的参数。
    """
    stripped = text.strip()
    try:
        return float(stripped)
    except ValueError:
        pass
    if epoch is None:
        raise ExportError(
            f"这个场次没有记录日期时间（.ld 头里没有），所以 {label} 不能用绝对时间。"
            f"改用相对秒，例如 {label}=12.5。"
        )
    candidate = stripped.replace("T", " ").strip()
    full = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")
    for fmt in full:
        try:
            return datetime.strptime(candidate, fmt).timestamp() - epoch
        except ValueError:
            continue
    # 裸时钟：有冒号、没有日期分隔符。
    if ":" in candidate and not any(sep in candidate for sep in ("-", "/")):
        day = datetime.fromtimestamp(epoch).strftime("%d/%m/%Y")
        for fmt in ("%d/%m/%Y %H:%M:%S.%f", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
            try:
                return datetime.strptime(f"{day} {candidate}", fmt).timestamp() - epoch
            except ValueError:
                continue
    raise ExportError(
        f"{label} 认不出这个时间：{text!r}。写成 2026-09-14 12:34:56.789、只写时分秒的"
        f"12:35:10.123（日期沿用本场次那天），或者直接用相对秒 12.5。"
    )


def parse_request(log: ldmod.LogFile, params: dict) -> Request:
    """把查询参数（字符串）或 CLI/测试给的字典，解析成一个 ``Request``。"""
    axis = (_text(params, "axis") or "time").lower()
    if axis not in AXES:
        raise ExportError(f"axis 只能是 time（相对秒）或 distance（米），收到 {axis!r}。")

    # 主索引列：默认跟着 axis 走；``timestamp`` 是"同一段时间轴、换一种写法"。
    index = (_text(params, "index") or "").lower()
    if not index:
        index = "distance_m" if axis == "distance" else "time_s"
    if index not in INDEXES:
        raise ExportError(
            f"index 只能是 {' / '.join(INDEXES)}，收到 {index!r}。"
            f"（time_s 相对秒 / timestamp 绝对时间戳 / distance_m 米）"
        )
    if axis == "distance" and index != "distance_m":
        raise ExportError(
            f"距离轴的主索引只能是 distance_m，收到 {index!r}。"
            f"想把时间列写成绝对时间戳就写 axis=time&index=timestamp。"
        )
    if axis == "time" and index == "distance_m":
        raise ExportError(
            "时间轴的主索引写 time_s（相对秒）或 timestamp（绝对时间戳）；"
            "要米就去写 axis=distance。"
        )
    if index == "timestamp" and epoch_of(log) is None:
        raise ExportError(
            "这个场次没有记录日期时间（.ld 头里没有），写不出绝对时间戳。"
            "把主索引换成 time_s（相对秒），或者改用距离轴导出。"
        )

    fmt = (_text(params, "format") or _text(params, "fmt") or "csv").lower()
    if fmt not in FORMATS:
        raise ExportError(f"format 只能是 csv 或 xlsx，收到 {fmt!r}。")

    layout = (_text(params, "layout") or "wide").lower()
    if layout not in LAYOUTS:
        raise ExportError(f"layout 只能是 wide（宽表）或 long（长表），收到 {layout!r}。")

    resample = (_text(params, "resample") or "linear").lower()
    if resample not in RESAMPLE_METHODS:
        raise ExportError(
            f"resample 只能是 {' / '.join(RESAMPLE_METHODS)}，收到 {resample!r}。"
            f"（linear 线性插值 / hold 前值保持 / nearest 最近邻 / mean 区间均值）"
        )

    maths = _flag(params, "maths", True)
    channels, channel_source = _parse_channels(log, params, maths)
    if not channels:
        raise ExportError(
            "这次导出一条通道都没有：把 channels 换成 all，或者用 names= 至少给一条通道名。"
        )

    rate = _number(params, "rate", "rate（采样率）", None)
    if rate is not None and (not math.isfinite(rate) or rate <= 0):
        raise ExportError(f"rate 要是正数（Hz），收到 {rate!r}；想保留原始采样就写 rate=auto。")

    total = float(log.duration)
    if axis == "distance":
        total = _max_distance(log)
        if total <= 0:
            raise ExportError(
                "这个场次没有可用的距离轴（距离序列算不出来或全为 0），"
                "把 axis 换成 time 再导出。"
            )

    absolute = _flag(params, "absolute", False)
    raw_from, raw_to = _text(params, "from"), _text(params, "to")
    if absolute:
        if raw_from is None or raw_to is None:
            raise ExportError(
                "绝对时间要同时给 from 与 to（例如 from=2026-09-14 12:34:56.789&to=…）。"
            )
        epoch = epoch_of(log)
        start = _moment(raw_from, epoch, "from")
        end = _moment(raw_to, epoch, "to")
        label = f"绝对时间 {raw_from} – {raw_to}"
    else:
        start = _number(params, "from", "from（起点）", 0.0)
        end = _number(params, "to", "to（终点）", total)
        label = f"{start:g} – {end:g} " + ("s" if axis == "time" else "m")
    if not math.isfinite(start) or not math.isfinite(end):
        raise ExportError("from/to 要是有限数字。")
    if start > end:
        raise ExportError(
            f"起始 {start:g} 大于结束 {end:g}：把 from 调小或者把 to 调大。"
        )
    if end < 0 or start > total:
        unit = "秒" if axis == "time" else "米"
        raise ExportError(
            f"这个范围落在场次之外：本场是 0 – {total:.3f} {unit}，"
            f"而你要的是 {start:g} – {end:g}。把 from/to 收进这个区间。"
        )
    if end > total + 1e-3:
        unit = "秒" if axis == "time" else "米"
        raise ExportError(
            f"结束点 {end:g} 超出场次长度：本场是 0 – {total:.3f} {unit}。"
            f"把 to 调到 {total:.3f} 以内。"
        )
    start, end = max(0.0, start), min(total, end)

    request = Request(
        channels=tuple(channels),
        axis=axis,
        start=float(start),
        end=float(end),
        rate=rate,
        resample=resample,
        layout=layout,
        fmt=fmt,
        bundle=_flag(params, "bundle", False),
        metadata=_flag(params, "metadata", True),
        range_label=label,
        channel_source=channel_source,
        maths=maths,
        index=index,
    )
    t_start, t_end = _time_window(log, request)
    return dataclasses_replace(request, t_start=t_start, t_end=t_end)


def _time_window(log: ldmod.LogFile, req: Request) -> tuple[float, float]:
    """把请求的范围换算成**秒**。

    距离轴上 ``from``/``to`` 是米，得先在距离序列上定位到时刻——用 ``searchsorted``
    的"首次到达"语义（车停着不动时同一个距离会持续很久，答案是**第一次**到那个距离
    的时刻，和 ``/api/session/<名>/at`` 一致）。
    """
    if req.axis == "time":
        return req.start, req.end
    try:
        distance = np.asarray(derive.distance_series(log), dtype=np.float64)
    except ValueError:
        return 0.0, 0.0
    times = timebase.axis(log)
    size = min(times.size, distance.size)
    if size == 0:
        return 0.0, 0.0
    spread = np.maximum.accumulate(distance[:size])
    first = min(int(np.searchsorted(spread, req.start, side="left")), size - 1)
    # 终点取"最后一个距离 ≤ to 的采样点"：左闭右闭在距离轴上就是"不超过 to"。
    last = max(first, min(int(np.searchsorted(spread, req.end, side="right")) - 1, size - 1))
    return float(times[first]), float(times[last])


def _parse_channels(log: ldmod.LogFile, params: dict, maths: bool):
    """通道来源：``all`` 或 ``selected`` + ``names``；数学通道单独一个开关。"""
    source = (_text(params, "channels") or "all").lower()
    if source not in ("all", "selected"):
        raise ExportError(
            f"channels 只能是 all（全部）或 selected（用 names= 指定），收到 {source!r}。"
        )
    available = [ch.name for ch in log.channels]
    derived = set(channelsmod.names(log))
    if source == "all":
        return [n for n in available if maths or n not in derived], "全部通道"
    raw = _text(params, "names") or ""
    wanted = [n.strip() for n in raw.split(",") if n.strip()]
    if not wanted:
        raise ExportError(
            "channels=selected 时要给 names=<逗号分隔的通道名>，"
            "例如 names=Vx KF,G Force Long。"
        )
    missing = [n for n in wanted if not log.has(n)]
    if missing:
        hint = available[0] if available else "（本场次没有通道）"
        raise ExportError(
            f"本场次没有 {missing[0]!r} 这条通道，所以没法导出。"
            f"通道名要跟 /api/session/<名>/info 里的一致（例如 {hint!r}），"
            f"逗号分隔、名字里的空格要照写。"
        )
    if not maths:
        skipped = [n for n in wanted if n in derived]
        wanted = [n for n in wanted if n not in derived]
        if not wanted:
            raise ExportError(
                f"names 里只有数学通道（{'、'.join(skipped)}），而 maths=0 关掉了它们。"
                f"把 maths 换成 1，或者换成原生通道。"
            )
    return wanted, f"手动勾选 {len(wanted)} 条"


# ------------------------------------------------------------------ 取数
def _series(log: ldmod.LogFile, name: str) -> tuple[np.ndarray, np.ndarray]:
    """一条通道的 ``(时间, 数值)``。数学通道在主时间基上，原生通道按自己那一档。"""
    ch = log.channel(name)
    values = np.asarray(log.values(ch), dtype=np.float64)
    rate = channelsmod.sample_rate(log, ch)
    return np.arange(values.size, dtype=np.float64) / rate, values


def _channel_count(log: ldmod.LogFile, name: str) -> int:
    ch = log.channel(name)
    if channelsmod.is_derived(log, ch):
        return int(np.asarray(log.values(ch)).size)
    return int(ch.sample_count)


def _max_distance(log: ldmod.LogFile) -> float:
    try:
        distance = np.asarray(derive.distance_series(log), dtype=np.float64)
    except ValueError:
        return 0.0
    finite = distance[np.isfinite(distance)]
    return float(finite[-1]) if finite.size else 0.0


def _index_times(log: ldmod.LogFile, req: Request) -> np.ndarray:
    """主索引上的时刻（秒）。范围**左闭右闭**。"""
    start, end = req.t_start, req.t_end
    if end < start:
        return np.empty(0, dtype=np.float64)
    if req.rate is None:
        base = timebase.axis(log)
        inside = base[(base >= start - _GRID_TOL) & (base <= end + _GRID_TOL)]
        master = float(log.sample_rate) or 1.0
        extras: list[np.ndarray] = []
        extra_count = 0
        for name in req.channels:
            rate = channelsmod.sample_rate(log, log.channel(name))
            count = _channel_count(log, name)
            if count <= 0:
                continue
            first = max(0, int(math.ceil(start * rate - _GRID_TOL)))
            last = min(count - 1, int(math.floor(end * rate + _GRID_TOL)))
            if last < first:
                continue
            times = np.arange(first, last + 1, dtype=np.float64) / rate
            off = np.abs(times * master - np.round(times * master)) > _GRID_TOL
            if off.any():
                extra = times[off]
                extra_count += int(extra.size)
                if extra_count > _MAX_OFF_GRID:
                    raise ExportError(
                        f"这些通道的采样点跟主时间基（{master:g} Hz）对不齐的太多了"
                        f"（超过 {_MAX_OFF_GRID:,} 个），并集索引会撑爆内存。"
                        f"改用统一采样率导出，例如 rate={master:g}。"
                    )
                extras.append(extra)
        if extras:
            return np.union1d(inside, np.concatenate(extras))
        return inside
    span = end - start
    count = int(math.floor(span * req.rate + _GRID_TOL)) + 1
    times = start + np.arange(count, dtype=np.float64) / float(req.rate)
    # **左闭右闭**：终点不落在格点上时要补上终点本身。整场上 10 Hz 导出 463.99 s
    # 的场次，格点是 0.0 … 463.9——只到 463.9 的话"包含起止点"就只做到了左边。
    # 代价是最后一段短一格（0.09 s 而不是 0.1 s），这一点写在元数据的 range 里。
    if times.size == 0 or times[-1] < end - _GRID_TOL:
        times = np.append(times, float(end))
    else:
        times = np.minimum(times, float(end))
    return times


def _resampled(log: ldmod.LogFile, req: Request, name: str, times: np.ndarray) -> np.ndarray:
    """把一条通道重采样到 ``times``；``rate=auto`` 时只取落在格子上的点，其余留空。"""
    ctimes, values = _series(log, name)
    if ctimes.size == 0:
        return np.full(times.size, np.nan)
    rate = channelsmod.sample_rate(log, log.channel(name))
    if req.rate is None:
        slots = times * rate
        rounded = np.round(slots)
        hit = np.abs(slots - rounded) <= _GRID_TOL
        out = np.full(times.size, np.nan)
        index = rounded.astype(np.int64)
        valid = hit & (index >= 0) & (index < values.size)
        out[valid] = values[index[valid]]
        return out
    inside = (times >= ctimes[0] - _GRID_TOL) & (times <= ctimes[-1] + _GRID_TOL)
    if req.resample == "linear" or (req.resample == "mean" and req.rate >= rate):
        # 升采样时 mean 与 linear 等价（契约里就是这么写的，别让它有两种读法）。
        return np.where(inside, np.interp(times, ctimes, values), np.nan)
    if req.resample == "hold":
        index = np.clip(np.searchsorted(ctimes, times, side="right") - 1, 0, values.size - 1)
        return np.where(inside, values[index], np.nan)
    if req.resample == "nearest":
        index = np.clip(np.searchsorted(ctimes, times), 0, values.size - 1)
        left = np.clip(index - 1, 0, values.size - 1)
        picked = np.where(np.abs(times - ctimes[left]) <= np.abs(ctimes[index] - times), left, index)
        return np.where(inside, values[picked], np.nan)
    # mean：一个目标点覆盖的原始样本取平均（只对降采样有意义）
    step = 1.0 / float(req.rate)
    edges = np.concatenate([[times[0] - step / 2], times + step / 2])
    lo = np.searchsorted(ctimes, edges[:-1], side="left")
    hi = np.searchsorted(ctimes, edges[1:], side="left")
    finite = np.isfinite(values)
    sums = np.concatenate([[0.0], np.cumsum(np.where(finite, values, 0.0))])
    counts = np.concatenate([[0], np.cumsum(finite.astype(np.int64))])
    number = (counts[hi] - counts[lo]).astype(np.float64)
    total = (sums[hi] - sums[lo]).astype(np.float64)
    out = np.where(number > 0, total / np.maximum(number, 1.0), np.nan)
    return np.where(inside & (hi > lo), out, np.nan)


def _index_values(log: ldmod.LogFile, req: Request, times: np.ndarray) -> np.ndarray:
    """主索引那一列：时间轴上就是秒；距离轴上把每个时刻换算成米。"""
    if req.axis == "time":
        return times
    try:
        distance = np.asarray(derive.distance_series(log), dtype=np.float64)
    except ValueError:
        return np.full(times.size, np.nan)
    base = timebase.axis(log)
    size = min(distance.size, base.size)
    return np.interp(times, base[:size], distance[:size])


def _stamp_text(epoch: float, seconds: float) -> str:
    """一个时刻的绝对时间戳文本（毫秒三位）。"""
    moment = datetime.fromtimestamp(epoch + float(seconds))
    return f"{moment:%Y-%m-%d %H:%M:%S}.{moment.microsecond // 1000:03d}"


def _stamp_texts(log: ldmod.LogFile, req: Request, seconds) -> list[str]:
    """``index=timestamp`` 那一列：**场次起点 + 相对秒**。

    起点来自 ``.ld`` 头里的日期时间，MoTeC 只写到秒，所以这一列的绝对精度是
    "起点精确到秒、相对部分精确到毫秒"——别把它当成微秒级同步时钟。这条同时写进
    导出的元数据（``timestamp.source_precision``）。
    """
    epoch = epoch_of(log)
    if epoch is None:
        raise ExportError(
            "这个场次没有记录日期时间（.ld 头里没有），写不出绝对时间戳。"
            "把 index 换成 time_s（相对秒），或者改用 axis=distance 导出距离。"
        )
    return [_stamp_text(epoch, value) for value in np.asarray(seconds, dtype=np.float64)]


def _chunk_size(channel_count: int) -> int:
    return max(1, min(20_000, _CHUNK_CELLS // max(1, channel_count)))


def _wide_chunks(log: ldmod.LogFile, req: Request, index: np.ndarray):
    """宽表按块产出 ``(行数, 矩阵)``，矩阵第一列是主索引、其余是通道。

    分块是为了内存：整份 19.4 万 × 445 的矩阵是 690 MB，一块只有约 32 MB。
    """
    lead = _index_values(log, req, index)
    step = _chunk_size(len(req.channels))
    for start in range(0, index.size, step):
        stop = min(index.size, start + step)
        block = index[start:stop]
        matrix = np.empty((stop - start, len(req.channels) + 1), dtype=np.float64)
        matrix[:, 0] = lead[start:stop]
        for column, name in enumerate(req.channels, start=1):
            matrix[:, column] = _resampled(log, req, name, block)
        yield matrix


def _wide_row_lists(log: ldmod.LogFile, req: Request, index: np.ndarray):
    """一行 = 一个索引点；缺失值是 ``nan``，由写文件的那一端变成空。"""
    for matrix in _wide_chunks(log, req, index):
        if req.index == "timestamp":
            texts = _stamp_texts(log, req, matrix[:, 0])
            for text, row in zip(texts, matrix[:, 1:].tolist()):
                yield [text] + row
            continue
        # ``tolist()`` 在 C 层做，比逐格取 Python 对象快得多——
        # 343 列 × 19.4 万行是 6700 万格，逐格转换要几分钟。
        yield from matrix.tolist()


def _long_rows(log: ldmod.LogFile, req: Request, index: np.ndarray):
    """长表：``索引, channel, value, unit``，**只写有值的格子**。

    同一条通道内按时间递增（通道极多、采样率各不相同时用它）。
    """
    lead = _index_values(log, req, index)
    texts = _stamp_texts(log, req, index) if req.index == "timestamp" else None
    for name in req.channels:
        column = _resampled(log, req, name, index)
        unit = channelsmod.unit(log, log.channel(name))
        for i in range(index.size):
            value = column[i]
            if not np.isfinite(value):
                continue
            yield [texts[i] if texts is not None else lead[i], name, float(value), unit]


def _rows(log: ldmod.LogFile, req: Request, index: np.ndarray):
    if req.layout == "long":
        return _long_rows(log, req, index)
    return _wide_row_lists(log, req, index)


def _long_frames(log: ldmod.LogFile, req: Request, index: np.ndarray):
    """长表按通道产出 DataFrame（pandas 在 C 层格式化，别逐格写 Python）。"""
    import pandas as pd

    lead = _index_values(log, req, index)
    name_column = req.index_name()
    for name in req.channels:
        column = _resampled(log, req, name, index)
        keep = np.isfinite(column)
        if not keep.any():
            continue
        unit = channelsmod.unit(log, log.channel(name))
        first_column = (
            _stamp_texts(log, req, index[keep]) if req.index == "timestamp" else lead[keep]
        )
        yield pd.DataFrame(
            {
                name_column: first_column,
                "channel": name,
                "value": column[keep],
                "unit": unit,
            }
        )


def _blank(value):
    """宽表里的缺失：CSV 要空字符串，Excel 要 ``None``。"""
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, np.floating) and not math.isfinite(float(value)):
        return None
    return value


def _header(log: ldmod.LogFile, req: Request) -> list[str]:
    if req.layout == "long":
        return [req.index_name(), "channel", "value", "unit"]
    names = [req.index_name()]
    for name in req.channels:
        unit = channelsmod.unit(log, log.channel(name))
        names.append(f"{name} [{unit}]" if unit else name)
    return names


# ------------------------------------------------------------------ 预估与元数据
def _row_count(log: ldmod.LogFile, req: Request, index: np.ndarray) -> int:
    """实际会写出多少数据行（不含表头）——``plan`` 与真导出必须一致。"""
    if req.layout == "wide":
        return int(index.size)
    total = 0
    for name in req.channels:
        total += int(np.isfinite(_resampled(log, req, name, index)).sum())
    return total


def plan(log: ldmod.LogFile, req: Request) -> dict:
    """预计行数 / 列数 / 体积 / 分几张表——**不产生文件**。"""
    index = _index_times(log, req)
    rows = _row_count(log, req, index)
    if rows == 0:
        raise ExportError(
            "当前范围无数据：这个窗口里一条样本都没有。把范围放宽一点，"
            "或者换成「全部日志」再导出。"
        )
    columns = len(_header(log, req))
    bytes_est = _estimate_bytes(log, req, index, rows)
    sheets = 0
    if req.fmt == "xlsx":
        sheets = max(1, math.ceil((rows + 1) / xlsx.MAX_ROWS))
    if req.bundle:
        bytes_est += 1024
    warnings: list[str] = []
    if req.fmt == "xlsx":
        warnings.append(
            "Excel 是压缩包，实际文件通常明显小于这里的预估（预估算的是同等内容的文本体积）。"
        )
    master = float(log.sample_rate) or 1.0
    if req.rate is None and req.layout == "wide" and len(req.channels) > 1:
        slow = [
            n for n in req.channels
            if channelsmod.sample_rate(log, log.channel(n)) < master
        ]
        if slow:
            warnings.append(
                f"原始采样模式下，比主时间基慢的 {len(slow)} 条通道大部分行是空的；"
                f"要紧凑就选统一采样率。"
            )
    return {
        "rows": int(rows),
        "columns": int(columns),
        "bytes": int(bytes_est),
        "sheets": int(sheets),
        "axis": req.axis,
        "index": req.index,
        "warnings": warnings,
    }


def metadata(log: ldmod.LogFile, req: Request, rows: int, columns: int) -> dict:
    """CSV 的 ``metadata.json`` 与 Excel 的「元数据」sheet 用的是同一份内容。"""
    info = {
        "file": log.path.name,
        "session": log.path.stem,
        "range": {
            "label": req.range_label,
            "from": req.start,
            "to": req.end,
            "unit": req.axis_unit(),
            "bounds": "左闭右闭",
        },
        "axis": req.axis,
        "index": req.index,
        "rate": "auto" if req.rate is None else req.rate,
        "resample": req.resample,
        "layout": req.layout,
        "format": req.fmt,
        "float_format": FLOAT_FORMAT if req.fmt == "csv" else "excel 原生 double",
        "rows": int(rows),
        "columns": int(columns),
        "channel_source": req.channel_source,
        "channels": [
            {
                "name": name,
                "unit": channelsmod.unit(log, log.channel(name)),
                "sample_rate": channelsmod.sample_rate(log, log.channel(name)),
                "derived": bool(channelsmod.is_derived(log, log.channel(name))),
            }
            for name in req.channels
        ],
        "source_meta": log.metadata(),
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if req.index == "timestamp":
        # 这一列的精度必须写清楚：起点是 .ld 头里的本地时间、只到秒，
        # 相对部分是采样时刻，到毫秒。别让队友以为它是微秒级同步时钟。
        info["timestamp"] = {
            "column": "timestamp",
            "format": "%Y-%m-%d %H:%M:%S.SSS",
            "timezone": "本机时区（.ld 头里存的就是本地时间）",
            "epoch": epoch_of(log),
            "source_precision": "起点精确到秒，相对部分精确到毫秒",
        }
    return info


def _metadata_rows(meta: dict):
    """把元数据摊成两列，给 Excel 的「元数据」sheet 用。"""
    yield ["项", "值"]
    yield ["日志文件", meta["file"]]
    yield ["场次", meta["session"]]
    yield ["导出范围", meta["range"]["label"] + f"（{meta['range']['bounds']}）"]
    yield ["主索引", meta["axis"] + " · " + meta.get("index", "")]
    if meta.get("timestamp"):
        yield ["时间戳列", "场次起点 + 相对秒（" + meta["timestamp"]["source_precision"] + "）"]
    yield ["采样率", meta["rate"]]
    yield ["重采样", meta["resample"]]
    yield ["形状", f"{meta['rows']} 行 × {meta['columns']} 列（{meta['layout']}）"]
    yield ["通道来源", meta["channel_source"]]
    yield ["通道数", len(meta["channels"])]
    for channel in meta["channels"]:
        unit = channel["unit"] or "（无单位）"
        kind = "数学通道" if channel["derived"] else "原生通道"
        yield [channel["name"], f"{unit} · {channel['sample_rate']:g} Hz · {kind}"]
    yield ["导出时间", meta["exported_at"]]


def filename(log: ldmod.LogFile, req: Request) -> str:
    """下载时那个文件名（中文保留，路径分隔符与冒号换成安全字符）。"""
    label = req.range_label.replace(":", "：").replace("/", "-").replace("\\", "-")
    mark = "-绝对时间" if req.index == "timestamp" else ""
    name = f"{log.path.stem}-{label}-{req.rate_label()}{mark}-{req.layout}.{req.fmt}"
    if req.bundle and req.fmt == "csv":
        name += ".zip"
    return name


# ------------------------------------------------------------------ 写文件
def _empty_range() -> ExportError:
    return ExportError(
        "当前范围无数据：这个窗口里一条样本都没有。把范围放宽一点，"
        "或者换成「全部日志」再导出。"
    )


def _estimate_bytes(log: ldmod.LogFile, req: Request, index: np.ndarray, rows: int) -> int:
    """按**真的行内容**估体积：先按同一套格式写前 200 行，再按行数比例外推。

    原先用「行数 × 列数 × 每格 4 字节」：原始采样模式下大部分格子是空的（比主时间基慢的
    通道只在少数行上有值），于是高避5圈那个 46400 行 × 438 列的窗口被估成 **1755754 B**，
    实际文件是 **949409 B**——面板上显示的数字比真实大了一倍，用户会以为导不出来。
    """
    if rows <= 0 or index.size == 0:
        return 0
    import io

    import pandas as pd

    sample = index[:_EST_SAMPLE_ROWS]
    header = _header(log, req)
    buffer = io.StringIO()
    sampled_rows = 0
    if req.layout == "wide":
        for matrix in _wide_chunks(log, req, sample):
            frame = pd.DataFrame(matrix, columns=header)
            if req.index == "timestamp":
                frame[header[0]] = _stamp_texts(log, req, matrix[:, 0])
            frame.to_csv(
                buffer, header=sampled_rows == 0, index=False,
                na_rep="", float_format=FLOAT_FORMAT,
            )
            sampled_rows += int(matrix.shape[0])
    else:
        for frame in _long_frames(log, req, sample):
            frame.to_csv(
                buffer, header=sampled_rows == 0, index=False,
                na_rep="", float_format=FLOAT_FORMAT,
            )
            sampled_rows += int(frame.shape[0])
    if sampled_rows == 0:
        return 0
    measured = len(buffer.getvalue().encode("utf-8"))
    return int(3 + measured * (rows / sampled_rows))       # + BOM


def _write_csv(log: ldmod.LogFile, req: Request, path: Path, progress) -> dict:
    import pandas as pd

    index = _index_times(log, req)
    if index.size == 0:
        raise _empty_range()
    header = _header(log, req)
    data_path = path.with_suffix(".data.csv") if req.bundle else path
    rows = 0
    # UTF-8 带 BOM：通道名是中文，Excel 双击打开要靠这个才不乱码。
    with data_path.open("w", encoding="utf-8-sig", newline="") as fh:
        first = True
        if req.layout == "wide":
            for matrix in _wide_chunks(log, req, index):
                frame = pd.DataFrame(matrix, columns=header)
                if req.index == "timestamp":
                    frame[header[0]] = _stamp_texts(log, req, matrix[:, 0])
                frame.to_csv(
                    fh, header=first, index=False, na_rep="", float_format=FLOAT_FORMAT
                )
                first = False
                rows += int(matrix.shape[0])
                if progress is not None:
                    progress(rows, index.size)
        else:
            for frame in _long_frames(log, req, index):
                frame.to_csv(
                    fh, header=first, index=False, na_rep="", float_format=FLOAT_FORMAT
                )
                first = False
                rows += int(len(frame))
                if progress is not None:
                    progress(rows, None)
        if first:  # 长表里一条有限值都没有：写个只有表头的文件，不要给空文件
            fh.write(",".join(header) + "\n")
    if req.bundle:
        meta = metadata(log, req, rows, len(header))
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(data_path, arcname=f"{log.path.stem}.csv")
            zf.writestr("metadata.json", json.dumps(meta, ensure_ascii=False, indent=2))
        data_path.unlink(missing_ok=True)
    if progress is not None:
        progress(rows, index.size)
    return {"rows": rows, "columns": len(header), "sheets": 0}


def _write_xlsx(log: ldmod.LogFile, req: Request, path: Path, progress) -> dict:
    index = _index_times(log, req)
    if index.size == 0:
        raise _empty_range()
    header = _header(log, req)
    sheets = []
    meta_rows = 0
    if req.metadata:
        lines = list(_metadata_rows(metadata(log, req, index.size, len(header))))
        meta_rows = len(lines) - 1
        sheets.append(
            {"name": "元数据", "header": lines[0], "rows": iter(lines[1:]), "split": False}
        )

    def data_rows():
        for row in _rows(log, req, index):
            yield [_blank(value) for value in row]

    sheets.append({"name": "数据", "header": header, "rows": data_rows(), "split": True})
    result = xlsx.write_workbook(path, sheets, progress=progress)
    return {
        "rows": result["rows"] - meta_rows,
        "columns": len(header),
        "sheets": result["sheets"],
        "names": result["names"],
    }


def write(log: ldmod.LogFile, req: Request, path: str | Path, progress=None) -> dict:
    """把请求写成文件；返回 ``{"rows", "columns", "sheets", "bytes", "path"}``。

    ``progress(done, total)`` 会被反复调用（``total`` 未知时给 ``None``），
    界面据此画进度条。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if req.fmt == "csv":
        stats = _write_csv(log, req, path, progress)
    else:
        stats = _write_xlsx(log, req, path, progress)
    stats["path"] = str(path)
    stats["bytes"] = path.stat().st_size if path.exists() else 0
    return stats
