"""场次旁边那份"用户自己的 JSON"只有一套读写（ticket #16）。

信标（`<场次>.laps.json`）、赛道区段（`.sections.json`）、GPS 校正（`.gps.json`）、
注释（`.notes.json`）、数学通道（`.maths.json` 与仓库里的 `maths/global.json`）、
CSV 列映射（`.map.json`）——六种侧车原先各自实现了一遍"文件放哪、缺了算什么、
读坏了怎么办、写坏了算不算成功"：六份接口、**四种**失败策略，`.gitignore` 还跟着
加了六条。ADR-0001 那条"用户资产不是缓存、`.ld` 永远只读"于是被独立实现了六遍。

现在只有这里回答那几个问题，策略只有一套：

    缺失  ->  该 kind 的空值（``Kind.empty``）：读出来就是"还没设过"，不是错误
    读坏  ->  抛 :class:`SidecarError`，消息里带一句"下一步做什么"，**绝不删那个文件**
    写    ->  先落同目录的临时文件，再 ``os.replace`` 原子替换
              （写一半断电也不会留下半个 JSON）

"读坏了当空"是这里最不该做的事：用户打开工作表看见一条注释也没有，补几条再一保存，
旧的就被覆盖掉了。坏文件必须吵，而且要吵得能照做。``.ld`` 一个字节都不动。

新加一种侧车 = 在 :data:`KINDS` 里加一条（后缀 + 空值 + 两句话），别处都不用动；
解码仍留在各自的领域模块（``LapConfig.from_dict`` 这类），这里只管文件。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "KINDS", "Kind", "SidecarError", "kind_of", "path_of", "read", "read_path",
    "register", "write", "write_path",
]


class SidecarError(ValueError):
    """侧车读不出来或写不下去。

    是 ``ValueError`` 的子类，所以 HTTP 层已有的 400 分支能直接用；消息按项目规则
    说清"下一步做什么"，而且**文件不会被删**（用户的东西不替他们扔）。
    """


@dataclass(frozen=True)
class Kind:
    """一种侧车的形状。"""

    #: 跟在场次名后面的后缀：``<场次>.ld`` -> ``<场次>.laps.json``。
    suffix: str
    #: 文件不存在时读出来的值（"还没设过"）。
    empty: Callable[[], Any]
    #: 读不出来时用来提示"删掉它会让什么回到默认"。
    reset: str
    #: 顶层允许的 JSON 类型（决定报错怎么说）。
    shape: tuple[type, ...] = (dict,)

    def blank(self) -> Any:
        return self.empty()

    def shape_label(self) -> str:
        names = {dict: "对象", list: "数组"}
        return "或".join(names.get(kind, kind.__name__) for kind in self.shape)


#: 六种侧车。加一种就在这里加一条——这就是"新加一种侧车只要一处登记"的那一处。
KINDS: dict[str, Kind] = {
    "laps": Kind(".laps.json", dict, "信标回到自动切分"),
    "sections": Kind(".sections.json", lambda: None, "区段回到自动切分"),
    "gps": Kind(".gps.json", lambda: None, "GPS 回到不校正"),
    "notes": Kind(".notes.json", list, "注释清空", shape=(list, dict)),
    "maths": Kind(".maths.json", dict, "数学通道回到只读全局定义"),
    "csvmap": Kind(".map.json", dict, "CSV 列名回到自动匹配"),
}


def register(name: str, kind: Kind) -> None:
    """登记一种侧车（正常用法是往 :data:`KINDS` 里加一条，这个入口留给测试）。"""
    if not isinstance(kind, Kind):
        raise TypeError("register() 收的是 Kind，不是别的")
    KINDS[name] = kind


def kind_of(name: str) -> Kind:
    kind = KINDS.get(name)
    if kind is None:
        raise KeyError(
            f"没登记过这种侧车：{name!r}。"
            f"在 sidecar.KINDS 里加一条（后缀 + 缺省值 + 坏了退回什么）就行。"
        )
    return kind


def path_of(name: str, session_path: str | Path) -> Path:
    """``<场次>.ld``（或 ``.csv`` / ``.ldx``）-> 这种侧车的路径。"""
    return Path(session_path).with_suffix(kind_of(name).suffix)


def _unreadable(kind: Kind, path: Path, why: Exception) -> str:
    return (
        f"{path.name} 读不出来（{type(why).__name__}: {why}）。"
        f"修好这个 JSON，或者直接删掉它让{kind.reset}——这个文件不会被自动删掉。"
    )


def read_path(name: str, path: str | Path) -> Any:
    """读一份侧车（按路径）。缺失=空值；读坏=``SidecarError``（带下一步）。"""
    kind = kind_of(name)
    path = Path(path)
    if not path.exists():
        return kind.blank()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SidecarError(_unreadable(kind, path, exc)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise SidecarError(_unreadable(kind, path, exc)) from exc
    if not isinstance(data, kind.shape):
        raise SidecarError(
            f"{path.name} 的顶层应该是一个 JSON {kind.shape_label()}，"
            f"读到的是 {type(data).__name__}。"
            f"改对它的顶层，或者删掉它让{kind.reset}。"
        )
    return data


def read(name: str, session_path: str | Path) -> Any:
    """读一份侧车（按场次路径）。"""
    return read_path(name, path_of(name, session_path))


def write_path(name: str, path: str | Path, payload: Any) -> Path:
    """写一份侧车（按路径）。先写临时文件再原子替换，写完一定有换行结尾。"""
    kind = kind_of(name)
    path = Path(path)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if not text.endswith("\n"):
        text += "\n"
    tmp = path.parent / (path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise SidecarError(
            f"{path.name} 写不进去（{type(exc).__name__}: {exc}）。"
            f"看看这个目录还在不在、有没有写权限；原来的文件没有被改坏。"
        ) from exc
    return path


def write(name: str, session_path: str | Path, payload: Any) -> Path:
    """写一份侧车（按场次路径）。"""
    return write_path(name, path_of(name, session_path), payload)
