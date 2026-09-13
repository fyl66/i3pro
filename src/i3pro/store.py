"""Columnar storage layer: ``.ld`` -> Parquet (+ metadata JSON) and SQL queries.

The Parquet table is a wide table sampled on the log's master time base::

    time | <channel 1> | <channel 2> | ...

Slow channels are held (sample & hold) onto the master grid, which is exactly
what the MoTeC CSV export does, so both sources share one schema.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import channels as channelsmod
from . import ld as ldmod

__all__ = [
    "write_parquet",
    "read_metadata",
    "read_table",
    "load_frame",
    "read_series",
    "query",
]


def _slug(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_]+", "_", text).strip("_").lower() or "channel"


def _resample(values: np.ndarray, factor: int) -> np.ndarray:
    if factor == 1:
        return values
    return np.repeat(values, factor)


def build_table(
    log: ldmod.LogFile,
    channels: list[str] | None = None,
    master_rate: float | None = None,
) -> tuple[pa.Table, dict]:
    """Materialise a log into a wide Arrow table on the master time base."""
    rate = float(master_rate or log.sample_rate)
    selected = [log.channel(n) for n in channels] if channels else list(log.channels)
    n = int(round(log.duration * rate)) + 1
    arrays: dict[str, pa.Array] = {}
    meta_channels = []
    used: set[str] = set()
    for ch in selected:
        # 目标时间基可能是 --rate 给的那一档，但「数学通道已经在主时间基上」这条规则
        # 仍然只由 channels.py 回答（ticket #18）。
        factor = channelsmod.hold_factor(log, ch, rate)
        values = log.values(ch)
        if factor > 1:
            values = _resample(values, factor)
        values = values[:n]
        if values.size < n:  # pad short channels (rare, e.g. last sample missing)
            values = np.concatenate([values, np.full(n - values.size, np.nan)])
        name = ch.name if ch.name not in used else f"{ch.name} ({ch.channel_id})"
        used.add(name)
        arrays[name] = pa.array(values.astype(np.float32))
        meta_channels.append(
            {
                "name": name,
                "unit": ch.unit,
                "sample_rate": ch.sample_rate,
                "native_samples": ch.sample_count,
                "scale": ch.scale,
                "decimals": ch.decimals,
                "channel_id": ch.channel_id,
                "column": _slug(name),
            }
        )
    time = np.arange(n, dtype=np.float64) / rate
    table = pa.table({"time": pa.array(time.astype(np.float64)), **arrays})
    meta = {
        "source": log.path.name,
        "format": "motec-ld",
        "device": log.device,
        "log_date": log.log_date,
        "log_time": log.log_time,
        "event": log.event_name,
        "sample_rate": rate,
        "duration": log.duration,
        "rows": n,
        "column_count": len(arrays),
        "channels": meta_channels,
    }
    return table, meta


def write_parquet(
    log: ldmod.LogFile,
    outdir: str | Path,
    channels: list[str] | None = None,
    master_rate: float | None = None,
    compression: str = "zstd",
) -> tuple[Path, Path]:
    """Write ``<stem>.parquet`` + ``<stem>.meta.json``; returns both paths."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    table, meta = build_table(log, channels=channels, master_rate=master_rate)
    stem = Path(log.path).stem
    parquet_path = outdir / f"{stem}.parquet"
    meta_path = outdir / f"{stem}.meta.json"
    pq.write_table(table, parquet_path, compression=compression)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return parquet_path, meta_path


def read_metadata(parquet_path: str | Path) -> dict:
    path = Path(parquet_path).with_suffix(".meta.json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"metadata sidecar not found: {path}")


def read_table(path: str | Path) -> pa.Table:
    return pq.read_table(path)


def load_frame(path: str | Path, columns: list[str] | None = None):
    import pandas as pd

    return pq.read_table(path, columns=columns).to_pandas()


def read_series(
    path: str | Path,
    channels: list[str],
    start: float | None = None,
    end: float | None = None,
) -> "pd.DataFrame":
    """Fast path: read only the requested columns and time window.

    Parquet is columnar, so pulling two channels out of a 342 channel session
    touches a few percent of the file instead of all of it.
    """
    import pyarrow.compute as pc

    columns = ["time", *channels]
    filters = None
    if start is not None and end is not None:
        filters = [("time", ">=", float(start)), ("time", "<=", float(end))]
    elif start is not None:
        filters = [("time", ">=", float(start))]
    elif end is not None:
        filters = [("time", "<=", float(end))]
    table = pq.read_table(path, columns=columns, filters=filters)
    if filters is not None and table.num_rows == 0:
        # fall back to a compute filter (older pyarrow ignores row-group stats)
        table = pq.read_table(path, columns=columns)
        mask = pc.and_(
            pc.greater_equal(table["time"], float(start if start is not None else -np.inf)),
            pc.less_equal(table["time"], float(end if end is not None else np.inf)),
        )
        table = table.filter(mask)
    return table.to_pandas()


def query(sql: str, paths: list[str | Path]) -> "pd.DataFrame":
    """Run SQL over one or more Parquet datasets.

    Every dataset is registered as a table named after its file stem (all
    non-alphanumeric characters replaced by ``_``). A convenience view named
    ``log`` points at the first dataset.
    """
    import pandas as pd

    con = sqlite3.connect(":memory:")
    names = []
    for path in paths:
        path = Path(path)
        name = _slug(path.stem)
        frame = load_frame(path)
        frame.to_sql(name, con, index=False)
        names.append(name)
    if names:
        con.execute(f'CREATE VIEW log AS SELECT * FROM "{names[0]}"')
    try:
        return pd.read_sql_query(sql, con)
    finally:
        con.close()
