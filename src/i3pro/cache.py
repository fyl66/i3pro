"""缓存与指纹：**"这份东西还是不是当初那一份"只有一处实现**。

到 ticket #49 为止，四处缓存各写一遍"什么算变了、缓存坏了怎么办"：

* 场次对象缓存（``SessionLibrary._cache``）——只按路径当键，靠容量淘汰；
* 数学通道挂载（``_maths_attached``）——本地/全局定义文件的 mtime；
* 侧边栏列表（``_listing_cache`` 内存 + ``%TEMP%`` 里那份磁盘缓存）——每场文件 +
  圈侧车 + 整个 DBC 目录；
* CAN 摘要（写进 ``<场次>.can.json`` 侧车）——源文件 + DBC 的**内容 sha256** + 圈侧车。

于是"指纹"有三种形状（``(存在, mtime, size)`` / ``[名字, size, mtime]`` / ``sha256``），
"坏了当没有"写了两遍，而**最容易踩的那一脚没有任何地方写着**：指纹要能写进 JSON 再比
回来，所以形状必须是 ``list``——``tuple`` 一进 JSON 就变 ``list``，比回来永远不等，
缓存于是每次都判"过期"（不报错，只是白算）。

这个模块只做两件事：**指纹怎么算**（:func:`file_stamp` / :func:`content_stamp` /
:func:`tree_stamp`）与**磁盘缓存怎么读写**（:func:`read_json` / :func:`write_json`）。
各处的"什么时候去问缓存"留在原处——那是各自的业务。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = [
    "content_stamp",
    "file_stamp",
    "read_json",
    "tree_stamp",
    "write_json",
]


def file_stamp(path: str | Path) -> list:
    """``[文件名, 字节数, mtime_ns]``：这份文件还是不是当初那一份；不在给 ``-1``。

    形状是 **list 不是 tuple**：指纹要能写进 JSON 再比回来，而 tuple 一进 JSON 就变
    list——比回来永远不等，缓存于是每次都白算一遍（不报错，只是慢）。
    """
    target = Path(path)
    try:
        info = target.stat()
    except OSError:
        return [target.name, -1, -1]
    return [target.name, int(info.st_size), int(info.st_mtime_ns)]


def content_stamp(path: str | Path) -> list:
    """``[文件名, sha256]``：**内容**变没变。

    DBC 这类"读进来决定怎么解码"的文件用它：改一行定义就得让 CAN 摘要重算，而
    mtime 有可能被工具（或拷贝）保留。代价是要真读一遍文件——所以只给几十 KB 的
    DBC 用，不给几百 MB 的日志用。
    """
    target = Path(path)
    try:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError:
        return [target.name, ""]
    return [target.name, digest]


def tree_stamp(root: str | Path, pattern: str = "*.dbc") -> list | None:
    """目录树里每个匹配文件的 ``[相对路径, 字节数, mtime_ns]``；目录不在给 ``None``。

    顺序排序，所以"同一棵没动过的树"永远给同一个指纹。
    """
    directory = Path(root)
    if not directory.is_dir():
        return None
    # 顺序：先浅后深、同层按路径——和 `canlog._dbc_files` 的取文件顺序一致，
    # 免得"同一棵树"在报告里和指纹里长得不一样。
    paths = sorted(directory.rglob(pattern),
                   key=lambda path: (len(path.relative_to(directory).parts), str(path)))
    out: list[list] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        out.append([path.relative_to(directory).as_posix(),
                    int(info.st_size), int(info.st_mtime_ns)])
    return out


def read_json(path: str | Path, stamp: Any) -> Any | None:
    """磁盘缓存读得出来、指纹也对得上才作数。

    **缓存坏了当没有**：文件不在 / 不是 JSON / 结构不对 / 指纹对不上，一律返回
    ``None`` 让调用方重算。缓存不是数据，读不出来不该吵。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("stamp") != stamp:
        return None
    return data.get("value")


def write_json(path: str | Path, stamp: Any, value: Any) -> bool:
    """原子写（``.part`` → 改名）；写不进去返回 ``False``。

    只读目录 / 权限不够不算错误：那只是"这次没缓存上"，不该让调用它的那条路挂掉。
    """
    target = Path(path)
    temp = target.with_name(target.name + ".part")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps({"stamp": stamp, "value": value},
                                   ensure_ascii=False),
                        encoding="utf-8", newline="\n")
        temp.replace(target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False
    return True
