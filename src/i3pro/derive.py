"""Derived quantities: speed series, track distance, GPS track projection.

The team's C125 logs do not populate the MoTeC ``Distance`` math channel (it is
flat zero in every file we inspected), so i3pro derives the distance axis from
the best available speed source:

1. ``Ground Speed`` / wheel speeds when logged,
2. ``Vx KF`` (the logger's own Kalman-filtered longitudinal speed),
3. ``GPS Speed``.

``distance = cumsum(v * dt)`` evaluated on the log's master time base.
"""

from __future__ import annotations

import numpy as np

from . import channelref
from . import channels as channelsmod
from . import gpsfix
from . import ld as ldmod
from . import timebase

__all__ = [
    "SPEED_CANDIDATES",
    "SATS_MIN",
    "UPSAMPLE_MODES",
    "check_upsample",
    "name_table",
    "resolve_channel",
    "speed_channel",
    "speed_series",
    "upsample_to_master",
    "distance_series",
    "gps_pair",
    "gps_distance",
    "gps_track",
    "to_meters",
]

#: i2 Pro 的 Channel Up-sampling 属性（帮助文件 《Log Files → Channel Properties》原文：
#: "how to calculate the channel value when a value is required that lies between sample
#: points. This is used when displaying a graph to 'join the dots'."）。四种取值与它一一对应：
#: ``linear`` 线性插值 / ``previous`` 保持前值（零阶保持）/ ``closest`` 取最近点 / ``next`` 取下一个点。
#: **画图默认 ``linear``**，分析路径仍然用 ``previous``（见 :func:`upsample_to_master`）。
UPSAMPLE_MODES = ("linear", "previous", "closest", "next")


def check_upsample(mode: str) -> str:
    """只认 :data:`UPSAMPLE_MODES` 里的四个词，别的报中文错并列出可选项。

    报错文案**只有这一处**：谁先校验谁就把这句直接转给用户，不许再抄一遍
    （抄一遍的下场是两句话慢慢漂开，而用户只看到其中一句）。
    """
    if mode not in UPSAMPLE_MODES:
        raise ValueError(
            f"不认识的曲线连接方式 {mode!r}：可选的只有 "
            + " / ".join(UPSAMPLE_MODES)
            + "。"
        )
    return mode


SPEED_CANDIDATES = (
    "Ground Speed",
    "GPS Speed",
    "Vx KF",
    "SpeedFR",
    "SpeedFL",
    "SpeedRR",
    "SpeedRL",
    "Vx",
)


def name_table(log: ldmod.LogFile) -> dict[str, str]:
    """``normalise(名字) -> 真名``，给 :func:`resolve_channel` 一次建好反复用。

    规则本身在 :mod:`i3pro.channelref`（"谁指哪条通道"只有那一处实现）；
    这里只是"本场次有哪些通道"这一个领域问题的入口。
    """
    return channelref.name_table(channelref.known_names(log))


def resolve_channel(
    log: ldmod.LogFile, name: str, table: dict[str, str] | None = None
) -> str | None:
    """``name`` 在本场次里实际叫什么；没有这条通道就是 ``None``。

    先按原样找，再按"去掉空格/下划线/大小写"找一遍：``.ld`` 里那条 ``Vx KF``
    与 CAN 线（``i2pro_data/dbc/TH.dbc`` 的 ``0xC1 Throttle_INFO``）解出来的
    ``Vx_KF`` 是同一个量。

    规则在 :func:`i3pro.channelref.lookup` 一处实现；``table`` 是给"一次问很多遍"
    的调用方省事的（:func:`name_table` 建一次）。
    """
    known = channelref.known_names(log)
    if isinstance(name, str) and name in known:
        return name
    return channelref.lookup(known, name,
                             table=table if table is not None else name_table(log))


GPS_PAIRS = (
    ("GPS Latitude", "GPS Longitude"),
    ("PosLat", "PosLon"),
    ("Latitude", "Longitude"),
)

#: 经纬度的词头。同一条总线上的第三方传感器会在后面加自己的后缀
#: （Xsens MTi 解出来的是 ``latitude_MTI`` / ``longitude_MTI``），所以配对要看
#: "同一后缀"而不是只认写死的那几个全名。
_LAT_HEADS = ("latitude", "poslat", "lat")
_LON_HEADS = ("longitude", "poslon", "lon")

#: 卫星数低于这个值算"没有定位"。判据只在卫星数通道**真的报过数**时才成立：
#: 本车日志里 ``GPS Sats Used`` 整份只有 0 与 -1（死通道），拿它去否定位会把
#: GPS 全部否掉——合并场次撞的就是这个（见 :func:`gps_track`）。
SATS_MIN = 4


def gps_pair(log: ldmod.LogFile) -> tuple[str, str] | None:
    """本场次里那一对经纬度通道；一条都不齐就返回 ``None``。

    两步，都是纯名字判断（不碰数值）：

    1. :data:`GPS_PAIRS` 里写死的那三对，按"去掉空格/下划线/大小写"匹配
       （``GPS Latitude`` / ``GPSPosLat`` 这类写法差异都能认）；
    2. **同一后缀**的一对：前缀恰好是认得的词、后缀逐字相同——
       ``latitude_MTI`` / ``longitude_MTI`` 就是这么配上的（ticket #42 加）。
       只比前缀不比对后缀的话，``Lateral`` / ``Longitudinal`` 会被误当成经纬度。

    结果只跟场次里的通道名有关，所以同样的输入永远给同样的答案。
    """
    table = name_table(log)
    for lat_name, lon_name in GPS_PAIRS:
        lat_ch = resolve_channel(log, lat_name, table)
        lon_ch = resolve_channel(log, lon_name, table)
        if lat_ch and lon_ch and lat_ch != lon_ch:
            return lat_ch, lon_ch
    # 同后缀的一对（latitude_MTI / longitude_MTI）：算法在 channelref，
    # 词头是本领域的词汇，留在这里。
    return channelref.suffix_pair(channelref.known_names(log), _LAT_HEADS, _LON_HEADS)


def speed_channel(log: ldmod.LogFile) -> str | None:
    """Pick the most trustworthy speed channel present in the log.

    名字的空格/下划线写法差异交给 :func:`resolve_channel`（``Vx KF`` / ``Vx_KF``）；
    这里比"找一条速度"多一条判据：**候选要真的在动**（``max|v| > 1 km/h``）。
    距离轴是拿这条速度积分出来的，死通道会积出一条假的 0 m 轴——实测
    ``2026_10_03_201147_ID0001.csv`` 的 ``Vx_KF`` 是 −0.05…0.00 kph，那种场次
    应该"没有距离轴"，而不是"有一条 0 米的距离轴"。

    只想要"哪条通道叫速度"（概览条画什么、轨迹按什么着色）就**别**用这个——
    那种场合不要求它在动，见 :func:`render.display_speed_channel`。
    """
    table = name_table(log)
    for candidate in SPEED_CANDIDATES:
        actual = resolve_channel(log, candidate, table)
        if actual is None:
            continue
        values = np.asarray(log.values(actual), dtype=np.float64)
        finite = values[np.isfinite(values)]
        if finite.size and float(np.max(np.abs(finite))) > 1.0:
            # 空通道（整列 NaN）不是"速度很大"，也不是"速度为零"；它只是没有
            # 采样。先取有限值再判，既避免 nanmax 警告，也让距离轴继续找下一条。
            return actual
    return None


def speed_series(log: ldmod.LogFile) -> np.ndarray:
    """Speed in km/h on the master time base (sample & hold for slow channels)."""
    name = speed_channel(log)
    if name is None:
        raise ValueError(f"{log.path.name}: no usable speed channel")
    return hold_to_master(log, name)


def hold_to_master(log: ldmod.LogFile, name: str) -> np.ndarray:
    ch = log.channel(name)
    values = log.values(ch)
    # 「数学通道已经在主时间基上、别再拉一遍」这条规则只写在 channels.py 里（ticket #18）
    factor = channelsmod.hold_factor(log, ch, log.sample_rate)
    if factor > 1:
        values = np.repeat(values, factor)
    n = timebase.length(log)
    if values.size < n:
        pad = values[-1] if values.size else 0.0
        values = np.concatenate([values, np.full(n - values.size, pad)])
    return values[:n]


def upsample_to_master(
    log: ldmod.LogFile, name: str, mode: str = "linear"
) -> np.ndarray:
    """把一条通道搬到主时间基上，**点与点之间怎么取值**由 ``mode`` 说了算。

    这是 i2 Pro 的 Channel Up-sampling 属性：

    * ``"previous"``：保持前值，就是 :func:`hold_to_master`——**分析路径一直用它**，
      距离轴、切圈、报表、导出、直方图一个都没改；
    * ``"linear"``：相邻两个原始采样之间连一条直线，i2 Pro 画图 "join the dots" 的默认观感；
    * ``"closest"``：取最近的那个采样点；
    * ``"next"``：取下一个采样点。

    为什么画图要单开这一条路：`.ld` 里慢通道不少（高避金标准实测 437 条里 147 条低于
    50 Hz，`Gear` / 5V 供电只有 1 Hz）。零阶保持会把 1 Hz 的通道画成"每 1 秒一个平顶、
    中间一道竖直跳变"，而 i2 Pro 默认把这些点连成斜线——同一个文件，观感差别就在这。

    已经在主时间基上的列（数学通道、CAN 用例导入时已经零阶保持的列）没有"点与点之间"
    可插，四种取值的答案都是它自己：这时候**不该**再抽一遍，否则数学通道会被按原生
    采样率拉长（ticket #18 修过一次的错）。

    ``mode`` 不认识时报中文错并列出可选项（:func:`check_upsample`）——调用方
    （HTTP 那一层）直接把这句转给用户。
    """
    check_upsample(mode)
    ch = log.channel(name)
    n = timebase.length(log)
    values = np.asarray(log.values(ch), dtype=np.float64)
    if mode == "previous" or channelsmod.is_derived(log, ch) or values.size == n:
        return hold_to_master(log, name)
    if values.size == 0:
        return np.zeros(n, dtype=np.float64)
    times = ch.time()
    master = timebase.axis(log)
    if mode == "linear":
        out = np.interp(master, times, values)
    else:
        index = np.searchsorted(times, master)
        if mode == "next":
            out = values[np.minimum(index, values.size - 1)]
        else:                                   # closest：左右两点比一下谁近
            right = np.minimum(index, values.size - 1)
            left = np.maximum(right - 1, 0)
            nearer = np.where(
                np.abs(times[right] - master) <= np.abs(master - times[left]), right, left
            )
            out = values[nearer]
    if out.size < n:
        pad = out[-1] if out.size else 0.0
        out = np.concatenate([out, np.full(n - out.size, pad)])
    return out[:n]


def distance_series(
    log: ldmod.LogFile, min_speed: float = 0.0, fix: "gpsfix.FixConfig | None" = None
) -> np.ndarray:
    """Cumulative distance [m] on the master time base."""
    for name in ("Distance", "Distance (2)"):
        if log.has(name):
            values = log.values(name)
            # A distance channel has to actually *accumulate*: asking only for
            # max-min accepts a channel that is zero everywhere with a single
            # spike, which silently collapses every lap to zero length. Require
            # the endpoints to differ and the curve to be mostly non-decreasing.
            if values.size > 2 and float(values[-1] - values[0]) > 1.0:
                rising = float(np.mean(np.diff(values) >= -0.5))
                if rising > 0.95:
                    return hold_to_master(log, name) - float(values[0])
    # GPS 校正里的「距离轴」作用域：用校正后的 GPS 路径长度当距离轴。
    # 默认不开——现在所有圈速、区段、报表都建立在下面这条速度积分的距离轴上。
    config = gpsfix.resolve(log, fix, "distance")
    if config.enabled:
        gps = gps_distance(log, config)
        if gps is not None:
            return gps
    speed = speed_series(log) / 3.6  # km/h -> m/s
    # 缺读数的样本按 0 速度算。距离是**累计**量（`cumsum`），一个空样本会让它之后
    # 整条轴全是 NaN——实测：10.25 的两段耐久接起来之后，接缝上那一格是空行，于是
    # 从接缝起每一圈的圈距都是 `nan`，`laps` 拿它当中位数、把 50 段全判成不完整。
    # 一个样本的代价是 0.01 s 的速度（厘米级），比整条轴没了强。
    speed = np.where(np.isfinite(speed), speed, 0.0)
    # Rolling backwards / GPS jitter would otherwise make the axis shrink.
    speed = np.where(speed < max(min_speed, 0.0), 0.0, speed)
    dt = 1.0 / log.sample_rate
    return np.cumsum(speed) * dt


def to_meters(lat: np.ndarray, lon: np.ndarray, lat0: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Equirectangular projection to a local metric frame."""
    lat0 = float(lat[0]) if lat0 is None else lat0
    x = (lon - lon[0]) * 111_320.0 * np.cos(np.radians(lat0))
    y = (lat - lat[0]) * 110_540.0
    return x, y


def gps_distance(
    log: ldmod.LogFile, fix: "gpsfix.FixConfig | None" = None
) -> np.ndarray | None:
    """GPS 路径长度当距离轴（米，主采样序列上）。

    空档期间**保持不动**：那几秒不知道车走了多少，宁可少算也不编一段匀速直线。
    跳点与空档都按 ``gpsfix`` 的标注断开，不累加（否则一次 214 m 的错位定位会
    让整条距离轴凭空长 214 m）。
    """
    try:
        # 这里要的是"距离轴"这一格，不能再让 gps_track 按 scope_track 判一次
        track = gps_track(log, fix=fix, scope="distance")
    except ValueError:
        return None
    time = np.asarray(track["time"], dtype=float)
    if time.size < 2:
        return None
    distance = gpsfix.path_distance(
        time,
        np.asarray(track["x"], dtype=float),
        np.asarray(track["y"], dtype=float),
        track.get("breaks"),
    )
    master = timebase.axis(log)
    # 零阶保持：定位是抽样点，两点之间车走了多少是未知的，不插值
    index = np.clip(np.searchsorted(time, master, side="right") - 1, 0, time.size - 1)
    return distance[index]


def gps_track(
    log: ldmod.LogFile,
    sats_channel: str = "GPS Sats Used",
    fix: "gpsfix.FixConfig | None" = None,
    scope: str = "track",
) -> dict:
    """Return the GPS trajectory in local metres, with invalid fixes removed.

    ``fix`` 是这一场的 GPS 校正配置（``gpsfix.FixConfig``）：传 ``None`` 表示按
    ``scope`` 去读这个场次的侧车（``track`` = 给轨迹图用，``laps`` = 给切圈用），
    显式传一份配置则原样使用。没打开 ``enabled`` 时，``time`` / ``x`` / ``y`` 与
    旧实现**逐点相同**；只是多带几个标注字段：``breaks``（哪两点的连线不许画）、
    ``jumps`` / ``holes`` / ``dropped``。
    """
    config = gpsfix.resolve(log, fix, scope)
    pair = gps_pair(log)
    if pair is None:
        raise ValueError(f"{log.path.name}: no GPS latitude/longitude channels")
    lat_ch, lon_ch = pair
    lat = log.values(lat_ch)
    lon = log.values(lon_ch)
    rate = log.channel(lat_ch).sample_rate
    time = np.arange(lat.size) / rate
    # 掉星时记录仪给的是 (0, 0)——不滤掉就等于把车放到几内亚湾，距离轴、轨迹
    # 与切圈会一起被带歪。这里把它和"卫星数不足"分开计数，界面上要念出来。
    #
    # **空格子（NaN）同样是"没有定位"**：分隔文本（我们自己的导出在 auto 模式下
    # 就会留空）读回来时，比主时间基慢的 GPS 列有一半格子是空的。NaN 的两次比较
    # 都是 False，所以它以前**混在有效点里**——轨迹里带着 NaN 空洞，切圈从 7 圈
    # 静默退化成 1 圈（真跑出来过，ticket #32 的文本导入验收里撞见的）。
    no_fix = (np.abs(lat) <= 1e-3) | (np.abs(lon) <= 1e-3)
    no_fix |= ~np.isfinite(lat) | ~np.isfinite(lon)
    low_sats = np.zeros(lat.size, dtype=bool)
    if log.has(sats_channel):
        sats = np.asarray(log.values(sats_channel), dtype=float)
        if np.isfinite(sats).any() and np.nanmax(sats) >= SATS_MIN:
            # 卫星数这条通道通常比经纬度慢（.ld 里经纬度 50 Hz、卫星数 20 Hz），
            # 按零阶保持搬到经纬度那条轴上再逐点比。
            sats_rate = log.channel(sats_channel).sample_rate
            sats_time = np.arange(sats.size, dtype=np.float64) / sats_rate
            low_sats = timebase.hold_sample(sats_time, sats, time) < SATS_MIN
        # 一次都没报过 >= SATS_MIN 的卫星数通道是**死的**（本车实测就是：整份日志里
        # 只有 0 与 -1），它没有任何信息，不能拿它去否掉定位。以前那条判据写成
        # "卫星数通道与经纬度等长才用"——在 .ld 上因为两者采样率不同从来没生效，
        # 而合并场次把每条通道都搬到同一条主轴上之后它突然生效，于是合并出来的
        # csv 全都变成"GPS never got a usable fix"（2026-10-14 实测）。
    valid = ~(no_fix | low_sats)
    if valid.sum() < 10:
        raise ValueError(f"{log.path.name}: GPS never got a usable fix")
    start = int(np.argmax(valid))
    time, lat, lon, valid = time[start:], lat[start:], lon[start:], valid[start:]
    time = time[valid]
    lat = lat[valid]
    lon = lon[valid]
    x, y = to_meters(lat, lon)
    track = {
        "time": time,
        "lat": lat,
        "lon": lon,
        "x": x,
        "y": y,
        "rate": rate,
        "channels": (lat_ch, lon_ch),
        # the local frame's origin, so a lat/lon picked in the UI can be mapped
        # back into the same x/y coordinates
        "origin": (float(lat[0]), float(lon[0])),
        "dropped": {
            "total": int(no_fix.size),
            "no_fix": int(np.count_nonzero(no_fix)),
            "low_sats": int(np.count_nonzero(low_sats & ~no_fix)),
        },
    }
    if config.enabled:
        return gpsfix.correct(track, config, log.sample_rate)
    return gpsfix.annotate(track, config)
