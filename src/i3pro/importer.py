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

import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Iterable

from . import containers

__all__ = ["ALLOWED_SUFFIXES", "safe_name", "unique_target", "store_stream", "import_paths",
           "bring", "stage_stream", "staging_dir", "sweep_staging", "STAGING_PREFIX"]

#: ``.ld`` plus its ``.ldx`` sidecar (layers, beacons) is the primary source.
#: ``.csv`` is accepted too: i2 Pro exports and other teams' / other tools'
#: tables both go through the same session model, so ``.csv`` is imported, not
#: treated as a second-class citizen.
#: ``.xlsx`` joined them in ticket #31 (读 Excel 成场次),同样进侧边栏。
#: ``.txt`` / ``.tsv`` 是分隔文本（ticket #32）：别的工具导出的表多半长这样。
#:
#: 这里列的是**格式**后缀，**不含** ``.gz``：判断名字走
#: :func:`i3pro.containers.is_importable`（ADR-0012），纯文本容器允许带压缩后缀
#: （``X.csv.gz``）——"压缩后缀 + 格式后缀"要一起剥掉才算数，直接用
#: ``Path(name).suffix`` 只会看到 ``.gz``，于是把一份好好的 CSV 挡在门外。
ALLOWED_SUFFIXES = (".ld", ".ldx", ".csv", ".xlsx", ".txt", ".tsv", ".mat")

#: 导入预览的暂存目录前缀。暂存在 ``%TEMP%`` 而不是数据根目录里：场次库会**递归**
#: 扫 ``*.csv``，把还没确认的文件放在它眼皮底下就会先冒出一个"半成品场次"。
STAGING_PREFIX = "i3pro-import-"


def safe_name(name: str) -> str:
    """Keep only the file name and refuse anything that is not a log file."""
    base = Path(str(name).replace("\\", "/")).name.strip()
    if not base or base in (".", ".."):
        raise ValueError("文件名为空")
    if base.startswith("~$"):
        raise ValueError(f"{base}: Office 临时文件")
    if not containers.is_importable(base):
        raise ValueError(
            f"只接受 {' / '.join(ALLOWED_SUFFIXES)}，收到 {base!r}。"
            "纯文本那三种带上 .gz 也算（例如 场次.csv.gz）；"
            ".ld / .xlsx 压了没用，先解压回原样。"
        )
    return base


def unique_target(directory: Path, name: str) -> Path:
    """Never silently overwrite: a clash gets ``-1``, ``-2``, ... appended."""
    target = directory / name
    if not target.exists():
        return target
    # 编号加在**主名**上，不是加在最后一段上（ADR-0012）：``X.csv.gz`` 撞名时该得到
    # ``X-1.csv.gz``，不是 ``X.csv-1.gz``——后者既不像容器，也会让侧边栏多出一场。
    stem = containers.session_name(name)
    tail = str(name)[len(stem):]
    for n in range(1, 1000):
        candidate = directory / f"{stem}-{n}{tail}"
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
    return {"file": target.name, "stem": containers.session_name(target),
            "bytes": written, "path": str(target)}


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
        "stem": containers.session_name(target),
        "bytes": target.stat().st_size,
        "path": str(target),
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
                if child.is_file() and containers.is_importable(child)
            ]
            if not candidates:
                suffixes = "/".join(ALLOWED_SUFFIXES)
                results.append({
                    "source": str(path),
                    "error": f"这个目录里没有 {suffixes}；下一步：把日志放进这个目录，"
                             "或直接把文件（不是文件夹）拖到 导入数据.bat 上",
                })
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


# --------------------------------------------------------------- 导入预览暂存
def bring(path: str | Path, directory: str | Path, move: bool = True) -> dict:
    """把一份已经在磁盘上的文件搬进数据目录（:func:`_bring` 的公开名字）。"""
    return _bring(Path(path), Path(directory), move)


def staging_dir(token: str) -> Path:
    """``token`` 对应的暂存目录。token 只认十六进制——它来自 URL，不能当路径用。"""
    clean = str(token).strip().lower()
    if not clean or any(c not in "0123456789abcdef" for c in clean):
        raise ValueError(f"暂存编号不合法：{token!r}。下一步：重新选一次文件再导入。")
    return Path(tempfile.gettempdir()) / (STAGING_PREFIX + clean)


def stage_stream(name: str, stream: BinaryIO, length: int | None = None) -> dict:
    """把上传的字节写进暂存目录，返回 ``{token, name, path, bytes}``。

    预览阶段（选分隔符 / 表头行 / 编码）读的就是这份暂存文件；用户点"导入"才
    :func:`bring` 进数据目录。取消（或服务重启）留下的目录由
    :func:`sweep_staging` 收掉。
    """
    token = secrets.token_hex(8)
    directory = staging_dir(token)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        info = store_stream(directory, name, stream, length)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return {"token": token, **info}


def sweep_staging(max_age: float = 3600.0, now: float | None = None) -> int:
    """删掉超过 ``max_age`` 秒没动过的暂存目录（服务启动时扫一次）。"""
    root = Path(tempfile.gettempdir())
    if not root.exists():
        return 0
    stamp = time.time() if now is None else now
    removed = 0
    for child in root.glob(STAGING_PREFIX + "*"):
        try:
            if not child.is_dir() or stamp - child.stat().st_mtime < max_age:
                continue
        except OSError:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed
