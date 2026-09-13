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

from . import ld as ldmod

__all__ = [
    "SPEED_CANDIDATES",
    "speed_channel",
    "speed_series",
    "distance_series",
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
    factor = max(1, int(round(log.sample_rate / ch.sample_rate)))
    if factor > 1:
        values = np.repeat(values, factor)
    n = int(round(log.duration * log.sample_rate)) + 1
    if values.size < n:
        pad = values[-1] if values.size else 0.0
        values = np.concatenate([values, np.full(n - values.size, pad)])
    return values[:n]


def distance_series(log: ldmod.LogFile, min_speed: float = 0.0) -> np.ndarray:
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


def gps_track(log: ldmod.LogFile, sats_channel: str = "GPS Sats Used") -> dict:
    """Return the GPS trajectory in local metres, with invalid fixes removed."""
    pair = next(((la, lo) for la, lo in GPS_PAIRS if log.has(la) and log.has(lo)), None)
    if pair is None:
        raise ValueError(f"{log.path.name}: no GPS latitude/longitude channels")
    lat_ch, lon_ch = pair
    lat = log.values(lat_ch)
    lon = log.values(lon_ch)
    rate = log.channel(lat_ch).sample_rate
    time = np.arange(lat.size) / rate
    valid = (np.abs(lat) > 1e-3) & (np.abs(lon) > 1e-3)
    if log.has(sats_channel):
        sats = log.values(sats_channel)
        if sats.size == lat.size:
            valid &= sats >= 4
    if valid.sum() < 10:
        raise ValueError(f"{log.path.name}: GPS never got a usable fix")
    start = int(np.argmax(valid))
    time, lat, lon, valid = time[start:], lat[start:], lon[start:], valid[start:]
    time = time[valid]
    lat = lat[valid]
    lon = lon[valid]
    x, y = to_meters(lat, lon)
    return {
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
    }
