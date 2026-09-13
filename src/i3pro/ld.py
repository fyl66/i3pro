"""Native reader for MoTeC ``.ld`` / ``.ldx`` logger files.

The container layout below was reverse engineered from the team's own C125 logs
and validated sample-by-sample against the matching MoTeC CSV exports
(see ``tools/verify_ld_vs_csv.py``).

File layout
-----------
``header`` (64 bytes, little endian)::

    0x00 u32 header_size            always 0x40
    0x04 u32 event_ptr              -> event record (see below)
    0x08 u32 first_channel_ptr      -> head of the channel linked list
    0x0c u32 channel_table_end      == offset of the first sample block
    0x24 u32 aux_ptr                (unknown auxiliary block)
    0x2c u32 aux_ptr_2              (unknown auxiliary block)
    0x40 ..                         device / date / time strings

``event record`` (at ``event_ptr``)::

    0x00 u32 id_hash
    0x04 u32 venue_ptr
    0x08 u32 session_hash
    0x0c u16 default_sample_rate
    0x10 u32 details_ptr
    0x14 char[32] event name

``channel record`` (124 bytes each, doubly linked list)::

    0x00 u32 prev_ptr
    0x04 u32 next_ptr
    0x08 u32 data_ptr        -> first sample of this channel
    0x0c u32 sample_count
    0x10 u16 channel_id
    0x12 u16 data_type       3 = int16, 5 = int32
    0x14 u16 bytes_per_sample
    0x16 u16 sample_rate     [Hz]
    0x18 u16 shift           (always 0 in every log we inspected)
    0x1a u16 multiplier
    0x1c u16 divider
    0x1e i16 decimals        display precision; -1 means "x10" (0xffff in the file)
    0x20 char[32] name
    0x40 char[8]  short_name
    0x48 char[24] unit
    0x60 ..                          (spare / display range)

Sample values are recovered with::

    engineering_value = raw * multiplier / divider / 10 ** decimals

(``decimals`` is signed: the C125 writes ``0xffff`` for a -1 exponent, which is
how ``Timestamp MTI`` ends up in units of 10 us and how the inverter
``TargetVelocity`` channels end up in rpm.)

which reproduces the MoTeC CSV export exactly for every 100 Hz channel
(max abs error 0.0 for the channels that are not resampled by the CSV writer).
"""

from __future__ import annotations

import mmap
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np

HEADER_SIZE = 0x40
CHANNEL_RECORD_SIZE = 124

DATA_TYPE_INT16 = 3
DATA_TYPE_INT32 = 5

_INT16_TYPES = {0, 1, 3}
_INT32_TYPES = {2, 4, 5}


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("latin-1").strip()


@dataclass(frozen=True)
class Channel:
    """One logged channel and its scaling information."""

    name: str
    short_name: str
    unit: str
    sample_rate: float
    sample_count: int
    data_offset: int
    data_type: int
    bytes_per_sample: int
    multiplier: int
    divider: int
    decimals: int
    shift: int
    channel_id: int
    index: int

    @property
    def dtype(self) -> np.dtype:
        if self.bytes_per_sample == 4 or self.data_type in _INT32_TYPES:
            return np.dtype("<i4")
        return np.dtype("<i2")

    @property
    def scale(self) -> float:
        divider = self.divider or 1
        exponent = max(-9, min(12, self.decimals))  # hostile values are clamped
        return (self.multiplier / divider) / (10.0**exponent)

    @property
    def duration(self) -> float:
        return (self.sample_count - 1) / self.sample_rate if self.sample_count else 0.0

    def time(self) -> np.ndarray:
        """Channel time base in seconds, starting at 0."""
        return np.arange(self.sample_count, dtype=np.float64) / self.sample_rate

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"Channel({self.name!r}, unit={self.unit!r}, "
            f"{self.sample_rate:g}Hz, n={self.sample_count})"
        )


@dataclass
class LogFile:
    """A parsed ``.ld`` file. Sample blocks are memory mapped, not copied."""

    path: Path
    header: dict
    device: str
    log_date: str
    log_time: str
    event_name: str
    channels: list[Channel]
    _buffer: mmap.mmap | None = field(default=None, repr=False, compare=False)
    #: 数学通道算出来的列（见 ``maths.py``）。键为通道名，值是主时间基上的
    #: float64 序列。它们没有 mmap 后备，所以 ``raw()`` 会拒绝而不是读出垃圾。
    derived: dict[str, np.ndarray] = field(default_factory=dict, repr=False, compare=False)

    # ------------------------------------------------------------------ load
    @classmethod
    def read(cls, path: str | Path) -> "LogFile":
        path = Path(path)
        with path.open("rb") as fh:
            buf = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            log = cls._parse(path, buf)
        except Exception:
            buf.close()
            raise
        log._buffer = buf
        return log

    @classmethod
    def _parse(cls, path: Path, buf: mmap.mmap) -> "LogFile":
        words = struct.unpack_from("<16I", buf, 0)
        if words[0] != HEADER_SIZE:
            raise ValueError(
                f"{path.name}: not a MoTeC .ld file (header_size={words[0]:#x})"
            )
        header = {
            "header_size": words[0],
            "event_ptr": words[1],
            "first_channel_ptr": words[2],
            "channel_table_end": words[3],
            "aux_ptr": words[9],
            "aux_ptr_2": words[11],
            "file_size": len(buf),
        }
        device, log_date, log_time = cls._header_strings(buf)
        event_name = _cstr(buf[words[1] + 0x14 : words[1] + 0x34]) if words[1] else ""
        channels = list(cls._walk_channels(buf, words[2]))
        return cls(
            path=path,
            header=header,
            device=device,
            log_date=log_date,
            log_time=log_time,
            event_name=event_name,
            channels=channels,
        )

    @staticmethod
    def _header_strings(buf: mmap.mmap) -> tuple[str, str, str]:
        head = bytes(buf[0x40:0x140])
        runs = [m.group(0).decode("latin-1") for m in re.finditer(rb"[ -~]{3,}", head)]
        device = next((r for r in runs if not re.match(r"[\d:/]", r)), "")
        date = next((r for r in runs if re.fullmatch(r"\d{2}/\d{2}/\d{4}", r)), "")
        time = next((r for r in runs if re.fullmatch(r"\d{2}:\d{2}:\d{2}", r)), "")
        return device, date, time

    @staticmethod
    def _walk_channels(buf: mmap.mmap, ptr: int) -> Iterator[Channel]:
        seen: set[int] = set()
        index = 0
        total = len(buf)
        while ptr and ptr not in seen and 0 < ptr < total - CHANNEL_RECORD_SIZE:
            seen.add(ptr)
            _prev, nxt = struct.unpack_from("<II", buf, ptr)
            data_ptr, sample_count = struct.unpack_from("<II", buf, ptr + 0x08)
            (channel_id, data_type, bytes_per_sample, sample_rate, shift) = (
                struct.unpack_from("<HHHHH", buf, ptr + 0x10)
            )
            multiplier, divider = struct.unpack_from("<HH", buf, ptr + 0x1A)
            decimals = struct.unpack_from("<h", buf, ptr + 0x1E)[0]
            name = _cstr(bytes(buf[ptr + 0x20 : ptr + 0x40]))
            short_name = _cstr(bytes(buf[ptr + 0x40 : ptr + 0x48]))
            unit = _cstr(bytes(buf[ptr + 0x48 : ptr + 0x60]))
            yield Channel(
                name=name,
                short_name=short_name,
                unit=unit,
                sample_rate=float(sample_rate) or 1.0,
                sample_count=int(sample_count),
                data_offset=int(data_ptr),
                data_type=int(data_type),
                bytes_per_sample=int(bytes_per_sample),
                multiplier=int(multiplier),
                divider=int(divider),
                decimals=int(decimals),
                shift=int(shift),
                channel_id=int(channel_id),
                index=index,
            )
            ptr = nxt
            index += 1

    # ------------------------------------------------------------- accessors
    @property
    def sample_rate(self) -> float:
        """Master (fastest) sample rate of the log."""
        return max((c.sample_rate for c in self.channels), default=0.0)

    @property
    def duration(self) -> float:
        return max((c.duration for c in self.channels), default=0.0)

    @property
    def buffer(self) -> mmap.mmap:
        if self._buffer is None:
            raise RuntimeError(f"{self.path.name} has been closed")
        return self._buffer

    def channel(self, name: str) -> Channel:
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

    def raw(self, name: str | Channel) -> np.ndarray:
        """Raw integer samples, no scaling applied."""
        ch = self.channel(name) if isinstance(name, str) else name
        if ch.name in self.derived:
            raise ValueError(
                f"{ch.name} 是数学通道算出来的，没有原始整数样本。"
                f"要它的数值请用 values()。"
            )
        return np.frombuffer(
            self.buffer, dtype=ch.dtype, count=ch.sample_count, offset=ch.data_offset
        )

    def values(self, name: str | Channel) -> np.ndarray:
        """Samples converted to engineering units (float64)."""
        ch = self.channel(name) if isinstance(name, str) else name
        cached = self.derived.get(ch.name)
        if cached is not None:
            return cached
        raw = self.raw(ch)
        if ch.scale == 1.0:
            return raw.astype(np.float64)
        return raw.astype(np.float64) * ch.scale

    def close(self) -> None:
        if self._buffer is not None:
            try:
                self._buffer.close()
            except BufferError:
                # numpy views handed out by raw()/values() still reference the
                # mapping; it is released once those arrays are collected.
                pass
            self._buffer = None

    def __enter__(self) -> "LogFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ misc
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
            "file_size": self.header["file_size"],
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<LogFile {self.path.name} device={self.device!r} "
            f"{len(self.channels)} channels {self.duration:.1f}s>"
        )
