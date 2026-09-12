"""Reader for MoTeC CSV exports (the file i2 Pro / C125 Manager produce).

Layout: quoted metadata key/value rows, blank lines, a channel-name row, a unit
row, then the numeric rows. The encoding is usually the Windows code page
(``gbk`` for the logs made on the team laptops), so decoding is attempted with
``utf-8-sig`` first and falls back to ``gbk`` and ``latin-1``.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_ENCODINGS = ("utf-8-sig", "gbk", "cp936", "latin-1")


@dataclass
class MotecCsv:
    path: Path
    metadata: dict[str, str]
    channels: list[str]
    units: list[str]
    frame: pd.DataFrame

    @property
    def sample_rate(self) -> float | None:
        try:
            return float(self.metadata.get("Sample Rate", ""))
        except ValueError:
            return None

    @property
    def duration(self) -> float | None:
        try:
            return float(self.metadata.get("Duration", ""))
        except ValueError:
            return None

    def values(self, channel: str) -> np.ndarray:
        return self.frame[channel].to_numpy(dtype=np.float64, copy=False)


def _open_text(path: Path):
    for enc in _ENCODINGS:
        try:
            with path.open("r", encoding=enc, newline="") as fh:
                head = fh.read(8192)
            if "\ufffd" not in head:
                return path.open("r", encoding=enc, newline="")
        except UnicodeDecodeError:
            continue
    return path.open("r", encoding="latin-1", newline="")


def read_structure(path: str | Path) -> tuple[dict[str, str], list[str], list[str], int]:
    """Return (metadata, channel names, units, first data row index)."""
    path = Path(path)
    with _open_text(path) as fh:
        reader = csv.reader(fh)
        meta: dict[str, str] = {}
        names: list[str] = []
        units: list[str] = []
        for i, row in enumerate(reader):
            if not row:
                continue
            if row[0] == "Time":
                names = row
                units = next(reader, [])
                return meta, names, units, i + 2
            cells = [c for c in row if c != ""]
            for k in range(0, len(cells) - 1, 2):
                meta.setdefault(cells[k], cells[k + 1])
    raise ValueError(f"{path.name}: no channel header row found")


def load(
    path: str | Path,
    channels: list[str] | None = None,
    max_rows: int | None = None,
) -> MotecCsv:
    """Load a MoTeC CSV export into a :class:`MotecCsv`."""
    path = Path(path)
    meta, names, units, _ = read_structure(path)
    wanted = None
    if channels:
        wanted = {"Time", *channels}
        missing = wanted.difference(names)
        if missing:
            raise KeyError(f"columns not present in {path.name}: {sorted(missing)}")
    keep_idx = [i for i, n in enumerate(names) if wanted is None or n in wanted]
    keep_names = [names[i] for i in keep_idx]
    columns: dict[str, list[float]] = {n: [] for n in keep_names}
    with _open_text(path) as fh:
        reader = csv.reader(fh)
        header_seen = False
        rows = 0
        for row in reader:
            if not header_seen:
                if row and row[0] == "Time":
                    header_seen = True
                continue
            if not row or row[0] == "":
                if header_seen and all(c == "" for c in row):
                    continue
                continue
            if not header_seen:
                continue
            if row[0] == "s" or (row and not _is_number(row[0])):
                continue  # unit row
            for i, n in zip(keep_idx, keep_names):
                cell = row[i] if i < len(row) else ""
                columns[n].append(float(cell) if cell not in ("", None) else np.nan)
            rows += 1
            if max_rows and rows >= max_rows:
                break
    frame = pd.DataFrame(columns)
    return MotecCsv(path=path, metadata=meta, channels=keep_names, units=units, frame=frame)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


def to_csv(buffer: io.StringIO) -> None:  # pragma: no cover - helper for tests
    buffer.seek(0)
