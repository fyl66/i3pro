"""场次的**容器**：文件名里的格式后缀与压缩后缀（ADR-0012）。

一个场次的身份是它的**主名**，与用什么容器存放无关——``X.ld`` / ``X.csv`` /
``X.csv.gz`` 是同一场的三种容器。所以"这份文件叫什么 / 是什么格式 / 要不要先解压"
这三件事只能有一处答案，就是这里：侧车路径（:mod:`i3pro.sidecar`）、场次库名字
（:mod:`i3pro.library`）、导入白名单（:mod:`i3pro.importer`）、命令行与 HTTP 的
后缀判断都从这里取。散着写的话，"``X.csv.gz`` 这场叫什么"迟早各处不一样，而它
是侧边栏名字、侧车文件名、导出文件名三处共用的东西。

**只有纯文本容器能压**（实测）：``.csv`` / ``.txt`` / ``.tsv`` 是数字写成的文本，
14 万行 × 520 列的合并场次 290 MB 压到 46 MB（12%），解压 453 MB/s——比解析这
张表本身还快。``.ld`` 是二进制、``.xlsx`` 本身就是 zip，再套 gzip 只会更慢更大，
这类容器明确拒绝，并把"这个格式压了没用"写进报错里。

帧表**不在**压缩范围内（ADR-0012）：场次库启动时会对每份 ``.csv`` 做内容嗅探
（:func:`i3pro.canlog.looks_like_frames`），压缩帧表会让这趟扫描变成逐份解压。
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import IO

__all__ = [
    "COMPRESSIBLE", "COMPRESSION", "IMPORTABLE", "SESSION_FORMATS",
    "container_tag", "format_suffix", "is_compressed", "is_importable",
    "is_session", "open_binary", "open_text", "session_name", "split_container",
    "with_compression",
]

#: 能当"场次容器"的格式后缀。``.ldx`` 不在里面：它跟着 ``.ld`` 走，自己不是场次。
SESSION_FORMATS = (".ld", ".csv", ".xlsx", ".txt", ".tsv", ".mat")

#: 导入白名单：场次容器 + ``.ldx``（MoTeC 的索引文件，跟着 ``.ld`` 一起拷）。
IMPORTABLE = SESSION_FORMATS + (".ldx",)

#: 值得压的容器（ADR-0012）。不在这张表里的格式被拒绝，而不是"压了但没人读得动"。
COMPRESSIBLE = (".csv", ".txt", ".tsv")

#: 压缩后缀。只认这一种：多认一种就多一条"谁先剥掉"的规矩，而 gzip 与 zstd 在这些
#: 文本上的差距远小于"换台机器解不开"的代价。
COMPRESSION = ".gz"

#: 剥后缀时认识的全部尾巴——场次容器 + ``.ldx``。**顺序即优先级**：长的先剥，
#: 否则 ``.csv.gz`` 会被当成"主名 ``X.csv`` + 压缩后缀"。见 :func:`split_container`。
_TAILS = SESSION_FORMATS + (".ldx",)


def split_container(path: str | Path) -> tuple[str, str, str]:
    """``X.csv.gz`` -> ``("X", ".csv", ".gz")``；``X.ld`` -> ``("X", ".ld", "")``。

    主名**只**剥掉"压缩后缀 + 格式后缀"这两层，其余点号原样留着：``a.b.csv`` 的主名
    是 ``a.b``。不能用"第一个点"或 ``Path.stem`` 来切——``Path("X.csv.gz").stem``
    是 ``X.csv``，那正是"侧边栏里这场叫 X.csv"的老毛病。

    认不出来的尾巴（``X.laps.json``、``报告.pdf``）一律原样返回，不猜。
    """
    name = Path(str(path).replace("\\", "/")).name
    compression = ""
    if name.lower().endswith(COMPRESSION):
        compression = name[len(name) - len(COMPRESSION):]
        name = name[: len(name) - len(COMPRESSION)]
    for tail in _TAILS:
        if name.lower().endswith(tail):
            return name[: len(name) - len(tail)], tail, compression.lower()
    return name, "", compression.lower()


def session_name(path: str | Path) -> str:
    """这个文件在场次库里叫什么名字（**主名**，格式与压缩后缀都不算）。"""
    return split_container(path)[0]


def format_suffix(path: str | Path) -> str:
    """容器格式后缀（``.csv`` / ``.ld`` …）；认不出来时是空串。"""
    return split_container(path)[1]


def compression_suffix(path: str | Path) -> str:
    """压缩后缀（``.gz``）；没压过就是空串。"""
    return split_container(path)[2]


def is_compressed(path: str | Path) -> bool:
    return bool(split_container(path)[2])


def is_session(path: str | Path) -> bool:
    """这份文件能不能当场次读（``.ldx`` 不能，它只是跟着 ``.ld`` 的索引）。"""
    return format_suffix(path) in SESSION_FORMATS


def is_importable(name: str | Path) -> bool:
    """导入时接不接受这个名字（含 ``.ldx``）。

    压过的名字只有**纯文本**那三种算数：``X.xlsx.gz`` / ``X.ld.gz`` 一律拒绝——
    收进来只会在侧边栏里变成一条"读不出来"，不如当场说清"解压回原样"。
    """
    _, fmt, compression = split_container(name)
    if fmt not in IMPORTABLE:
        return False
    return not compression or fmt in COMPRESSIBLE


def container_tag(path: str | Path) -> str:
    """侧边栏括号里那截：``X.csv.gz`` -> ``csv.gz``（同名容器并存时用来区分）。"""
    _, fmt, compression = split_container(path)
    return (fmt + compression).lstrip(".")


def with_compression(path: str | Path) -> str:
    """``X.csv`` -> ``X.csv.gz``（压缩产物该叫什么）；已经压过的不再叠一层。"""
    name = Path(str(path).replace("\\", "/")).name
    return name if name.lower().endswith(COMPRESSION) else name + COMPRESSION


def open_text(
    path: str | Path,
    encoding: str = "utf-8",
    newline: str = "",
    errors: str | None = None,
):
    """按容器打开一个**文本**流：压过的就现解压，没压的就是普通文件。

    刻意不写 ``errors="replace"``：调用方（:func:`i3pro.csvlog.scan_rows`、
    :func:`i3pro.txtlog.detect_encoding`）正是靠 ``UnicodeDecodeError`` 一个个
    试编码的，替它们吞掉错误等于把"换一种编码再试"那条路拆了。
    """
    path = Path(path)
    if is_compressed(path):
        return gzip.open(path, "rt", encoding=encoding, newline=newline, errors=errors)
    return path.open("r", encoding=encoding, newline=newline, errors=errors)


def open_binary(path: str | Path) -> IO[bytes]:
    """按容器打开一个**二进制**流（探编码、嗅表头用）。"""
    path = Path(path)
    return gzip.open(path, "rb") if is_compressed(path) else path.open("rb")
