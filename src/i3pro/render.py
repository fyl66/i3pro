"""Build the payload for the browser workbench and render it as one HTML file.

The workbench runs in two modes from the *same* template:

``static``
    ``render_html()`` embeds the traces for a fixed set of channels, producing a
    single self-contained file (no server, no CDN) that can be mailed around and
    double-clicked at the track.

``serve``
    ``build_payload(..., api_base="/api")`` embeds only metadata and the full
    channel index; the browser then pulls traces on demand, so every one of the
    342-437 channels is searchable and the view is a shareable URL.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import derive, gpsfix, histogram as histogrammod, laps as lapsmod
from . import notes as notesmod, report as reportmod, sections as sectionsmod
from . import ld as ldmod
from . import spectrum as spectrummod

__all__ = [
    "render_html",
    "render_page",
    "build_payload",
    "build_overlay",
    "downsample",
    "channel_index",
    "trace",
    "points",
    "histogram",
    "spectrum",
    "groups",
    "pick_channels",
    "track_payload",
    "notes_payload",
    "sections_payload",
    "report_payload",
    "snapshot_report",
    "snapshot_histograms",
    "snapshot_spectra",
]

TEMPLATE = Path(__file__).with_name("web") / "viewer.html"

#: Pixel columns per channel in a static snapshot. Higher = smoother when you
#: zoom in, at the cost of file size (2000 ≈ 1.2 MB for a 12 channel payload).
DEFAULT_BUCKETS = 2000

#: Channels a driver/vehicle-dynamics engineer opens first, best first.
DEFAULT_CHANNEL_PRIORITY = (
    "Vx KF",
    "Ground Speed",
    "GPS Speed",
    "SpeedFL",
    "SpeedFR",
    "SpeedRL",
    "SpeedRR",
    "Brake Signal",
    "TH",
    "Throttle",
    "Brake Pressure",
    "G Force Lat",
    "G Force Long",
    "G Force Vert",
    "Steering Angle",
    "SW Angle",
    "SteerAngle",
    "AMKFR ActualTorqueValue",
    "Battery Power",
    "MCU1 FR TempMotor",
    "GPS Heading",
    "AngleSlip",
)

#: Channels the distance-axis overlay compares by default.
OVERLAY_PRIORITY = (
    "Vx KF",
    "Ground Speed",
    "SpeedFR",
    "Brake Signal",
    "TH",
    "G Force Long",
    "AMKFR ActualTorqueValue",
)

SPEED_FOR_COLORING = ("Vx KF", "Ground Speed", "GPS Speed", "SpeedFR", "SpeedFL")

#: MoTeC-style status/error channels: binary or state flags drawn in a band
#: under the graph rather than as traces (i2 Pro's "Status and Errors" panel).
STATUS_HINTS = (
    "error", "warning", "warn", "status", "flag", "fault", "ready", "derating",
    "inverteron", "dcon", "enable", "quit", "systemready", "valid", "clipping",
    "selftest", "switch", "sign ",
)

#: Order the unit groups the way an engineer reads them, best first.
GROUP_UNIT_ORDER = (
    "km/h", "m/s", "rpm", "G", "deg", "deg/s", "deg/s/s", "%", "kW", "Nm", "NM",
    "V", "mV", "A", "mA", "C", "mm", "bar", "kPa", "psi", "MPa", "m/s/s", "m",
    "s", "us", "ms", "l", "Pa", "y", "h", "min",
)

GROUP_LABELS = {
    "km/h": "速度", "m/s": "速度", "rpm": "转速", "G": "加速度", "deg": "角度",
    "deg/s": "角速度", "deg/s/s": "角加速度", "%": "百分比", "kW": "功率",
    "Nm": "扭矩", "NM": "扭矩", "V": "电压", "mV": "电压", "A": "电流",
    "mA": "电流", "C": "温度", "mm": "位移", "bar": "压力", "kPa": "压力",
    "psi": "压力", "MPa": "压力", "m/s/s": "加速度", "m": "距离", "s": "时间",
    "us": "时间", "ms": "时间", "l": "燃油", "Pa": "压力",
}


def _finite(values: np.ndarray) -> np.ndarray:
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


def is_status_channel(ch) -> bool:
    """Cheap name-based guess, used to sort channels into the status band."""
    lowered = ch.name.lower()
    if not any(hint in lowered for hint in STATUS_HINTS):
        return False
    # a real measurement that merely mentions "switch" (e.g. "Switch voltage")
    # is not a status channel; MoTeC status channels are unitless.
    return not ch.unit


def groups(log: ldmod.LogFile) -> tuple[list[dict], list[str]]:
    """Split channels into unit groups, exactly the way i2 Pro groups them.

    Channels in a group share one y-axis, because comparing a wheel speed to a
    GPS speed only makes sense on a common scale. Returns the groups plus the
    names of the status/error channels, which i2 draws as a band instead.
    """
    buckets: dict[str, list[str]] = {}
    status: list[str] = []
    for ch in log.channels:
        if is_status_channel(ch):
            status.append(ch.name)
            continue
        key = ch.unit.strip() or "无单位"
        buckets.setdefault(key, []).append(ch.name)

    def rank(item: tuple[str, list[str]]) -> tuple[int, int, str]:
        unit, channels = item
        try:
            position = GROUP_UNIT_ORDER.index(unit)
        except ValueError:
            position = len(GROUP_UNIT_ORDER)
        return (position, -len(channels), unit)

    out = []
    for unit, channels in sorted(buckets.items(), key=rank):
        label = GROUP_LABELS.get(unit, unit)
        name = f"{label} [{unit}]" if unit != "无单位" else label
        out.append(
            {
                "key": unit,
                "unit": "" if unit == "无单位" else unit,
                "label": name,
                "channels": channels,
            }
        )
    return out, status


def points(
    log: ldmod.LogFile,
    names: list[str],
    time: np.ndarray,
    start: float | None = None,
    end: float | None = None,
    max_points: int = 30000,
) -> dict:
    """Raw samples over a window, for the scatter component.

    Min/max decimation would move points away from the trajectory, so the
    scatter takes every n-th real sample instead and only ever reads the
    currently zoomed window.
    """
    lo = 0 if start is None else max(0, int(np.searchsorted(time, start)))
    hi = time.size if end is None else min(time.size, int(np.searchsorted(time, end)))
    if hi <= lo:
        return {"time": [], "values": {}, "stride": 1}
    stride = max(1, int(np.ceil((hi - lo) / max(1, max_points))))
    index = np.arange(lo, hi, stride)
    out = {"time": np.round(time[index], 4).tolist(), "values": {}, "stride": stride}
    for name in names:
        if not log.has(name):
            continue
        values = derive.hold_to_master(log, name)[: time.size]
        out["values"][name] = np.round(_finite(values)[index], 5).tolist()
        out.setdefault("units", {})[name] = log.channel(name).unit
    return out


def histogram(
    log: ldmod.LogFile,
    name: str,
    time: np.ndarray,
    bins: int = histogrammod.DEFAULT_BINS,
    start: float | None = None,
    end: float | None = None,
    gate: str | None = None,
    gate_mode: str = "nonzero",
    gate_lo: float | None = None,
    gate_hi: float | None = None,
    colour: str | None = None,
) -> dict:
    """一条通道在一个窗口里的取值分布（ticket #9）。

    窗口是**半开区间** ``[start, end)``，和切圈、报表一个口径。算的是原始样本，
    不是降采样后的包络——Min/Max 抽稀会把每一格塞进两个极值，分布会变成假的。
    所以这个入口只在 serve 模式按需调用（快照里内嵌的是导出时算好的几份，
    见 :func:`snapshot_histograms`）。
    """
    if not log.has(name):
        raise ValueError(
            f"本场次没有通道 {name!r}。先在左侧「通道」里搜一下名字——"
            f"名字里含空格 / 括号 / 短横线的，在表达式里要用单引号括起来。"
        )
    size = int(time.size)
    values = derive.hold_to_master(log, name)[:size]
    lo = 0 if start is None else max(0, int(np.searchsorted(time, float(start))))
    hi = size if end is None else min(size, int(np.searchsorted(time, float(end))))
    if hi <= lo:
        # 空窗口不是"分布是零"：给一句能照做的提示，别让图上出现一条平线。
        return {
            "channel": name,
            "unit": log.channel(name).unit,
            "colour_channel": colour,
            "colour_unit": log.channel(colour).unit if colour and log.has(colour) else "",
            "bins": [], "count": 0, "excluded": 0, "range": None,
            "stats": histogrammod.summarize([]),
            "window": [start, end],
            "notice": "这个区间里没有样本——时间轴的起止是不是选反了？",
        }

    # 门槛可以先是一条通道名，也可以是一条数学通道表达式——两种都在
    # histogram.gate_values 里解析，别在界面里再写一套 if。
    gate_series = None
    if gate:
        gate_series = histogrammod.gate_values(log, gate, size)[lo:hi]
    colour_values = None
    if colour:
        if not log.has(colour):
            raise ValueError(
                f"着色通道 {colour!r} 不在本场次里。要么换一条，要么把「色」选成「无」。"
            )
        colour_values = derive.hold_to_master(log, colour)[:size][lo:hi]

    payload = histogrammod.histogram(
        values[lo:hi],
        count=bins,
        gate=gate_series,
        colour=colour_values,
        gate_mode=gate_mode,
        gate_lo=gate_lo,
        gate_hi=gate_hi,
    )
    payload["channel"] = name
    payload["unit"] = log.channel(name).unit
    payload["colour_channel"] = colour
    payload["colour_unit"] = log.channel(colour).unit if colour else ""
    payload["window"] = [
        round(float(time[lo]), 4),
        round(float(time[max(lo, hi - 1)]), 4),
    ]
    payload["notice"] = _histogram_notice(payload, bins)
    return payload


def _histogram_notice(payload: dict, bins: int) -> str | None:
    """窗口里发生的、用户必须知道才不会被误导的那几件事。"""
    if not payload.get("count"):
        if payload.get("excluded"):
            return (f"门槛把这一段 {payload['excluded']} 个样本全排除了——"
                    f"放宽条件或者换个窗口再看")
        return "这条通道在这个窗口里没有有效样本（NaN 不算样本）"
    if payload.get("excluded"):
        return (f"已按门槛排除 {payload['excluded']} 个样本，"
                f"剩下 {payload['count']} 个参与统计")
    if payload.get("range") and payload["stats"]["min"] == payload["stats"]["max"]:
        return (f"这条通道在窗口内一直是 {payload['stats']['min']}，没有变化"
                f"（区间已撑开，只为能画出来）")
    return None


def snapshot_histograms(
    log: ldmod.LogFile,
    laps,
    channels: list[str],
    bins: int = histogrammod.DEFAULT_BINS,
) -> dict:
    """快照里内嵌的那几份分布：整场 + 每条完整圈，每条通道各一份。

    快照背后没有服务可以再问一次，所以窗口只能先算好——但**算法还是上面那一个**，
    内嵌只是把 ``counts`` 存成数组（区间等宽，``range`` 加格数就能还原每格的边界）。
    界面上改分箱数只能**往粗里并格**（并格是精确的，再细分就是编的），所以这里统一
    按 ``bins`` 格算好，前端报出它实际用的格数。

    不内嵌区段窗口：区段是"每条圈 × 每个区段"，26 条圈的场次会变成几百份，
    快照会大到发不出去。要看某个区段的分布，缩放之后在 serve 模式下看。
    """
    time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
    windows: list[dict] = [{
        "key": "all", "label": "整场", "start": 0.0,
        "end": round(float(log.duration), 4),
    }]
    for lap in laps:
        if not getattr(lap, "complete", False):
            continue        # 进场/出场那半圈会把分布拖出一截假的长尾
        windows.append({
            "key": str(lap.label), "label": str(lap.label),
            "start": round(float(lap.start_time), 4),
            "end": round(float(lap.end_time), 4),
        })

    series: dict[str, dict] = {}
    for name in channels:
        if not name or not log.has(name):
            continue
        entries: dict[str, dict] = {}
        for window in windows:
            try:
                payload = histogram(
                    log, name, time, bins=bins,
                    start=window["start"], end=window["end"],
                )
            except ValueError:
                continue
            entries[window["key"]] = {
                "range": payload["range"],
                "count": payload["count"],
                "stats": payload["stats"],
                "counts": [b["count"] for b in payload["bins"]],
                "notice": payload["notice"],
            }
        series[name] = {"unit": log.channel(name).unit, "windows": entries}

    return {
        "bins": int(bins),
        "colour": False,        # 快照不内嵌按第三通道着色：色值是"每条通道 × 每个窗口"
        "windows": windows,     # 一份，索引用；数据在 series 里按窗口 key 存
        "series": series,
        "notice": None if series else "本场没有可统计的通道",
    }


def spectrum(
    log: ldmod.LogFile,
    name: str,
    start: float | None = None,
    end: float | None = None,
    points: int = spectrummod.DEFAULT_POINTS,
    window: str = spectrummod.DEFAULT_WINDOW,
    overlap: float = spectrummod.DEFAULT_OVERLAP,
    smooth: int = 1,
    scale: str = spectrummod.DEFAULT_SCALE,
    detrend: bool = True,
) -> dict:
    """一条通道在 ``[start, end)`` 上的频谱（Welch 平均周期图，ticket #10）。

    **按通道自己的采样率算**：慢通道在主时间基上是被"保持"拉长的，那种台阶的频谱
    会凭空多出一堆高频。窗口时间也是按该通道自己的时间基切的——两边都是从 0 起算，
    所以"第几秒"是同一个意思。
    """
    if not log.has(name):
        raise ValueError(
            f"本场次没有通道 {name!r}。先在左侧「通道」里搜一下名字——"
            f"名字里含空格 / 括号 / 短横线的，在表达式里要用单引号括起来。"
        )
    channel = log.channel(name)
    derived = ldmod.is_derived_channel(log, channel)
    # 派生列（数学通道）在主时间基上，原生通道用自己那一档。
    fs = float(log.sample_rate if derived else channel.sample_rate)
    values = np.asarray(log.values(channel), dtype=np.float64)
    lo = 0 if start is None else max(0, int(round(float(start) * fs)))
    hi = values.size if end is None else min(values.size, int(round(float(end) * fs)))
    if hi <= lo:
        return {
            "channel": name,
            "unit": channel.unit,
            "frequencies": [], "power": [],
            "points": 0, "sample_rate": fs, "nyquist": fs / 2.0,
            "resolution": None, "segments": 0, "samples": 0,
            "window": window, "overlap": overlap, "smooth": smooth, "scale": scale,
            "window_range": [start, end],
            "peak_frequency": None, "peak_value": None,
            "notice": "这个区间里没有样本——时间轴的起止是不是选反了？",
            "derived": derived,
        }
    payload = spectrummod.welch(
        values[lo:hi], fs, points=points, window=window, overlap=overlap,
        smooth=smooth, scale=scale, detrend=detrend,
    )
    payload["frequencies"] = [float(v) for v in payload["frequencies"]]
    payload["power"] = [float(v) for v in payload["power"]]
    payload["channel"] = name
    payload["unit"] = channel.unit
    payload["derived"] = derived
    payload["window_range"] = [round(lo / fs, 4), round(max(lo, hi - 1) / fs, 4)]
    if name.endswith(" Temp") and payload["peak_frequency"] and payload["peak_frequency"] < 0.05:
        # 温度这类慢通道的谱大部分是漂移，不说一句用户会以为"悬架很软"。
        payload["notice"] = ((payload["notice"] or "")
                             + " 这条通道变化很慢，谱里的能量基本是漂移而不是振动——"
                               "看悬架请选 G Force / 位移 / 加速度那类通道。").strip()
    return payload


def snapshot_spectra(
    log: ldmod.LogFile,
    channels: list[str],
    points: int = spectrummod.DEFAULT_POINTS,
    window: str = spectrummod.DEFAULT_WINDOW,
    overlap: float = spectrummod.DEFAULT_OVERLAP,
) -> dict:
    """快照里内嵌的那几份频谱：整场，每条被勾选的通道一份（ticket #10）。

    只嵌**整场**、只嵌**默认参数**：频谱的每个参数组合都是一整条曲线，按圈再各带一份
    会让快照大到发不出去。要按区段或按参数现算，用 serve 模式。
    """
    series: dict[str, dict] = {}
    for name in channels:
        if not name or not log.has(name):
            continue
        try:
            payload = spectrum(log, name, points=points, window=window, overlap=overlap)
        except ValueError:
            continue
        series[name] = {
            "unit": payload["unit"],
            "sample_rate": payload["sample_rate"],
            "points": payload["points"],
            "resolution": payload["resolution"],
            "segments": payload["segments"],
            "peak_frequency": payload["peak_frequency"],
            "peak_value": payload["peak_value"],
            "power": payload["power"],
        }
    return {
        "points": int(points),
        "window": window,
        "overlap": float(overlap),
        "series": series,
        "notice": None if series else "本场没有可做频谱的通道",
    }


def downsample(
    time: np.ndarray,
    values: np.ndarray,
    distance: np.ndarray | None = None,
    buckets: int = DEFAULT_BUCKETS,
) -> dict:
    """Min/max decimation that keeps spikes (per MoTeC / i3rs practice).

    Each pixel column contributes both the local minimum and the local maximum,
    so a one-sample brake-pressure spike survives zooming out - a plain
    every-nth-sample decimation would drop it.
    """
    n = time.size
    values = _finite(values)
    if n <= buckets * 2:
        picked = np.arange(n)
    else:
        edges = np.linspace(0, n, buckets + 1).astype(int)
        rows: list[int] = []
        for a, b in zip(edges, edges[1:]):
            if b <= a:
                continue
            chunk = values[a:b]
            lo = int(np.argmin(chunk))
            hi = int(np.argmax(chunk))
            rows.extend(sorted({a + lo, a + hi}))
        picked = np.asarray(rows, dtype=np.int64)

    out = {
        "time": np.round(time[picked], 4).tolist(),
        "value": np.round(values[picked], 5).tolist(),
    }
    if distance is not None and distance.size == n:
        out["distance"] = np.round(_finite(distance)[picked], 3).tolist()
    return out


def _downsample_breaks(breaks: np.ndarray, step: int) -> np.ndarray:
    """抽稀时"断开"一个字都不能丢：一格里面断过一次，这一格就断。

    轨迹载荷会被抽稀到 1500 点，而跳点可能只有一个采样（0.05 s），直接按步长取值
    会把它整段丢掉——那样图上又会连出一条不存在的直线。

    **断在哪一段**才是容易错的地方：``breaks[i]`` 指的是"第 ``i-1`` 与第 ``i`` 个点
    之间不许连线"。抽稀之后，第 ``k`` 个点代表源数据第 ``k*step`` 个采样，所以源
    下标 ``i`` 处那个断点落在**被画出来的**第 ``t = (i-1)//step`` 段里（第 ``t`` 与
    第 ``t+1`` 个抽稀点之间），要断的是那一段，不是桶首那一段。早先按桶首算，结果
    跳过了跳变前面那 0.2 m 的正常段，反而把 214 m 的幽灵线画了出来（真机截图抓到的）。
    """
    flags = np.asarray(breaks, dtype=bool)
    if step <= 1 or flags.size == 0:
        return flags.copy()
    # shifted[j] = flags[j+1]：把"落在第 i 个点上的断点"挪到第 i-1 位上，这样
    # 按桶取 or 得到的就是"(i-1)//step == 桶号"的那个断点
    shifted = np.zeros_like(flags)
    shifted[:-1] = flags[1:]
    in_bucket = np.maximum.reduceat(shifted, np.arange(0, flags.size, step))
    out = np.zeros_like(in_bucket)
    out[1:] = in_bucket[:-1]
    out[0] = False
    return out


def channel_index(log: ldmod.LogFile) -> list[dict]:
    """Name / unit / rate for every channel, so the UI can search all of them."""
    derived = getattr(log, "derived_names", ())
    derived_units = getattr(log, "derived_units", None) or {}
    out = []
    for ch in log.channels:
        # 数学通道算出来的列在主时间基上，单位和采样率都以它的定义为准；
        # 与一条慢的原生通道同名时，界面不能还显示那条原生通道的 1 Hz。
        is_derived = ch.name in derived
        out.append(
            {
                "name": ch.name,
                "unit": derived_units.get(ch.name, ch.unit) if is_derived else ch.unit,
                "rate": log.sample_rate if is_derived else ch.sample_rate,
                "samples": ch.sample_count,
                "derived": is_derived,
            }
        )
    return out


def trace(
    log: ldmod.LogFile,
    name: str,
    time: np.ndarray,
    distance: np.ndarray | None = None,
    buckets: int = DEFAULT_BUCKETS,
    start: float | None = None,
    end: float | None = None,
) -> dict:
    """One channel, held onto the master time base and min/max decimated."""
    values = derive.hold_to_master(log, name)[: time.size]
    if start is not None or end is not None:
        lo = 0 if start is None else int(np.searchsorted(time, start))
        hi = time.size if end is None else int(np.searchsorted(time, end))
        lo, hi = max(0, lo), min(time.size, hi)
        payload = downsample(
            time[lo:hi],
            values[lo:hi],
            None if distance is None else distance[lo:hi],
            buckets,
        )
    else:
        payload = downsample(time, values, distance, buckets)
    # 数学通道的单位来自它的定义，采样率是主时间基——哪怕它和一条慢的原生通道同名
    channel = log.channel(name)
    derived_units = getattr(log, "derived_units", None)
    payload["unit"] = (derived_units or {}).get(name, channel.unit)
    payload["rate"] = (
        log.sample_rate if ldmod.is_derived_channel(log, channel) else channel.sample_rate
    )
    return payload


def pick_channels(log: ldmod.LogFile, limit: int = 12) -> list[str]:
    """Prefer the engineer's usual channels, then pad with anything live."""
    picked = [name for name in DEFAULT_CHANNEL_PRIORITY if log.has(name)]
    if len(picked) < limit:
        for ch in log.channels:
            if ch.name in picked or not ch.unit:
                continue
            values = log.values(ch)
            if values.size and float(np.nanmax(values) - np.nanmin(values)) > 1e-6:
                picked.append(ch.name)
            if len(picked) >= limit:
                break
    return picked[:limit]


def overlay_channels(log: ldmod.LogFile) -> list[str]:
    picked = [name for name in OVERLAY_PRIORITY if log.has(name)]
    return picked or pick_channels(log, 3)


def build_overlay(log: ldmod.LogFile, laps, channels, step: float = 1.0) -> dict | None:
    """Distance-axis overlay for the given laps (``None`` when unusable)."""
    if len(laps) < 2:
        return None
    channels = [c for c in channels if log.has(c)]
    if not channels:
        return None
    try:
        result = lapsmod.overlay(log, laps[:2], channels, step=step)
    except (ValueError, KeyError):
        return None
    distance = np.asarray(result["distance"], dtype=np.float64)
    delta = np.asarray(lapsmod.time_delta(result["laps"][0], result["laps"][1], distance)[1])
    return {
        "distance": np.round(distance, 3).tolist(),
        "channels": channels,
        "step": step,
        "laps": [
            {
                "lap": row["lap"],
                "lap_time": round(float(row["lap_time"]), 3),
                "length": round(float(row["length"]), 1),
                "time": np.round(np.asarray(row["time"], dtype=np.float64), 4).tolist(),
                **{
                    c: np.round(np.asarray(row[c], dtype=np.float64), 5).tolist()
                    for c in channels
                },
            }
            for row in result["laps"]
        ],
        "delta": np.round(delta, 4).tolist(),
    }


def detect(log: ldmod.LogFile):
    """Laps for this session, honouring the saved beacon/lap sidecar."""
    try:
        return lapsmod.detect_from_config(log)
    except ValueError:
        return []


def default_lap_pair(log: ldmod.LogFile, laps, ref: str | None = None, cmp: str | None = None):
    """The two laps a race engineer wants by default: the quickest ones."""
    if len(laps) < 2:
        return None, None
    by_label = {l.label: l for l in laps}
    if ref and ref in by_label:
        chosen_ref = by_label[ref]
    else:
        ranked = sorted((l for l in laps if l.complete), key=lambda l: l.lap_time) or laps
        chosen_ref = ranked[0]
    if cmp and cmp in by_label and by_label[cmp] is not chosen_ref:
        return chosen_ref, by_label[cmp]
    ranked = sorted(
        (l for l in laps if l.complete and l is not chosen_ref), key=lambda l: l.lap_time
    )
    chosen_cmp = ranked[0] if ranked else next((l for l in laps if l is not chosen_ref), None)
    return chosen_ref, chosen_cmp


def track_payload(
    log: ldmod.LogFile,
    points: int = 1500,
    start: float | None = None,
    end: float | None = None,
    fix: "gpsfix.FixConfig | None" = None,
) -> dict | None:
    """GPS trajectory in local metres, coloured by the best available speed.

    With ``start``/``end`` only the trajectory inside that time window is
    returned - i2 Pro's GPS Track component can plot either the whole selected
    data or just the zoomed data, and showing the lap you are looking at is what
    makes the map useful while analysing a corner.

    ``breaks`` 是给画线用的：里面每个下标都表示"这里不许和上一点连线"。
    空档（掉星）与跳点（一次 214 m 的错位定位）都算断开——轨迹图上不画一条
    不存在的直线，比画出来再说"其实那里没数据"要诚实。
    """
    try:
        # fix=None -> 按这个场次侧车里的「轨迹」作用域；显式传配置则原样用
        track = derive.gps_track(log, fix=fix)
    except ValueError:
        return None
    time = track["time"]
    x, y = track["x"], track["y"]
    lat, lon = track["lat"], track["lon"]
    breaks = np.asarray(track.get("breaks", np.zeros(time.size, dtype=bool)), dtype=bool)
    if breaks.size != time.size:
        breaks = np.zeros(time.size, dtype=bool)
    jump_rows = list(track.get("jump_rows") or [])
    holes = list(track.get("holes") or [])
    if start is not None or end is not None:
        lo = 0 if start is None else int(np.searchsorted(time, start))
        hi = time.size if end is None else int(np.searchsorted(time, end))
        lo, hi = max(0, lo), min(time.size, hi)
        time, x, y = time[lo:hi], x[lo:hi], y[lo:hi]
        lat, lon = lat[lo:hi], lon[lo:hi]
        breaks = breaks[lo:hi]
        jump_rows = [
            r for r in jump_rows if (start is None or r["to"] >= start)
            and (end is None or r["from"] <= end)
        ]
        holes = [
            h for h in holes if (start is None or h["to"] >= start)
            and (end is None or h["from"] <= end)
        ]
    if time.size < 2:
        return None
    speed_name = next((n for n in SPEED_FOR_COLORING if log.has(n)), None)
    if speed_name is None:
        speed = np.zeros(time.size)
    else:
        master = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
        speed = np.interp(time, master, derive.hold_to_master(log, speed_name))
    step = max(1, time.size // max(1, points))
    breaks_out = _downsample_breaks(breaks, step)
    return {
        "x": np.round(x[::step], 2).tolist(),
        "y": np.round(y[::step], 2).tolist(),
        "speed": np.round(speed[::step], 2).tolist(),
        "time": np.round(time[::step], 3).tolist(),
        "speed_channel": speed_name,
        "lat": np.round(lat[::step], 7).tolist(),
        "lon": np.round(lon[::step], 7).tolist(),
        # 抽稀之后"要不要断开"：只要这一格里断过一次，这一格就断
        "breaks": [int(i) for i in np.flatnonzero(breaks_out)],
        "jump_times": [round(float(r["from"]), 3) for r in jump_rows],
        "holes": [
            {
                "from": round(float(h["from"]), 3),
                "to": round(float(h["to"]), 3),
                "seconds": round(float(h["seconds"]), 3),
            }
            for h in holes
        ],
        "fix": track.get("fix"),
        "dropped": track.get("dropped"),
        # local frame origin, so a click on the map can be turned back into lat/lon
        "origin": list(track.get("origin") or (float(track["lat"][0]), float(track["lon"][0]))),
    }


def gps_payload(log: ldmod.LogFile, config: "gpsfix.FixConfig | None" = None) -> dict:
    """GPS 校正面板的载荷：当前配置 + "这段数据坏在哪"的实测计数。

    计数永远按**原始**轨迹算（那才是"数据有多脏"的答案）；校正生效时另外给一份
    ``applied``，说明它到底改了什么。面板上两句话都在，用户不用猜。
    """
    stored = None
    error = None
    try:
        stored = gpsfix.load_config(log.path)
    except ValueError as exc:
        error = str(exc)
    cfg = config if config is not None else (stored or gpsfix.FixConfig())
    out: dict = {
        "config": cfg.as_dict(),
        "stored": stored is not None,
        "error": error,
        "summary": None,
        "applied": None,
        "notice": None,
    }
    try:
        # 只用来数坏点：阈值按用户设的走，但不做时移 / 插值
        base = derive.gps_track(
            log,
            fix=gpsfix.FixConfig(spike_kmh=cfg.spike_kmh, gap_s=cfg.gap_s),
        )
    except ValueError as exc:
        out["notice"] = f"这场记录没有可用的 GPS 轨迹：{exc}"
        return out
    out["summary"] = gpsfix.summary(base, cfg)
    if cfg.enabled and cfg.scope_track:
        fixed = derive.gps_track(log, fix=cfg, scope="track")
        out["applied"] = {
            **(fixed.get("fix") or {}),
            "segments": int(fixed.get("segments") or 1),
            # 断开几处 = 抽稀前算出来的断点数（空档 + 跳点），不是"分了几段"
            "breaks": int(np.count_nonzero(fixed.get("breaks"))),
            "samples": int(np.asarray(fixed["time"]).size),
        }
    elif cfg.enabled:
        # 校正开着、但「轨迹」这一格没勾：图上看到的还是原始定位。不说清楚，
        # 用户会以为校正没生效，然后去调偏移量。
        out["notice"] = "校正已开，但「轨迹」这一格没勾——图上画的仍是原始定位。"
    return out


def notes_payload(log: ldmod.LogFile, track: dict | None = None) -> list[dict]:
    """这一场的注释 + 落点：时刻、文字、距离轴上的位置、轨迹图上的 x/y。

    读的是 `<场次>.notes.json`（写坏了就当没有，见 ``notes.load_notes``）。位置在这里
    算，界面只负责画：距离在主采样序列上插值（100 Hz 的格子，误差小于一个像素），
    轨迹取最近的那个抽稀采样（抽稀点之间隔着几米，插值没有意义）。
    """
    notes = notesmod.load_notes(log.path)
    if not notes:
        return []
    try:
        distance = lapsmod.distance_on_master(log)
    except ValueError:
        return notesmod.marks(notes, track)
    master = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
    size = min(master.size, distance.size)
    return notesmod.marks(notes, track, master[:size], distance[:size])


def sections_payload(
    log: ldmod.LogFile,
    laps=None,
    config=None,
    notice: str | None = None,
) -> dict:
    """赛道区段的载荷：区段表 + 参考圈上的分段 + 每条圈上的边界时刻。

    快照与本地服务走同一个函数——快照里也必须能看见区段带子，否则"图上有没有"
    就变成两种事实了。

    ``config`` 为空表示"看当前生效的"：侧车里存过就用它，没存过就按缺省参数自动
    切一份（**不落盘**），和 i2 Pro 第一次生成赛道图时的行为一致。
    """
    recognized = list(laps) if laps is not None else list(detect(log))
    lap = sectionsmod.reference_lap(recognized)
    out: dict = {
        "available": sectionsmod.available_bases(log),
        "basis_labels": dict(sectionsmod.BASIS_LABELS),
        "kind_labels": dict(sectionsmod.KIND_LABELS),
        "config": None,
        "lap": None if lap is None else lap.label,
        "length_m": 0.0,
        "bands": [],
        "laps": [],
        "summary": None,
        "notice": notice,
        "error": None,
    }
    if lap is None:
        out["notice"] = notice or "本场还没有圈，先放一个信标再来分区段"
        return out
    if config is None:
        try:
            config, auto_notice = sectionsmod.effective_config(log, recognized)
        except ValueError as exc:
            out["error"] = str(exc)
            out["notice"] = notice or str(exc)
            return out
        if auto_notice and not notice:
            notice = auto_notice
    out["config"] = config.as_dict()
    out["length_m"] = round(float(config.length_m), 1)
    out["notice"] = notice
    try:
        out["bands"] = sectionsmod.bands(log, lap, config)
        out["laps"] = sectionsmod.lap_marks(log, recognized, config)
        out["summary"] = sectionsmod.summarize(log, lap, config)
    except ValueError as exc:      # 参考圈的距离轴坏了：给原因，不给假的带子
        out["error"] = str(exc)
        out["notice"] = notice or str(exc)
    return out


def report_payload(
    log: ldmod.LogFile,
    laps=None,
    config=None,
    channels: list[str] | None = None,
    kind: str | None = None,
    by: str = "lap",
    lap_label: str | None = None,
) -> dict:
    """时间报告与通道报告：快照、服务、CLI 走的是同一个出口。

    区段切不出来时（本场没有圈、或者没有可用的判据）返回一条**能照做**的提示，
    而不是一张空表——空表看起来像"算出来就是零"，那是两种事实。
    """
    recognized = list(laps) if laps is not None else detect(log)
    notice = None
    if config is None:
        try:
            config, notice = sectionsmod.effective_config(log, recognized)
        except ValueError as exc:
            return {"error": str(exc), "notice": str(exc), "time": None, "channels": None}
    if config is None or len(config.boundaries) < 2:
        message = notice or "本场还没有圈，先放一个信标再来看报表"
        return {"error": message, "notice": message, "time": None, "channels": None}
    chosen = [c for c in (channels or pick_channels(log)) if log.has(c)]
    out = reportmod.report_payload(
        log, recognized, config, chosen, kind=kind, by=by, lap_label=lap_label
    )
    out["notice"] = notice
    out["error"] = None
    return out


def snapshot_report(log: ldmod.LogFile, laps, config, channels: list[str]) -> dict:
    """快照里带的那一份报表：时间报告 + 通道报告（按圈 / 按区段各一份）。

    快照是双击就开的文件，背后没有服务可以再问一次，所以两种分组都得先算好，
    界面上的下拉框在离线时才有东西可切。区段分组只算参考圈那一份——把每条圈
    都算一遍会让快照里塞进几十倍的数据，而"换一条圈看区段"本来就是要联网的活。
    """
    reference = sectionsmod.reference_lap(laps)
    return {
        "time": reportmod.time_report(log, laps, config),
        "channels_lap": reportmod.channel_report(log, laps, config, channels, by="lap"),
        "channels_section": reportmod.channel_report(
            log, laps, config, channels, by="section"
        ),
        "reference_lap": None if reference is None else str(reference.label),
        "error": None,
        "notice": None,
    }


def build_payload(
    log: ldmod.LogFile,
    channels: list[str] | None = None,
    ref: str | None = None,
    cmp: str | None = None,
    buckets: int = DEFAULT_BUCKETS,
    api_base: str | None = None,
    step: float = 1.0,
    with_track: bool = True,
    overview_buckets: int = 900,
    with_report: bool = False,
    with_histograms: bool = False,
    with_spectra: bool = False,
) -> dict:
    """Everything the workbench needs. Traces are only embedded in static mode."""
    time = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
    try:
        distance = derive.distance_series(log)[: time.size]
    except ValueError:
        distance = None

    recognized = detect(log)
    chosen_ref, chosen_cmp = default_lap_pair(log, recognized, ref, cmp)

    selected = [c for c in (channels or pick_channels(log)) if log.has(c)]
    traces: dict[str, dict] = {}
    if api_base is None:
        for name in selected:
            traces[name] = trace(log, name, time, distance, buckets)

    # The outing strip always needs one cheap whole-session series. Prefer the
    # speed channel, because that is the shape a driver/engineer scans for.
    overview_name = next(
        (n for n in SPEED_FOR_COLORING if log.has(n)),
        selected[0] if selected else None,
    )
    overview = None
    if overview_name is not None:
        overview = trace(log, overview_name, time, distance, overview_buckets)
        overview["name"] = overview_name

    overlay = None
    if chosen_ref is not None and chosen_cmp is not None:
        overlay = build_overlay(log, [chosen_ref, chosen_cmp], overlay_channels(log), step)

    channel_groups, status_channels = groups(log)
    speed_name = derive.speed_channel(log)
    track = track_payload(log) if with_track else None
    return {
        "meta": {
            **log.metadata(),
            "speed_channel": speed_name,
            "has_distance": distance is not None,
            "lap_labels": [l.label for l in recognized],
            "duration": log.duration,
        },
        "channels": channel_index(log),
        "groups": channel_groups,
        "status": status_channels,
        "selected": selected,
        "traces": traces,
        "overview": overview,
        "laps": lapsmod.lap_table(log, recognized) if recognized else [],
        "overlay": overlay,
        "ref": None if chosen_ref is None else chosen_ref.label,
        "cmp": None if chosen_cmp is None else chosen_cmp.label,
        "track": track,
        "laps_config": lapsmod.load_config(log.path).as_dict(),
        # 注释：一张自己的侧车（`<场次>.notes.json`），不参与切圈 / 比圈 / 报表。
        # 快照把落点一起内嵌；serve 模式 /info 不内嵌轨迹，所以 x/y 是 None，
        # 由 /api/session/<名>/notes 现算（那边拿得到 GPS 轨迹）。
        "notes": notes_payload(log, track),
        "sections": sections_payload(log, recognized),
        # GPS 校正（ticket #14）：面板要的配置 + 这段数据坏在哪的实测计数
        "gps": gps_payload(log),
        "report": (
            _snapshot_report_or_error(log, recognized, selected) if with_report else None
        ),
        # 快照里内嵌的几份分布（整场 + 每条完整圈）。serve 模式不内嵌——那边是
        # 按当前缩放区间现算的，见 /api/session/<名>/histogram。
        "histograms": (
            snapshot_histograms(log, recognized, selected) if with_histograms else None
        ),
        # 快照里内嵌的频谱（整场、默认参数）。serve 模式不内嵌——那边按当前窗口现算，
        # 见 /api/session/<名>/spectrum。
        "spectra": (
            snapshot_spectra(log, selected) if with_spectra else None
        ),
        "api": api_base,
        "buckets": buckets,
        "session": log.path.stem,
    }


def _snapshot_report_or_error(
    log: ldmod.LogFile, recognized, channels: list[str]
) -> dict:
    """快照要的报表；切不出区段时给一条能照做的提示，而不是一张空表。"""
    try:
        config, notice = sectionsmod.effective_config(log, recognized)
    except ValueError as exc:
        return {"error": str(exc), "notice": str(exc), "time": None}
    if config is None or len(config.boundaries) < 2:
        message = notice or "本场还没有圈，先放一个信标再来看报表"
        return {"error": message, "notice": message, "time": None}
    out = snapshot_report(log, recognized, config, channels)
    out["notice"] = notice
    return out


def render_html(
    log: ldmod.LogFile,
    out: str | Path,
    channels: list[str] | None = None,
    ref: str | None = None,
    cmp: str | None = None,
    buckets: int = DEFAULT_BUCKETS,
    with_track: bool = True,
    with_report: bool = True,
    with_histograms: bool = True,
    with_spectra: bool = True,
) -> Path:
    """Write a self-contained workbench snapshot and return its path."""
    payload = build_payload(
        log,
        channels=channels,
        ref=ref,
        cmp=cmp,
        buckets=buckets,
        with_track=with_track,
        with_report=with_report,
        with_histograms=with_histograms,
        with_spectra=with_spectra,
    )
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(payload), encoding="utf-8")
    return out


def render_page(payload: dict) -> str:
    """Inject a payload into the workbench template."""
    template = TEMPLATE.read_text(encoding="utf-8")
    return template.replace(
        "/*__I3PRO_DATA__*/null", json.dumps(payload, ensure_ascii=False)
    )
