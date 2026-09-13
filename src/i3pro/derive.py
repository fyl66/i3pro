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

from . import gpsfix
from . import ld as ldmod

__all__ = [
    "SPEED_CANDIDATES",
    "speed_channel",
    "speed_series",
    "distance_series",
    "gps_distance",
    "gps_track",
    "to_meters",
]

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

GPS_PAIRS = (
    ("GPS Latitude", "GPS Longitude"),
    ("PosLat", "PosLon"),
    ("Latitude", "Longitude"),
)


def speed_channel(log: ldmod.LogFile) -> str | None:
    """Pick the most trustworthy speed channel present in the log."""
    for candidate in SPEED_CANDIDATES:
        if log.has(candidate):
            values = log.values(candidate)
            if np.nanmax(np.abs(values)) > 1.0:  # not a dead channel
                return candidate
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
    # 数学通道算出来的列本来就在主时间基上；按原生采样率再拉一遍会把曲线毁掉
    factor = 1 if ldmod.is_derived_channel(log, ch) else max(
        1, int(round(log.sample_rate / ch.sample_rate))
    )
    if factor > 1:
        values = np.repeat(values, factor)
    n = int(round(log.duration * log.sample_rate)) + 1
    if values.size < n:
        pad = values[-1] if values.size else 0.0
        values = np.concatenate([values, np.full(n - values.size, pad)])
    return values[:n]


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
    n = int(round(log.duration * log.sample_rate)) + 1
    master = np.arange(n) / log.sample_rate
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
    pair = next(((la, lo) for la, lo in GPS_PAIRS if log.has(la) and log.has(lo)), None)
    if pair is None:
        raise ValueError(f"{log.path.name}: no GPS latitude/longitude channels")
    lat_ch, lon_ch = pair
    lat = log.values(lat_ch)
    lon = log.values(lon_ch)
    rate = log.channel(lat_ch).sample_rate
    time = np.arange(lat.size) / rate
    # 掉星时记录仪给的是 (0, 0)——不滤掉就等于把车放到几内亚湾，距离轴、轨迹
    # 与切圈会一起被带歪。这里把它和"卫星数不足"分开计数，界面上要念出来。
    no_fix = (np.abs(lat) <= 1e-3) | (np.abs(lon) <= 1e-3)
    low_sats = np.zeros(lat.size, dtype=bool)
    if log.has(sats_channel):
        sats = log.values(sats_channel)
        if sats.size == lat.size:
            low_sats = sats < 4
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
