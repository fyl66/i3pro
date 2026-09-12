"""Getting logs into the data folder.

Three callers share this module so the rules stay identical everywhere:

* ``i3pro import`` - copy (or move) ``.ld`` / ``.ldx`` files from anywhere on
  disk into the data folder. ``导入数据.bat`` is a drag-and-drop wrapper.
* the workbench's upload endpoint - the browser sends the bytes of a file the
  user picked, and they land in the same place.
* future batch tooling.

Everything is written to ``<name>.part`` first and then renamed into position,
so a half-finished copy never appears in the session list as a broken log.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO, Iterable

__all__ = ["ALLOWED_SUFFIXES", "safe_name", "unique_target", "store_stream", "import_paths"]

#: MoTeC writes the log as ``.ld`` plus an optional ``.ldx`` sidecar (layers,
#: beacons). ``.csv`` exports are not imported: they are the comparison baseline,
#: not the primary data source.
ALLOWED_SUFFIXES = (".ld", ".ldx")


def safe_name(name: str) -> str:
    """Keep only the file name and refuse anything that is not a log file."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    if not base or base in (".", ".."):
        raise ValueError("文件名为空")
    if base.startswith("~$"):
        raise ValueError(f"{base}: Office 临时文件")
    if Path(base).suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError(f"只接受 {' / '.join(ALLOWED_SUFFIXES)}，收到 {base!r}")
    return base


def unique_target(directory: Path, name: str) -> Path:
    """Never silently overwrite: a clash gets ``-1``, ``-2``, ... appended."""
    target = directory / name
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for n in range(1, 1000):
        candidate = directory / f"{stem}-{n}{suffix}"
        if not candidate.exists():
            return candidate
    raise ValueError(f"{name}: 同名文件太多")


def store_stream(
    directory: Path,
    name: str,
    stream: BinaryIO,
    length: int | None = None,
    chunk_size: int = 1 << 20,
) -> dict:
    """Write exactly ``length`` bytes (or until EOF) into ``directory``."""
    directory.mkdir(parents=True, exist_ok=True)
    clean = safe_name(name)
    target = unique_target(directory, clean)
    temp = target.with_name(target.name + ".part")
    written = 0
    try:
        with temp.open("wb") as out:
            while length is None or written < length:
                want = chunk_size if length is None else min(chunk_size, length - written)
                chunk = stream.read(want)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
        if length is not None and written != length:
            raise ValueError(f"{clean}: 只收到 {written}/{length} 字节，上传中断")
        temp.replace(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return {"file": target.name, "stem": target.stem, "bytes": written, "path": str(target)}


def _bring(path: Path, directory: Path, move: bool) -> dict:
    clean = safe_name(path.name)
    target = unique_target(directory, clean)
    if move:
        shutil.move(str(path), str(target))
    else:
        shutil.copy2(path, target)
    return {
        "source": str(path),
        "file": target.name,
        "stem": target.stem,
        "bytes": target.stat().st_size,
    }


def import_paths(
    paths: Iterable[str | Path], directory: str | Path, move: bool = False
) -> list[dict]:
    """Import files, or everything log-shaped inside a folder, recursively."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            candidates = [
                child
                for child in sorted(path.rglob("*"))
                if child.is_file() and child.suffix.lower() in ALLOWED_SUFFIXES
            ]
            if not candidates:
                results.append({"source": str(path), "error": "这个目录里没有 .ld/.ldx"})
            for child in candidates:
                if child in seen:
                    continue
                seen.add(child)
                results.append(_one(child, directory, move))
            continue
        if path in seen:
            continue
        seen.add(path)
        results.append(_one(path, directory, move))
    return results


def _one(path: Path, directory: Path, move: bool) -> dict:
    try:
        if not path.is_file():
            raise ValueError("找不到文件")
        return _bring(path, directory, move)
    except (ValueError, OSError) as exc:
        return {"source": str(path), "error": str(exc)}
