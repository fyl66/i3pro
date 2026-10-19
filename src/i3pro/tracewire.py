"""``/trace`` 的二进制载荷编解码（ticket #56 / 架构评审 C2）。

为什么单独一层：取数计算本身已经走 numpy，慢的是把 8000 桶 × 12 通道的 Python
列表再逐值 ``json.dumps``。这一层只做"已经算好的 trace 字典 ↔ 字节"的转换，
不碰数据语义，也不把协议细节漏进 :mod:`i3pro.api`。

帧格式（小端）：::

    magic  "I3TR"                  4 bytes
    header_len                     uint32
    header_json                    UTF-8，长度 header_len
    padding                        对齐到 8 字节
    arrays                         按 header 里的顺序连续存放

``header_json`` 只放通道名、单位、采样率、数组形状等小元数据；时间用 float64，
值 / min / max / 距离用 float32，count 用 int32。每个数组前都补到 8 字节边界，
浏览器可以直接用 ``Float64Array`` / ``Float32Array`` / ``Int32Array`` 建视图，
不用逐元素 ``DataView`` 读。
"""

from __future__ import annotations

import json
import struct
from typing import Mapping

import numpy as np

__all__ = ["CONTENT_TYPE", "MAGIC", "VERSION", "decode", "encode"]

#: 二进制响应类型；前端按它决定走 ArrayBuffer 还是 JSON。
CONTENT_TYPE = "application/x-i3pro-trace"
MAGIC = b"I3TR"
VERSION = 1

_HEADER = struct.Struct("<4sI")
_DTYPES = {
    "f8": np.dtype("<f8"),
    "f4": np.dtype("<f4"),
    "i4": np.dtype("<i4"),
}
_DTYPE_BY_NAME = {name: dtype for name, dtype in _DTYPES.items()}


def _align8(value: int) -> int:
    return (int(value) + 7) & ~7


def _array(values, dtype_name: str) -> np.ndarray:
    dtype = _DTYPE_BY_NAME[dtype_name]
    arr = np.asarray(values, dtype=dtype)
    return np.ascontiguousarray(arr.reshape(-1))


def _payload(trace: Mapping, key: str):
    """Read an optional array without using truthiness (numpy arrays raise)."""
    value = trace.get(key)
    return [] if value is None else value


def encode(traces: Mapping[str, Mapping]) -> bytes:
    """把 ``render.trace`` 的结果编成自描述二进制帧。"""
    header = {"version": VERSION, "channels": []}
    arrays: list[np.ndarray] = []

    for name, trace in traces.items():
        channel = {
            "name": str(name),
            "unit": str(trace.get("unit") or ""),
            "rate": trace.get("rate"),
            "upsample": trace.get("upsample"),
            "envelope": bool(trace.get("envelope")),
            "samples": int(trace.get("samples") or 0),
            "buckets": int(trace.get("buckets") or 0),
            "arrays": [],
        }
        if "span" in trace:
            channel["span"] = [float(v) for v in trace["span"]]
        if "distance_span" in trace:
            channel["distance_span"] = [float(v) for v in trace["distance_span"]]

        def add(key: str, dtype_name: str, values) -> None:
            arr = _array(values, dtype_name)
            channel["arrays"].append({
                "key": key,
                "dtype": dtype_name,
                "length": int(arr.size),
            })
            arrays.append(arr)

        add("time", "f8", _payload(trace, "time"))
        add("value", "f4", _payload(trace, "value"))
        if "min" in trace:
            add("min", "f4", _payload(trace, "min"))
        if "max" in trace:
            add("max", "f4", _payload(trace, "max"))
        if "count" in trace:
            add("count", "i4", _payload(trace, "count"))
        if "distance" in trace:
            add("distance", "f4", _payload(trace, "distance"))
        header["channels"].append(channel)

    text = json.dumps(
        header, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    body = bytearray(_HEADER.pack(MAGIC, len(text)))
    body.extend(text)
    while len(body) % 8:
        body.append(0)
    for arr in arrays:
        while len(body) % 8:
            body.append(0)
        body.extend(arr.tobytes(order="C"))
    return bytes(body)


def decode(body: bytes) -> dict[str, dict]:
    """解码 :func:`encode` 的帧；给单测当独立裁判，生产路径不走它。"""
    if len(body) < _HEADER.size:
        raise ValueError("取数二进制帧太短：连头都没有。")
    magic, header_len = _HEADER.unpack_from(body)
    if magic != MAGIC:
        raise ValueError(f"取数二进制帧的 magic 不对：{magic!r}")
    start = _HEADER.size
    end = start + int(header_len)
    if end > len(body):
        raise ValueError("取数二进制帧的头长度超过了正文。")
    try:
        header = json.loads(body[start:end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"取数二进制帧的头不是合法 JSON：{exc}") from exc
    if int(header.get("version") or 0) != VERSION:
        raise ValueError(f"取数二进制帧的版本不认识：{header.get('version')!r}")

    out: dict[str, dict] = {}
    offset = _align8(end)
    for channel in header.get("channels", []):
        name = str(channel.get("name") or "")
        trace = {
            "unit": str(channel.get("unit") or ""),
            "rate": channel.get("rate"),
            "upsample": channel.get("upsample"),
            "envelope": bool(channel.get("envelope")),
            "samples": int(channel.get("samples") or 0),
            "buckets": int(channel.get("buckets") or 0),
        }
        if "span" in channel:
            trace["span"] = [float(v) for v in channel["span"]]
        if "distance_span" in channel:
            trace["distance_span"] = [float(v) for v in channel["distance_span"]]
        for descriptor in channel.get("arrays", []):
            dtype_name = str(descriptor.get("dtype") or "")
            dtype = _DTYPE_BY_NAME.get(dtype_name)
            if dtype is None:
                raise ValueError(f"取数二进制帧里有不认识的 dtype：{dtype_name!r}")
            offset = _align8(offset)
            count = int(descriptor.get("length") or 0)
            end = offset + count * dtype.itemsize
            if end > len(body):
                raise ValueError("取数二进制帧的数组长度超过了正文。")
            trace[str(descriptor.get("key"))] = np.frombuffer(
                body, dtype=dtype, count=count, offset=offset
            ).copy()
            offset = end
        out[name] = trace
    return out
