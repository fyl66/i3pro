"""Read a CSV into the same session shape the rest of the pipeline expects.

The whole point of this module is that **a CSV session and an `.ld` session are
the same thing downstream**: lap detection, the distance axis, lap comparison,
reports and the workbench all address a session through the small set of
accessors below (``channels`` / ``channel`` / ``has`` / ``values`` /
``sample_rate`` / ``duration`` …). Nothing downstream learns a second shape.

Two kinds of file go through here:

* **i2 Pro / C125 Manager exports** - quoted metadata rows, then a channel-name
  row, a unit row, then numbers.
* **any other CSV** - a header row, maybe a unit row, then numbers.

Both are found by the same heuristic: the header is the widest row in the first
few dozen, and the row after it is the unit row when none of its cells is a
number.
"""

from __future__ import annotations

import math
import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from . import derive, ld as ldmod, render

__all__ = ["CsvSession", "read_csv_session", "canonical_names", "ALIASES", "scan_rows",
           "load_map", "save_map", "MAP_SUFFIX", "open_session"]

#: Column-mapping sidecar written next to a CSV the user corrected by hand.
#: Same principle as the lap sidecar (ADR-0001): the data file stays untouched.
MAP_SUFFIX = ".map.json"

#: Column names that mean "this is the time axis".
TIME_NAMES = frozenset({"time", "t", "timestamp", "times", "time s"})

#: The unit each canonical channel normally carries, taken from the team's own
#: `C125` logs. Used to *check* a matched column, and to fill a unit the file
#: left blank. Only entries we have actually observed are listed.
CANONICAL_UNITS = {
    "Vx KF": "km/h", "GPS Speed": "km/h", "Ground Speed": "km/h",
    "SpeedFL": "km/h", "SpeedFR": "km/h", "SpeedRL": "km/h", "SpeedRR": "km/h",
    "G Force Lat": "G", "G Force Long": "G", "G Force Vert": "G",
    "GPS Heading": "deg", "Battery Power": "kW",
    "AMKFR ActualTorqueValue": "NM",
}

#: Metadata keys an i2 Pro export writes above the table.
META_KEYS = ("Device", "Log Date", "Log Time", "Sample Rate", "Venue",
             "Driver", "Vehicle", "Comment", "Event")


def _metadata(rows: list[list[str]], head: int) -> dict[str, str]:
    """Read the key/value rows an i2 Pro export puts above the table."""
    found: dict[str, str] = {}
    for row in rows[:head]:
        cells = [c.strip() for c in row if c.strip()]
        for i in range(0, len(cells) - 1, 2):
            if cells[i] in META_KEYS:
                found.setdefault(cells[i], cells[i + 1])
    return found


def _map_path(path: str | Path) -> Path:
    return Path(path).with_suffix(MAP_SUFFIX)


def load_map(path: str | Path) -> dict:
    """Manual column corrections for this CSV, or empty when there are none."""
    sidecar = _map_path(path)
    if not sidecar.exists():
        return {"renames": {}, "units": {}}
    try:
        import json

        data = json.loads(sidecar.read_text(encoding="utf-8"))
        return {"renames": dict(data.get("renames") or {}),
                "units": dict(data.get("units") or {})}
    except (OSError, ValueError, TypeError):
        return {"renames": {}, "units": {}}


def save_map(path: str | Path, renames: dict[str, str], units: dict[str, str]) -> Path:
    import json

    sidecar = _map_path(path)
    merged = load_map(path)
    merged["renames"].update(renames)
    merged["units"].update(units)
    sidecar.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return sidecar

#: Names our analysis asks for by name. Derived from the constants that already
#: look channels up, so this list cannot drift from them.
def canonical_names() -> list[str]:
    names = set(derive.SPEED_CANDIDATES)
    names |= set(render.DEFAULT_CHANNEL_PRIORITY)
    names |= set(render.OVERLAY_PRIORITY)
    names |= set(render.SPEED_FOR_COLORING)
    return sorted(names)


#: Foreign spellings seen in other teams' / other tools' exports, mapped onto the
#: names our analysis uses. Deliberately conservative: a column is only renamed
#: when the mapping is unambiguous, and the report always says what happened.
ALIASES = {
    "t": "Time",
    "timestamp": "Time",
    "gps speed": "GPS Speed",
    "gps velocity": "GPS Speed",
    "ground speed": "Ground Speed",
    "vehicle speed": "Vx KF",
    "wheel speed fl": "SpeedFL",
    "wheel speed fr": "SpeedFR",
    "wheel speed rl": "SpeedRL",
    "wheel speed rr": "SpeedRR",
    "throttle position": "TH",
    "throttle pos": "TH",
    "accelerator position": "TH",
    "apps": "TH",
    "brake pressure": "Brake Signal",
    "brake position": "Brake Signal",
    "brake": "Brake Signal",
    "steering angle": "SW Angle",
    "steering wheel angle": "SW Angle",
    "steer": "SW Angle",
    "lat accel": "G Force Lat",
    "lateral accel": "G Force Lat",
    "long accel": "G Force Long",
    "longitudinal accel": "G Force Long",
    "vert accel": "G Force Vert",
    "gps latitude": "GPS Latitude",
    "latitude": "GPS Latitude",
    "gps longitude": "GPS Longitude",
    "longitude": "GPS Longitude",
    "lap distance": "Distance",
}


def _normalise(text: str) -> str:
    """Case- and separator-insensitive key for matching."""
    keep = [c.lower() if c.isalnum() else " " for c in str(text).strip()]
    return " ".join("".join(keep).split())


def scan_rows(path: Path, limit: int = 40) -> list[list[str]]:
    """Decode the first ``limit`` rows, trying the encodings these files use."""
    for encoding in ("utf-8-sig", "gbk", "cp936", "latin-1"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                rows = []
                for _ in range(limit):
                    line = handle.readline()
                    if not line:
                        break
                    if "\ufffd" in line:
                        raise UnicodeDecodeError(encoding, b"", 0, 1, "replacement char")
                    rows.append(next(csv.reader([line])))
                return rows
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"{path.name}: 无法解码为文本")


def _header_index(rows: list[list[str]]) -> int:
    """The channel-name row: the widest row near the top that is not all numbers.

    Data rows are just as wide as the header, so width alone is not enough -
    excluding all-numeric rows is what stops the reader from treating the first
    line of data as the column names.
    """
    candidates = [(len(r), i) for i, r in enumerate(rows)
                  if len(r) > 1 and any(not _looks_numeric(c) for c in r if c != "")]
    if not candidates:
        candidates = [(len(r), i) for i, r in enumerate(rows) if len(r) > 1]
    if not candidates:
        raise ValueError("找不到表头行")
    return max(candidates, key=lambda pair: (pair[0], -pair[1]))[1]


def _looks_numeric(text: str) -> bool:
    try:
        float(str(text).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _decimals_of(source: list[str] | None) -> int:
    """Display precision: how many digits the file actually writes."""
    if source:
        best = 0
        for text in source[:2000]:
            text = str(text)
            if "." in text:
                best = max(best, len(text.split(".", 1)[1].rstrip("0")) or 0)
        return min(best, 6)
    return 2


@dataclass
class CsvSession:
    """A session backed by a CSV file. Mirrors the ``.ld`` session surface."""

    path: Path
    channels: list[ldmod.Channel]
    columns: dict[str, np.ndarray]
    time: np.ndarray
    sample_rate: float
    duration: float
    header: dict
    device: str
    log_date: str
    log_time: str
    event_name: str
    report: list[dict]

    def channel(self, name: str) -> ldmod.Channel:
        for ch in self.channels:
            if ch.name == name:
                return ch
        lowered = name.lower()
        for ch in self.channels:
            if ch.name.lower() == lowered:
                return ch
        raise KeyError(f"channel {name!r} not found in {self.path.name}")

    def has(self, name: str) -> bool:
        try:
            self.channel(name)
        except KeyError:
            return False
        return True

    def raw(self, name: str | ldmod.Channel) -> np.ndarray:
        ch = self.channel(name) if isinstance(name, str) else name
        return self.columns[ch.name]

    def values(self, name: str | ldmod.Channel) -> np.ndarray:
        """Already in engineering units - a CSV has no raw/scaled split."""
        return np.asarray(self.raw(name), dtype=np.float64)

    def time_base(self) -> np.ndarray:
        return self.time

    def close(self) -> None:                      # symmetry with LogFile
        pass

    def __enter__(self) -> "CsvSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def metadata(self) -> dict:
        return {
            "file": self.path.name,
            "device": self.device,
            "log_date": self.log_date,
            "log_time": self.log_time,
            "event": self.event_name,
            "sample_rate": self.sample_rate,
            "duration": self.duration,
            "channels": len(self.channels),
            "file_size": self.path.stat().st_size,
            "format": "csv",
        }

    def __repr__(self) -> str:                    # pragma: no cover - cosmetic
        return (f"<CsvSession {self.path.name} {len(self.channels)} channels "
                f"{self.duration:.1f}s>")


def read_csv_session(
    path: str | Path,
    renames: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
) -> CsvSession:
    """Read ``path`` into a session, plus a per-column mapping report.

    ``renames`` / ``units`` are the manual override: keyed by the *original*
    column name (or its normalised form), they win over automatic matching.
    """
    path = Path(path)
    stored = load_map(path)
    merged_renames = {**stored["renames"], **(renames or {})}
    merged_units = {**stored["units"], **(units or {})}
    renames = {_normalise(k): v for k, v in merged_renames.items()}
    units = {_normalise(k): v for k, v in merged_units.items()}

    rows = scan_rows(path)
    head = _header_index(rows)
    names = [str(c).strip() for c in rows[head]]
    unit_row = rows[head + 1] if head + 1 < len(rows) else []
    has_unit_row = bool(unit_row) and not any(_looks_numeric(c) for c in unit_row if c != "")
    skip = head + (2 if has_unit_row else 1)

    canonical = {_normalise(n): n for n in canonical_names()}

    def resolve(raw_name: str) -> tuple[str, str]:
        key = _normalise(raw_name)
        if key in renames:
            return renames[key] or str(raw_name).strip(), "手工指定"
        if key in canonical:
            return canonical[key], "原名"
        if key in ALIASES:
            return ALIASES[key], "别名"
        return str(raw_name).strip(), "未匹配"

    # The time column is found on the *resolved* names, so a manual mapping can
    # name it too.
    time_index = None
    for i, raw_name in enumerate(names):
        if _normalise(resolve(raw_name)[0]) in TIME_NAMES:
            time_index = i
            break
    if time_index is None:
        raise ValueError(
            f"{path.name}: 找不到时间列（列名里没有 Time / t / Timestamp）。"
            '用 --map "原始列=Time" 指定哪一列是时间，或在导出时带上时间列 —— '
            "没有时间轴就无法切圈，也不该拿别的列冒充。"
        )

    frame = pd.read_csv(
        path, header=None, skiprows=skip, skip_blank_lines=True,
        dtype=np.float64, engine="c", on_bad_lines="skip",
        encoding="utf-8-sig", encoding_errors="replace",
    )
    frame = frame.dropna(axis=1, how="all")
    if frame.empty:
        raise ValueError(f"{path.name}: 表头之后没有数值行")

    time = frame.iloc[:, time_index].to_numpy(dtype=np.float64)
    good = np.isfinite(time)
    time = time[good]
    if time.size < 2:
        raise ValueError(f"{path.name}: 时间列没有足够的数值")
    steps = np.diff(time)
    steps = steps[steps > 0]
    rate = float(1.0 / np.median(steps)) if steps.size else 0.0
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"{path.name}: 时间列不是单调递增")

    # The other two matching signals: the metadata block's declared sample rate,
    # and the unit each canonical channel normally carries.
    meta = _metadata(rows, head)
    rate_from = "时间列"
    try:
        meta_rate = float(meta.get("Sample Rate", ""))
    except ValueError:
        meta_rate = 0.0
    if meta_rate > 0 and abs(meta_rate - rate) / meta_rate < 0.05:
        rate, rate_from = meta_rate, "元数据"

    channels: list[ldmod.Channel] = []
    columns: dict[str, np.ndarray] = {}
    report: list[dict] = []
    for i, raw_name in enumerate(names):
        key = _normalise(raw_name)
        if i >= frame.shape[1]:
            report.append({"column": raw_name, "status": "缺列",
                           "detail": "表头列数多于数据列数"})
            continue
        values = frame.iloc[:, i].to_numpy(dtype=np.float64)[good]
        if i == time_index:
            report.append({"column": raw_name, "status": "时间轴", "name": "Time",
                           "matched_by": "时间列", "unit": "s", "rate": rate,
                           "samples": int(values.size)})
            continue
        if values.size and not np.isfinite(values).any():
            report.append({"column": raw_name, "status": "跳过（非数值列）",
                           "detail": "整列没有可用数值"})
            continue

        if key in renames:
            name, matched_by = renames[key], "手工指定"
        elif key in canonical:
            name, matched_by = canonical[key], "原名"
        elif key in ALIASES:
            name, matched_by = ALIASES[key], "别名"
        else:
            name, matched_by = str(raw_name).strip(), "未匹配"
        name = name or f"列{i + 1}"
        if name in columns:                       # two columns, one canonical name
            name = f"{name} ({i + 1})"

        unit = units.get(key, "")
        if not unit and has_unit_row and i < len(unit_row):
            unit = str(unit_row[i]).strip()
        expected = CANONICAL_UNITS.get(name)
        warning = ""
        if expected and not unit and matched_by in ("原名", "别名", "手工指定"):
            unit = expected                       # the file left the unit blank
        elif expected and unit and unit.lower() != expected.lower():
            warning = f"单位不符：文件写 {unit}，该通道通常是 {expected}"
        decimals = _decimals_of([f"{v:g}" for v in values[:200]])
        channels.append(ldmod.Channel(
            name=name, short_name="", unit=unit, sample_rate=rate,
            sample_count=int(values.size), data_offset=0, data_type=0,
            bytes_per_sample=0,
            multiplier=10 ** decimals, divider=1, decimals=decimals, shift=0,
            channel_id=i, index=len(channels),
        ))
        columns[name] = values
        report.append({"column": raw_name, "status": "通道", "name": name,
                       "matched_by": matched_by, "unit": unit, "rate": rate,
                       "rate_from": rate_from, "warning": warning,
                       "samples": int(values.size)})

    if not channels:
        raise ValueError(f"{path.name}: 没有可用的通道列")
    return CsvSession(
        path=path, channels=channels, columns=columns, time=time,
        sample_rate=rate, duration=float(time[-1] - time[0]),
        header={"rate_from": rate_from, "metadata": meta},
        device=meta.get("Device", "CSV"), log_date=meta.get("Log Date", ""),
        log_time=meta.get("Log Time", ""),
        event_name=meta.get("Event") or path.stem, report=report,
    )


def open_session(path: str | Path, **kwargs):
    """Pick the reader by extension, so callers stop caring about the format."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return read_csv_session(path, **kwargs)
    return ldmod.LogFile.read(path)
