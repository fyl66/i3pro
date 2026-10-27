"""MATLAB v5（TinyMAT）读侧：把逆变器日志读成场次。

德雷斯顿（Ephorus）控制器导出的 ``.mat`` 是 **MATLAB v5**（不是 v7.3 HDF5），由
TinyMAT 写出。读法与 CSV / Excel 一样收口到 ``csvlog.session_from_frame``——所以
画图、数学通道、导出、侧车都不用另写一份。为什么要手写而不是用 ``scipy``，见
``docs/adr/0013-mat-v5-reader-without-scipy.md``。

实测的两种"不按规格写"的地方，读的时候都要兜住：

* **元素不补齐 8 字节**：MATLAB 写的 v5 文件每个数据元素按 8 字节对齐，TinyMAT
  不补——按规格走会在第 3 个变量处错位。:func:`_next_pos` 两种都试。
* **同名重复变量**：每份日志都有 5 条（``Reserved_1..3``、``Use_*_Ethernet``）。
  这里改成 ``_2`` 后缀保留，不替用户丢列。

时间轴用文件里那条 ``t``（秒，间隔有抖动）；导入采样率默认 100 Hz，按整数倍抽点，
不插值——留下来的每个时刻都是真实采样时刻。文件里万一没有 ``t``，才退回声明的
原生采样率（默认 500 Hz）现造一条等间隔时间轴。
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

import numpy as np
import pandas as pd

from . import csvlog

__all__ = [
    "NATIVE_RATE_HZ", "DEFAULT_RATE_HZ", "guess_unit",
    "read_mat_session", "read_variables",
]

#: 兜底的原生采样率：文件里**没有** ``t`` 时才用它。这几份日志都带 ``t``
#: （秒，采样有抖动），实测中位间隔 2 ms ≈ 500 Hz；``mat/marple_ready/*.repair.json``
#: 里的 ``decimation_factor: 5`` 也与这个数对得上。
NATIVE_RATE_HZ = 500.0

#: 导入后的默认主时间基。900,096 点 ÷ 5 = 180,000 点，正好是一份 30 分钟的台架记录。
DEFAULT_RATE_HZ = 100.0

_HEADER = b"MATLAB 5.0 MAT-file"
_HDF5 = b"\x89HDF\r\n\x1a\n"

_DTYPES = {1: np.int8, 2: np.uint8, 3: np.int16, 4: np.uint16, 5: np.int32,
           6: np.uint32, 7: np.float32, 9: np.float64, 12: np.int64, 13: np.uint64}

_CLASSES = {1: "cell", 2: "struct", 3: "object", 4: "char", 5: "sparse",
            6: "double", 7: "single", 8: "int8", 9: "uint8", 10: "int16",
            11: "uint16", 12: "int32", 13: "uint32", 14: "int64", 15: "uint64"}

#: ``mxDOUBLE_CLASS`` … ``mxUINT64_CLASS``。这些类的数组才能当通道；``char``
#: 虽然也用整数元素存，但它的语义是文本，不能因为底层 tag 可读就混进通道。
_NUMERIC_CLASSES = frozenset(range(6, 16))

#: 单位后缀。**长的排前面**：``_A_rms`` 要先于 ``_A`` 命中，``_ms`` 要先于 ``_s``。
_UNIT_SUFFIXES = (
    ("_A_rms", "A"), ("_V_rms", "V"), ("_VAms", "VA/ms"), ("_Nmms", "Nm/ms"),
    ("_RPM", "RPM"), ("_degC", "degC"), ("_kHz", "kHz"), ("_Hz", "Hz"),
    ("_ns", "ns"), ("_Ohm", "Ohm"), ("_Nm", "Nm"), ("_VA", "VA"),
    ("_ms", "ms"), ("_s", "s"), ("_A", "A"), ("_V", "V"), ("_W", "W"),
)

def guess_unit(name: str) -> str:
    """通道名后缀 → 单位（猜）。猜不出来给空串，让人覆盖（ticket #72）。"""
    clean = str(name).strip().rstrip("_")
    for suffix, unit in _UNIT_SUFFIXES:
        if clean.endswith(suffix):
            return unit
    return ""


def _plausible(buf, pos: int) -> bool:
    if pos == len(buf):
        return True
    if pos + 8 > len(buf):
        return False
    raw = struct.unpack_from("<I", buf, pos)[0]
    kind = raw & 0xFFFF
    if not 1 <= kind <= 18:
        return False
    if raw >> 16:                      # small data element：数据就在 tag 里
        return True
    size = struct.unpack_from("<I", buf, pos + 4)[0]
    return pos + 8 + size <= len(buf)


def _next_pos(buf, start: int, size: int) -> int:
    """下一个元素的位置：先按 MATLAB 规格（8 字节对齐），不行再按 TinyMAT（不对齐）。"""
    padded = start + ((size + 7) // 8) * 8
    if _plausible(buf, padded):
        return padded
    if _plausible(buf, start + size):
        return start + size
    return padded


def _read_tag(buf, pos: int):
    """→ ``(类型, 字节数, 数据起点, 下一个元素的起点, 是否小元素)``。"""
    if pos + 8 > len(buf):
        raise ValueError(
            "MAT 文件在元素标签处截断了。下一步：重新从控制器或共享目录拷一份完整文件。"
        )
    raw = struct.unpack_from("<I", buf, pos)[0]
    if raw >> 16:
        return raw & 0xFFFF, raw >> 16, pos + 4, pos + 8, True
    kind = raw & 0xFFFF
    size = struct.unpack_from("<I", buf, pos + 4)[0]
    start = pos + 8
    if start + size > len(buf):
        raise ValueError(
            f"MAT 文件不完整：字节 {pos} 处的元素声明了 {size} 字节，但文件在这里结束了。"
            "下一步：重新从控制器或共享目录拷一份完整文件。"
        )
    return kind, size, start, _next_pos(buf, start, size), False


def _parse_matrix(buf, start: int, size: int):
    """一个 ``miMATRIX`` → ``(名字, 类, 维度, 实数块)``。

    实数块是 ``(类型, 字节数, 起点, 所在的缓冲区)``，解析阶段只记偏移、不另造一份
    数组；:func:`read_variables` 随后才把每条通道复制成 ``float64`` 装配场次。
    """
    pos, end = start, start + size
    dims: list[int] = []
    name = ""
    cls = None
    real = None
    while pos + 8 <= end:
        kind, n, data, nxt, _small = _read_tag(buf, pos)
        if kind == 6 and cls is None:
            cls = struct.unpack_from("<I", buf, data)[0] & 0xFF
        elif kind == 5:
            dims = list(struct.unpack_from("<%di" % (n // 4), buf, data))
        elif kind in (1, 16, 17) and not name:
            name = bytes(buf[data:data + n]).decode("latin-1", "replace").split("\0")[0]
        elif kind in _DTYPES:
            real = (kind, n, data, buf)
            break
        pos = nxt
    return name, cls, dims, real


def _walk(buf, out: list) -> None:
    pos = 0
    while pos < len(buf):
        if pos + 8 > len(buf):
            if any(buf[pos:]):
                raise ValueError(
                    f"MAT 文件在字节 {pos} 处截断了，末尾不足一个元素标签。"
                    "下一步：重新从控制器或共享目录拷一份完整文件。"
                )
            return
        kind, size, data, nxt, _small = _read_tag(buf, pos)
        if kind == 15:                                  # miCOMPRESSED
            try:
                inner = zlib.decompress(buf[data:data + size])
            except zlib.error as exc:
                raise ValueError(
                    "这段数据解不开（zlib）：文件可能没传完整。下一步：重新从记录仪/"
                    "共享目录拷一份再试。"
                ) from exc
            _walk(inner, out)
        elif kind == 14:                                # miMATRIX
            out.append(_parse_matrix(buf, data, size))
        pos = nxt


def read_variables(path: str | Path) -> tuple[list[tuple[str, np.ndarray]], dict]:
    """读一份 MAT v5，返回 ``[(变量名, 一维数组)]`` 与一份事实报告。

    只认数值矩阵。``struct`` / ``cell`` / ``char`` 遇到就报错并说下一步——静默跳过
    会让"少了一条通道"变成没人解释的事。
    """
    path = Path(path)
    blob = path.read_bytes()
    if blob.startswith(_HDF5):
        raise ValueError(
            f"{path.name} 是 MATLAB v7.3（HDF5）文件，不是 v5。"
            "下一步：在 MATLAB 里用 `save(..., '-v7')` 另存一份，或让控制器按 v5 导出。"
        )
    if not blob.startswith(_HEADER):
        raise ValueError(
            f"{path.name} 不像 MATLAB v5 文件（头 20 字节是 {blob[:20]!r}）。"
            "下一步：确认它是控制器导出的 .mat，不是原始 .bin。"
        )
    found: list = []
    _walk(memoryview(blob)[128:], found)
    if not found:
        raise ValueError(
            f"{path.name} 里一个变量都没有。下一步：用 MATLAB 打开确认文件没坏，"
            "或重新从控制器导出。"
        )
    pairs: list[tuple[str, np.ndarray]] = []
    seen: dict[str, int] = {}
    duplicates: list[dict] = []
    skipped: list[dict] = []
    for name, cls, dims, real in found:
        if cls not in _NUMERIC_CLASSES or real is None:
            skipped.append({"name": name or "(无名)", "class": _CLASSES.get(cls, str(cls)),
                            "dims": dims})
            continue
        kind, nbytes, offset, source = real
        dtype = np.dtype(_DTYPES[kind])
        count = nbytes // dtype.itemsize
        if count == 0:
            skipped.append({"name": name or "(无名)", "class": _CLASSES.get(cls, str(cls)),
                            "dims": dims})
            continue
        values = np.frombuffer(bytes(source[offset:offset + nbytes]),
                               dtype=dtype, count=count)
        if dims:
            shape = 1
            for d in dims:
                shape *= max(1, int(d))
            if shape == values.size:
                values = values.reshape(tuple(int(d) for d in dims), order="F")
        values = np.asarray(values, dtype=np.float64).ravel(order="F")
        base = (name or "unnamed").strip() or "unnamed"
        count_seen = seen.get(base, 0) + 1
        seen[base] = count_seen
        clean = base if count_seen == 1 else f"{base}_{count_seen}"
        if count_seen > 1:
            duplicates.append({"source": base, "renamed": clean})
        pairs.append((clean, values))
    if skipped:
        first = skipped[0]
        raise ValueError(
            f"{path.name} 里有 {len(skipped)} 个变量不是数值矩阵（第一条："
            f"{first['name']}，类型 {first['class']}，维度 {first['dims']}）。"
            "下一步：在 MATLAB 里把这些变量展开存成数值矩阵再导出。"
        )
    info = {
        "variables": len(pairs),
        "duplicates": duplicates,
        "sha256": hashlib.sha256(blob).hexdigest(),
        "bytes": len(blob),
    }
    return pairs, info


def _factor(native_rate: float, rate: float) -> int:
    if rate <= 0 or native_rate <= 0:
        raise ValueError(
            f"采样率要是正数，收到原生 {native_rate!r} / 导入 {rate!r}。"
            "下一步：例如 `--rate 100`（默认）或 `--rate 500`（原生）。"
        )
    factor = native_rate / rate
    rounded = int(round(factor))
    if rounded < 1 or abs(factor - rounded) > 1e-6:
        raise ValueError(
            f"原生 {native_rate:g} Hz 抽不到 {rate:g} Hz——只支持整数倍。"
            "下一步：用 500 / 250 / 125 / 100 / 50 / 25 这类能整除的档位，"
            "或者把 --native-rate 改成实际的原生采样率。"
        )
    return rounded


def _time_variable(pairs):
    """``t`` / ``Time`` / ``time_s`` —— 逆变器日志自带的时间轴（秒）。"""
    for wanted in ("t", "time", "time_s", "timestamp"):
        for name, values in pairs:
            if str(name).strip().lower() == wanted and values.size > 2:
                return name, values
    return None, None


def _measured_rate(time: np.ndarray) -> tuple[float, float]:
    """→ ``(中位采样率, 中位间隔)``；取中位是为了不吃掉偶发的丢帧/抖动。"""
    steps = np.diff(np.asarray(time, dtype=np.float64))
    steps = steps[np.isfinite(steps) & (steps > 0)]
    if not steps.size:
        return 0.0, 0.0
    step = float(np.median(steps))
    return (1.0 / step if step > 0 else 0.0), step


def read_mat_session(
    path: str | Path,
    *,
    rate: float = DEFAULT_RATE_HZ,
    native_rate: float | None = None,
    renames: dict[str, str] | None = None,
    units: dict[str, str] | None = None,
):
    """把一份逆变器 ``.mat`` 读成场次（与 CSV / Excel 同一个 ``CsvSession``）。

    时间轴用文件里那条 ``t``（秒）。``rate`` 是**导入采样率**：按整数倍抽点，
    不是插值——所以保留下来的每个时刻都是真实采样时刻（有抖动就有抖动）。
    文件里没有 ``t`` 时才退回 ``native_rate``（默认 500 Hz）去声明时间基。
    """
    path = Path(path)
    pairs, info = read_variables(path)
    _t_name, time_values = _time_variable(pairs)
    measured, median_step = _measured_rate(time_values) if time_values is not None else (0.0, 0.0)
    if native_rate:
        native = float(native_rate)
    else:
        native = measured or NATIVE_RATE_HZ
    if median_step > 0 and not native_rate:
        # 有真实时间轴：按"一个输出间隔里放得下几个原始点"取整数倍。抖动不影响
        # 这个数——中位间隔 2.00 ms、要 100 Hz 时就是 round(1/(0.002×100)) = 5。
        factor = max(1, int(round(1.0 / (median_step * float(rate)))))
    else:
        factor = _factor(native, float(rate))

    lengths = {len(values) for _name, values in pairs}
    shortest = min(lengths)
    trimmed = [name for name, values in pairs if len(values) != shortest]

    names: list[str] = []
    columns: dict[str, np.ndarray] = {}
    for name, values in pairs:
        unit = guess_unit(name)
        label = f"{name} [{unit}]" if unit else name
        if label in columns:                      # 猜出来的单位碰巧撞名（极少）
            label = f"{name} ({len(columns) + 1})"
        names.append(label)
        columns[label] = values[:shortest][::factor]
    frame = pd.DataFrame(columns)

    session = csvlog.session_from_frame(
        path, [names], frame, renames=renames, units=units,
        fmt="mat", generate_rate=float(rate),
    )
    session.device = "Ephorus 逆变器"
    session.header.update({
        "mat_native_rate": native,
        "mat_measured_rate": measured,
        "mat_rate": float(rate),
        "mat_decimation": factor,
        "mat_time_column": _t_name or "",
        "mat_variables": info["variables"],
        "mat_duplicates": info["duplicates"],
        "mat_trimmed": trimmed,
        "mat_sha256": info["sha256"],
        "mat_bytes": info["bytes"],
        "rate_from": "逆变器日志（声明值）",
    })
    return session
