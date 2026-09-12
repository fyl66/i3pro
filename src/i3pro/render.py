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
    "pick_channels",
    "track_payload",
]

TEMPLATE = Path(__file__).with_name("web") / "viewer.html"

DEFAULT_BUCKETS = 1200

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


def _finite(values: np.ndarray) -> np.ndarray:
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)


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
    try:
        return lapsmod.detect_laps(log)
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


def track_payload(log: ldmod.LogFile, points: int = 1500) -> dict | None:
    """GPS trajectory in local metres, coloured by the best available speed."""
    try:
        track = derive.gps_track(log)
    except ValueError:
        return None
    speed_name = next((n for n in SPEED_FOR_COLORING if log.has(n)), None)
    if speed_name is None:
        speed = np.zeros(track["x"].size)
    else:
        master = np.arange(int(round(log.duration * log.sample_rate)) + 1) / log.sample_rate
        speed = np.interp(track["time"], master, derive.hold_to_master(log, speed_name))
    step = max(1, track["x"].size // points)
    return {
        "x": np.round(track["x"][::step], 2).tolist(),
        "y": np.round(track["y"][::step], 2).tolist(),
        "speed": np.round(speed[::step], 2).tolist(),
        "time": np.round(track["time"][::step], 3).tolist(),
        "speed_channel": speed_name,
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

    overlay = None
    if chosen_ref is not None and chosen_cmp is not None:
        overlay = build_overlay(log, [chosen_ref, chosen_cmp], overlay_channels(log), step)

    speed_name = derive.speed_channel(log)
    return {
        "meta": {
            **log.metadata(),
            "speed_channel": speed_name,
            "has_distance": distance is not None,
            "lap_labels": [l.label for l in recognized],
        },
        "channels": channel_index(log),
        "selected": selected,
        "traces": traces,
        "laps": lapsmod.lap_table(log, recognized) if recognized else [],
        "overlay": overlay,
        "ref": None if chosen_ref is None else chosen_ref.label,
        "cmp": None if chosen_cmp is None else chosen_cmp.label,
        "track": track_payload(log) if with_track else None,
        "api": api_base,
        "buckets": buckets,
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
