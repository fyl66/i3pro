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
import datetime
import re
from dataclasses import dataclass, field
import inspect
from pathlib import Path

import numpy as np
import pandas as pd

from . import derive, ld as ldmod, render, sidecar

__all__ = ["CsvSession", "read_csv_session", "canonical_names", "ALIASES", "scan_rows",
           "load_map", "save_map", "load_sheet", "load_options", "save_options",
           "MAP_SUFFIX", "open_session", "NO_HEADER",
           "layout_of", "time_index_of", "session_from_frame", "merged_maps"]

#: Column-mapping sidecar written next to a CSV the user corrected by hand.
#: Same principle as the lap sidecar (ADR-0001): the data file stays untouched.
#: 侧车后缀由 ``sidecar.KINDS["csvmap"]`` 说了算；这个名字留着给文档引用。
MAP_SUFFIX = sidecar.kind_of("csvmap").suffix

#: Column names that mean "this is the time axis".
TIME_NAMES = frozenset({"time", "t", "timestamp", "times", "time s"})

#: ``layout_of(header=...)`` 用这个值表示"这份表**没有表头行**"：列名退化成
#: ``列1`` / ``列2`` …，所有行都是数据（ticket #32）。没有它的话，"只有数字的
#: TXT"这种很常见的导出根本进不来——猜表头那一步只能猜出"找不到表头行"。
NO_HEADER = -1

#: 写进 ``<场次>.map.json`` 的**解析方式**（ticket #32）。和列名覆盖同住一张侧车：
#: 它们回答的是同一个问题——"这份表该怎么读"。
OPTION_KEYS = ("delimiter", "encoding", "header", "unit_row",
               "generate_rate", "generate_start")

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
    return sidecar.path_of("csvmap", path)


def load_map(path: str | Path) -> dict:
    """Manual column corrections for this table, or empty when there are none."""
    data = sidecar.read("csvmap", path)
    return {"renames": dict(data.get("renames") or {}),
            "units": dict(data.get("units") or {})}


def load_sheet(path: str | Path) -> str:
    """Excel：上一次选的是哪一张 sheet（没选过就是空串）。"""
    return str(sidecar.read("csvmap", path).get("sheet") or "")


def save_map(
    path: str | Path,
    renames: dict[str, str],
    units: dict[str, str],
    sheet: str | None = None,
) -> Path:
    """把手工选择写进 ``<场次>.map.json``。

    在这份**原始** sidecar 上合并，而不是在 :func:`load_map` 的返回值上合并——
    后者只认得 ``renames`` / ``units``，会把 ``sheet`` 这类别的键悄悄丢掉
    （真发生过：Excel 选了第三张 sheet，存一次列名覆盖就退回了第一张）。
    """
    merged = dict(sidecar.read("csvmap", path))
    merged["renames"] = {**(merged.get("renames") or {}), **renames}
    merged["units"] = {**(merged.get("units") or {}), **units}
    if sheet is not None:
        merged["sheet"] = str(sheet)
    return sidecar.write("csvmap", path, merged)


def merged_maps(
    path: str | Path,
    renames: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """边车里存的手工映射 + 这次调用显式给的（显式的优先），键都归一化。"""
    stored = load_map(path)
    merged_renames = {**stored["renames"], **(renames or {})}
    merged_units = {**stored["units"], **(units or {})}
    return ({_normalise(k): v for k, v in merged_renames.items()},
            {_normalise(k): v for k, v in merged_units.items()})


def load_options(path: str | Path) -> dict:
    """这份表上次是用什么方式解析的（分隔符 / 编码 / 表头行 / 单位行 / 生成时间列）。

    空值不返回：``delimiter=""`` 和"没记过"是两件事，调用方要用 ``is None`` 区分，
    所以宁可把没记过的键整个省掉。
    """
    raw = sidecar.read("csvmap", path)
    return {key: raw[key] for key in OPTION_KEYS if key in raw and raw[key] not in (None, "")}


def save_options(path: str | Path, **options) -> Path:
    """把解析方式写进 ``<场次>.map.json``（在原始侧车上合并，别的键不动）。

    ``header=NO_HEADER``（-1）要能存进来，所以判空只认 ``None`` 与空串，不认 0。
    传 ``None`` 表示"这一项改回自动"，会把这一个键删掉。
    """
    unknown = [key for key in options if key not in OPTION_KEYS]
    if unknown:
        raise ValueError(
            f"不认识的解析选项 {'、'.join(unknown)}。这一版能存的是："
            f"{'、'.join(OPTION_KEYS)}。"
        )
    merged = dict(sidecar.read("csvmap", path))
    for key, value in options.items():
        if value is None or value == "":
            merged.pop(key, None)
        else:
            merged[key] = value
    return sidecar.write("csvmap", path, merged)

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


#: ``Vx KF [km/h]``：我们自己导出宽表时把单位写进列名（Excel 里一眼看得出量纲）。
#: 读回来的时候要把它拆回"名字 + 单位"，否则一条通道换个名字，"两条来源不打架"
#: 那条对拍和距离轴都会跟着失效——那正是 round-trip 这条硬判据要挡住的东西。
_BRACKET_UNIT = re.compile(r"^(?P<name>.+?)\s*\[(?P<unit>[^\[\]]{0,24})\]\s*$")


def split_unit(raw: str) -> tuple[str, str]:
    """``"Vx KF [km/h]"`` -> ``("Vx KF", "km/h")``；没有方括号后缀就原样返回。"""
    match = _BRACKET_UNIT.match(str(raw).strip())
    if not match:
        return str(raw).strip(), ""
    return match.group("name").strip(), match.group("unit").strip()


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
        # 表头猜不出来的两个真实原因：整份表被切成了一列（分隔符不对），或者
        # 这份表压根没有表头行。两个都要说到，因为下一步不一样。
        raise ValueError(
            "找不到表头行：读到的每一行都只有一列，或者前几行全是数字。"
            "下一步：多半是分隔符不对（分号 / Tab 的表按逗号读就会整份变成一列），"
            "在导入预览里换一个分隔符，或手动指定表头行 / 选「没有表头行」。"
        )
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
    #: 数学通道的列放进 ``columns``（CSV 没有原生/派生的存储之分），名字与单位
    #: 和 ``.ld`` 会话一样在这里**声明**，下游走同一条缝（见 channels.py，ticket #18）。
    derived_names: set[str] = field(default_factory=set)
    derived_units: dict[str, str] = field(default_factory=dict)
    #: 这份表是从什么文件读来的（``csv`` / ``xlsx``）以及读的哪张 sheet。
    #: 下游只拿它写文案（场次页、导入报告、导出元数据），不参与取值。
    fmt: str = "csv"
    sheet: str = ""

    @property
    def derived_target(self) -> dict[str, np.ndarray]:
        """数学通道的列放哪（CSV 会话与原生列同住 ``columns``）。"""
        return self.columns

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
        # ``parse_note`` 由读取器写进 ``header``（ticket #32）：分隔符 / 编码 / 表头行 /
        # 生成时间列这些"这份表是怎么读出来的"，界面抬头上要看得见——否则用户没法
        # 判断"列名没匹配上"到底是文件的问题还是我们读法的问题。
        note = str(self.header.get("parse_note") or "")
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
            "format": self.fmt,
            **({"parse_note": note} if note else {}),
            **({"sheet": self.sheet} if self.sheet else {}),
        }

    def __repr__(self) -> str:                    # pragma: no cover - cosmetic
        return (f"<CsvSession {self.path.name} {len(self.channels)} channels "
                f"{self.duration:.1f}s>")


def read_csv_session(
    path: str | Path,
    renames: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
    **options,
) -> CsvSession:
    """Read ``path`` into a session, plus a per-column mapping report.

    ``renames`` / ``units`` are the manual override: keyed by the *original*
    column name (or its normalised form), they win over automatic matching.

    分隔文本只有一条读取器（ticket #32）：``.csv`` 与 ``.txt`` 的差别只是**默认**
    分隔符，其余（编码 / 表头行 / 单位行 / 生成时间列）都是同一件事。所以这里把活
    交给 :mod:`i3pro.txtlog`——它先看侧车里记下的解析方式，没记过才现猜。
    """
    from . import txtlog                      # 本地 import：txtlog 反过来要用本模块

    return txtlog.read_text_session(path, renames=renames, units=units, **options)


def layout_of(
    rows: list[list[str]],
    header: int | None = None,
    unit_row: bool | None = None,
) -> tuple[int, int]:
    """``(表头行号, 数据起始行号)``——分隔文本与 Excel 两条读取器都走这里。

    ``header`` / ``unit_row`` 是**用户在导入预览里挑的**（ticket #32，写进侧车）；
    不给就按老规矩猜：最宽且不全是数字的那一行是表头，它下面一行不是数字就是单位行。
    ``header=NO_HEADER`` 表示这份表没有表头行，列名按位置叫 列1/列2…。
    """
    if header is not None and header < 0:
        return NO_HEADER, 0
    head = _header_index(rows) if header is None else int(header)
    if head >= len(rows):
        raise ValueError(
            f"表头行选的是第 {head + 1} 行，可这份表只有 {len(rows)} 行。"
            "下一步：在导入预览里换一行，或者选「没有表头行」。"
        )
    if unit_row is None:
        candidate = rows[head + 1] if head + 1 < len(rows) else []
        has_unit_row = bool(candidate) and not any(
            _looks_numeric(c) for c in candidate if c != ""
        )
    else:
        has_unit_row = bool(unit_row)
    return head, head + (2 if has_unit_row else 1)


def time_index_of(names: list[str], resolve, label: str) -> int:
    """哪一列是时间。在**解析后**的名字上找，所以手工映射也能把某一列命名成 Time。"""
    for i, raw_name in enumerate(names):
        if _normalise(resolve(raw_name)[0]) in TIME_NAMES:
            return i
    raise ValueError(
        f"{label}: 找不到时间列（列名里没有 Time / t / Timestamp）。"
        '用 --map "原始列=Time" 指定哪一列是时间，或在导出时带上时间列；'
        "如果这份表本来就没有时间（一行就是一个采样点），用 --rate 100 "
        "（或在导入预览里勾「按固定采样率生成时间列」）现造一列 —— "
        "没有时间轴就无法切圈，也不该拿别的列冒充。"
    )


def _resolver(renames: dict[str, str], canonical: dict[str, str]):
    def resolve(raw_name: str) -> tuple[str, str]:
        key = _normalise(raw_name)
        if key in renames:
            return renames[key] or str(raw_name).strip(), "手工指定"
        if key in canonical:
            return canonical[key], "原名"
        if key in ALIASES:
            return ALIASES[key], "别名"
        return str(raw_name).strip(), "未匹配"

    return resolve


def _floats(series) -> np.ndarray | None:
    """一列 -> float 数组；只要有一个非空单元格不是数字就返回 ``None``。

    纯数值列走 numpy 的快车道；分隔文本读出来的列是**字符串**（ticket #32 起不再
    经过 pandas 的类型推断），所以先整列向量化试一次 ``to_numeric``——按格子
    ``float()`` 的话，341 列 × 19 万行要 60 秒（实测），向量化是几毫秒。只有整列
    试不成时才退回到逐格解析，逐格解析能把"空格子"与"填了非数字"分开：前者留空，
    后者整列交回去让调用方按"非数值列"处理——**不猜、不当 0**。
    """
    if pd.api.types.is_bool_dtype(series) or pd.api.types.is_numeric_dtype(series):
        return series.to_numpy(dtype=np.float64)
    values = series.to_numpy(dtype=object)
    fast = _vector_floats(values)
    if fast is not None:
        return fast
    out = np.full(values.shape, np.nan, dtype=np.float64)
    for i, value in enumerate(values):
        if value is None or value is pd.NaT:
            continue
        if isinstance(value, bool):
            out[i] = float(value)
            continue
        if isinstance(value, (int, float, np.integer, np.floating)):
            out[i] = float(value)
            continue
        text = str(value).strip().replace(",", "")
        if not text:
            continue
        try:
            out[i] = float(text)
        except ValueError:
            return None
    return out


def _vector_floats(values: np.ndarray) -> np.ndarray | None:
    """整列向量化解析；有一个非空格子解析不了就返回 ``None``（交给逐格那条路）。"""
    try:
        text = pd.Series(values, dtype="object").astype("string")
    except (TypeError, ValueError):              # pragma: no cover - 极端脏数据
        return None
    stripped = text.str.strip().str.replace(",", "", regex=False)
    numbers = pd.to_numeric(stripped, errors="coerce")
    empty = stripped.isna() | stripped.eq("")
    if (numbers.isna() & ~empty).any():
        return None
    return numbers.to_numpy(dtype=np.float64, na_value=np.nan)


def _stamp(value) -> datetime.datetime | None:
    """一个单元格 -> 时刻。认 Excel 的日期单元格和 ISO 文本，别的一律 ``None``。"""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, np.datetime64):
        # numpy 的 datetime64 不能直接转 datetime（ns 精度会溢出），先降到微秒。
        return value.astype("datetime64[us]").astype(datetime.datetime)
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return None                       # 数字在"日期时间列"里没有意义，别乱猜
    text = str(value).strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _time_values(series) -> tuple[np.ndarray | None, str, str]:
    """时间列 -> ``(相对秒, 起点时刻的文本, 说明)``。

    三种写法都认：**相对秒**（数字）、**Excel 日期单元格**、**ISO 文本**。后两种
    按"第一行是起点"折算成相对秒，起点本身留给场次的日期/时间用。
    """
    numbers = _floats(series)
    if numbers is not None:
        return numbers, "", ""
    stamps = [_stamp(value) for value in series.to_numpy(dtype=object)]
    known = [s for s in stamps if s is not None]
    if not known:
        return None, "", ""
    origin = known[0]
    seconds = np.full(len(stamps), np.nan, dtype=np.float64)
    for i, stamp in enumerate(stamps):
        if stamp is None:
            continue
        try:
            seconds[i] = (stamp - origin).total_seconds()
        except TypeError as exc:      # 一个带时区一个不带：说出来，别猜
            raise ValueError(
                f"时间列里有的时刻带时区、有的不带（{exc}）。"
                "下一步：在 Excel 里把这一列的格式统一，或先另存为 CSV 再导入。"
            ) from exc
    return seconds, origin.isoformat(sep=" "), "日期时间列"


def session_from_frame(
    path: str | Path,
    rows: list[list[str]],
    frame,
    *,
    renames: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
    fmt: str = "csv",
    sheet: str = "",
    header: int | None = None,
    unit_row: bool | None = None,
    generate_rate: float | None = None,
    generate_start: float | None = None,
) -> CsvSession:
    """把"表头那几行 + 一张数据表"装配成一个场次（ticket #31）。

    分隔文本与 Excel 两条读取器共用这一份：表头在哪一行、有没有单位行、哪一列是时间、
    列名走原名/别名还是手工覆盖、报告怎么写——都只在这里回答一次。``frame`` 是
    **已经跳过表头**的数据表，单元格可以是数字、文本、日期（Excel）或数字（CSV）。

    ``header`` / ``unit_row`` 是用户在导入预览里挑的（``None`` = 还是猜）；
    ``generate_rate`` 是最多猜错一次的地方——这份表**没有时间列**时，按这个采样率
    现造一列时间（ticket #32）。表本来就有时间列时它不起作用：宁可忽略用户勾的框，
    也不能让造出来的时间盖掉文件里真实的时间。``generate_start`` 是这一列的起点
    （秒，默认 0），侧车把它记下来，第二次读得到同一列。
    """
    path = Path(path)
    renames, units = merged_maps(path, renames, units)
    head, skip = layout_of(rows, header, unit_row)
    has_unit_row = skip == head + 2
    if head == NO_HEADER:
        # 没有表头行：列名按位置叫 列1/列2…（"只有数字的 TXT"是常态，不该被逼着
        # 先回去加一行表头）。宽度只能从数据本身看。
        raw_names = [f"列{i + 1}" for i in range(int(frame.shape[1]))]
        names = list(raw_names)
        named_units = [""] * len(raw_names)
    else:
        raw_names = [str(c).strip() for c in rows[head]]
        pairs = [split_unit(raw) for raw in raw_names]
        names = [name for name, _unit in pairs]
        named_units = [unit for _name, unit in pairs]
    unit_cells = list(rows[head + 1]) if has_unit_row and head + 1 < len(rows) else []
    canonical = {_normalise(n): n for n in canonical_names()}
    resolve = _resolver(renames, canonical)

    generated = False
    try:
        time_index = time_index_of(names, resolve, path.name)
    except ValueError:
        if not generate_rate:
            raise
        rate = float(generate_rate)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError(
                f"生成时间列要用正的采样率，收到 {generate_rate!r}。"
                "下一步：填一个正数（例如 100），或先给这份表补一列时间。"
            ) from None
        time_index, generated = 0, True
        frame = frame.copy()
        frame.insert(0, "__generated_time__",
                     float(generate_start or 0.0) + np.arange(int(frame.shape[0])) / rate)
        raw_names.insert(0, "Time")
        names.insert(0, "Time")
        named_units.insert(0, "s")
        unit_cells.insert(0, "")
    if time_index >= frame.shape[1]:
        raise ValueError(f"{path.name}: 表头写了时间列，数据里却没有这一列")
    time, time_origin, time_note = _time_values(frame.iloc[:, time_index])
    if generated:
        time_note = f"按固定采样率生成（{rate:g} Hz）"
    if time is None:
        raise ValueError(
            f"{path.name}: 时间列（{names[time_index]}）里没有可用的时刻。"
            "这一列要么是相对秒（数字），要么是日期/时间（Excel 单元格或 ISO 文本）；"
            '如果时间在别的列，用 --map "原始列=Time" 指定。'
        )
    good = np.isfinite(time)
    time = time[good]
    if time.size < 2:
        raise ValueError(
            f"{path.name}: 时间列只有 {time.size} 个可用时刻，至少要有 2 个才能算采样率。"
            "下一步：补上时间列，或换一份导出（导出时勾上时间轴）。"
        )
    steps = np.diff(time)
    steps = steps[steps > 0]
    rate = float(1.0 / np.median(steps)) if steps.size else 0.0
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError(
            f"{path.name}: 时间列不是单调递增。"
            "下一步：确认这一列真的是时间（不是行号或倒序的时间戳）。"
        )

    # The other two matching signals: the metadata block's declared sample rate,
    # and the unit each canonical channel normally carries.
    meta = _metadata(rows, max(head, 0))         # head 可能是 NO_HEADER(-1)
    rate_from = "生成" if generated else "时间列"
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
            report.append({"column": raw_names[i], "status": "缺列",
                           "detail": "表头列数多于数据列数"})
            continue
        if i == time_index:
            report.append({"column": raw_names[i], "status": "时间轴", "name": "Time",
                           "matched_by": "时间列", "unit": "s", "rate": rate,
                           "samples": int(time.size),
                           **({"generated": True} if generated else {}),
                           **({"detail": time_note, "origin": time_origin}
                              if time_note else {})})
            continue
        values = _floats(frame.iloc[:, i])
        if values is None:
            report.append({"column": raw_names[i], "status": "跳过（非数值列）",
                           "detail": "这一列有非数字的格子"})
            continue
        values = values[good]
        if values.size and not np.isfinite(values).any():
            report.append({"column": raw_names[i], "status": "跳过（非数值列）",
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
        if not unit and i < len(unit_cells):
            unit = str(unit_cells[i]).strip()
        if not unit:
            unit = named_units[i]              # 列名里自带的「[单位]」
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
        report.append({"column": raw_names[i], "status": "通道", "name": name,
                       "matched_by": matched_by, "unit": unit, "rate": rate,
                       "rate_from": rate_from, "warning": warning,
                       "samples": int(values.size)})

    if not channels:
        raise ValueError(f"{path.name}: 没有可用的通道列")
    if not meta.get("Log Date") and time_origin:
        meta.setdefault("Log Date", time_origin[:10])
        meta.setdefault("Log Time", time_origin[11:19])
    return CsvSession(
        path=path, channels=channels, columns=columns, time=time,
        sample_rate=rate, duration=float(time[-1] - time[0]),
        header={"rate_from": rate_from, "metadata": meta,
                **({"time_origin": time_origin} if time_origin else {}),
                **({"sheet": sheet} if sheet else {})},
        device=meta.get("Device") or {"xlsx": "Excel", "txt": "文本"}.get(fmt, "CSV"),
        log_date=meta.get("Log Date", ""),
        log_time=meta.get("Log Time", ""),
        event_name=meta.get("Event") or path.stem, report=report,
        fmt=fmt, sheet=sheet,
    )


def open_session(path: str | Path, **kwargs):
    """按**内容**挑读取器，调用方从此不用关心格式。

    ``.csv`` 有两种完全不同的东西：i2 Pro / 别的工具导出的**通道表**，和 CAN 记录仪
    写出来的**原始帧表**。它们都叫 ``.csv``，但列名和读法毫无共同之处，所以这里按
    表头特征先分一次流（ticket #38）；认不出是帧表就还是走通道表那条路。

    ``.xlsx`` 走 Excel 那条路（ticket #31）：读法不同（zip 里的 OOXML、日期单元格、
    共享字符串），但**装配成一个场次**那一步是同一份代码（``session_from_frame``）。

    ``.txt`` / ``.tsv`` 是纯分隔文本（ticket #32）：分隔符要猜、时间列可能没有，
    但装出来仍然是同一个场次形状。
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        from . import canlog

        # 调用方（场次库）把**同一份** kwargs 递给两个读取器：`dbc_dir` 这类只有帧表
        # 读取器认识。两条路都按各自签名过滤，而不是给其中一条写死白名单——写死白名单
        # 的那版把 `dbc_dir` 也转给了通道表读取器，于是**每个 CSV 场次的请求都 500**
        # （真发生过：ticket #38 那次回归，靠恢复金标准数据才暴露出来）。
        if canlog.looks_like_frames(path):
            return canlog.read_can_session(path, **_accepted_by(canlog.read_can_session, kwargs))
        return read_csv_session(path, **_accepted_by(read_csv_session, kwargs))
    if suffix in (".txt", ".tsv"):
        from . import txtlog

        return txtlog.read_text_session(
            path, **_accepted_by(txtlog.read_text_session, kwargs)
        )
    if suffix == ".xlsx":
        from . import xlslog

        return xlslog.read_xlsx_session(path, **_accepted_by(xlslog.read_xlsx_session, kwargs))
    return ldmod.LogFile.read(path)


def _accepted_by(func, kwargs: dict) -> dict:
    """只把 ``func`` 签名里确实有的参数交给它（多出来的属于另一个读取器）。"""
    allowed = set(inspect.signature(func).parameters)
    return {key: value for key, value in kwargs.items() if key in allowed}
