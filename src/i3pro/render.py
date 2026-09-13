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

from . import derive, laps as lapsmod
from . import ld as ldmod

__all__ = [
    "render_html",
    "render_page",
    "build_payload",
    "build_overlay",
    "downsample",
    "channel_index",
    "trace",
    "points",
    "groups",
    "pick_channels",
    "track_payload",
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


def channel_index(log: ldmod.LogFile) -> list[dict]:
    """Name / unit / rate for every channel, so the UI can search all of them."""
    return [
        {
            "name": ch.name,
            "unit": ch.unit,
            "rate": ch.sample_rate,
            "samples": ch.sample_count,
        }
        for ch in log.channels
    ]


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
    payload["unit"] = log.channel(name).unit
    payload["rate"] = log.channel(name).sample_rate
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
) -> dict | None:
    """GPS trajectory in local metres, coloured by the best available speed.

    With ``start``/``end`` only the trajectory inside that time window is
    returned - i2 Pro's GPS Track component can plot either the whole selected
    data or just the zoomed data, and showing the lap you are looking at is what
    makes the map useful while analysing a corner.
    """
    try:
        track = derive.gps_track(log)
    except ValueError:
        return None
    time = track["time"]
    x, y = track["x"], track["y"]
    lat, lon = track["lat"], track["lon"]
    if start is not None or end is not None:
        lo = 0 if start is None else int(np.searchsorted(time, start))
        hi = time.size if end is None else int(np.searchsorted(time, end))
        lo, hi = max(0, lo), min(time.size, hi)
        time, x, y = time[lo:hi], x[lo:hi], y[lo:hi]
        lat, lon = lat[lo:hi], lon[lo:hi]
    if time.size < 2:
        return None
    speed_name = next((n for n in SPEED_FOR_COLORING if log.has(n)), None)
    if speed_name is None:
        speed = np.zeros(time.size)
    else:
        master = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
        speed = np.interp(time, master, derive.hold_to_master(log, speed_name))
    step = max(1, time.size // max(1, points))
    return {
        "x": np.round(x[::step], 2).tolist(),
        "y": np.round(y[::step], 2).tolist(),
        "speed": np.round(speed[::step], 2).tolist(),
        "time": np.round(time[::step], 3).tolist(),
        "speed_channel": speed_name,
        "lat": np.round(lat[::step], 7).tolist(),
        "lon": np.round(lon[::step], 7).tolist(),
        # local frame origin, so a click on the map can be turned back into lat/lon
        "origin": list(track.get("origin") or (float(track["lat"][0]), float(track["lon"][0]))),
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
        "track": track_payload(log) if with_track else None,
        "laps_config": lapsmod.load_config(log.path).as_dict(),
        "api": api_base,
        "buckets": buckets,
        "session": log.path.stem,
    }


def render_html(
    log: ldmod.LogFile,
    out: str | Path,
    channels: list[str] | None = None,
    ref: str | None = None,
    cmp: str | None = None,
    buckets: int = DEFAULT_BUCKETS,
    with_track: bool = True,
) -> Path:
    """Write a self-contained workbench snapshot and return its path."""
    payload = build_payload(
        log, channels=channels, ref=ref, cmp=cmp, buckets=buckets, with_track=with_track
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
