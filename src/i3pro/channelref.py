"""通道引用（channel reference）：用户写的名字 → 本场次里真通道。

一个名字可能从五个地方来：**数学通道表达式里打的**、**工作表里排的别名候选**、
**导入 CSV 的列名**、DBC 解出来的信号名、`.ld` 里的通道名。它们问的是同一件事——
"这条通道本场次到底叫什么"——所以在 ticket #46 之前，这件事被写了两遍、三遍、
五遍，而且互相绊倒过：

* 数学通道认不出 ``FSD-Distance1``（用户报的）；
* ``csvlog.canonical_names()`` 拿 :data:`derive.SPEED_CANDIDATES` 当列名归一表，
  往那份名单里多写一个"同一化的写法"（``Vx_KF``）会让**导入的 CSV 列被改名**
  （实测：一张列叫 ``Vx KF`` 的表被改成了 ``Vx_KF``）；
* 别名解析只认原生通道，于是别名永远落不到数学通道上——而 CONTEXT.md 写的是
  "数学通道除此之外与原生通道完全一样"。

这个模块只做三件事，每一件都只做一次：

* :func:`normalise` —— "写法不同、同一条通道"的那条规则（空格 / 下划线 / 大小写）；
* :func:`lookup` / :func:`lookup_unique` —— 在一组已知名字里找命中。**两种歧义策略
  都是有意保留的**：``lookup`` 取排序后的第一条（确定性优先，"这场用了哪条速度"不能
  随字典顺序漂移），``lookup_unique`` 命中多条就给 ``None``（不猜，数学通道用它）；
* :func:`suffix_pair` / :func:`first_present` —— 两条组合规则：同后缀配一对、
  有序候选取第一条存在的。

调用方各自的**规则顺序**留在原地（导入先查改名表、数学通道先看原样），
共用的是上面这几条。词汇也留在各自的领域模块里：经纬度的词头在 :mod:`i3pro.derive`，
别人的列名映射在 :mod:`i3pro.csvlog`。
"""

from __future__ import annotations

from typing import Iterable

from . import channels as channelsmod

__all__ = [
    "first_present",
    "known_names",
    "lookup",
    "lookup_unique",
    "name_table",
    "normalise",
    "suffix_pair",
]


def normalise(text: str) -> str:
    """去掉空格 / 下划线 / 大小写之后的键，用来认"同一个名字的两种写法"。

    ``Vx KF`` / ``Vx_KF`` / ``vx-kf`` 都是 ``vxkf``。没名字（``None`` / 空串）给空串，
    调用方按"空名字不匹配任何东西"处理。
    """
    return "".join(character for character in str(text or "").lower()
                   if character.isalnum())


def known_names(log) -> set[str]:
    """本场次**能被引用**的通道名：原生通道 + 数学通道。

    数学通道必须算数：CONTEXT.md 写着"除此之外与原生通道完全一样（可画图、可散点、
    可切圈、可进报表）"。ticket #46 之前别名解析只认真实通道，于是"给车速配一条别名"
    永远落不到算出来的那条车速上。
    """
    return {channel.name for channel in log.channels} | set(channelsmod.names(log))


def name_table(known: Iterable[str]) -> dict[str, str]:
    """``normalise(名字) -> 真名``：同一个键落回多条时取**排序后的第一条**，
    所以同样的输入永远给同样的答案（不然"这场用了哪条速度"会随字典顺序漂移）。
    """
    table: dict[str, str] = {}
    for name in sorted(known):
        table.setdefault(normalise(name), name)
    return table


def lookup(known, name, *, table: dict[str, str] | None = None) -> str | None:
    """``name`` 在本场次里实际叫什么；没有就是 ``None``。

    先按原样找，再按 :func:`normalise` 找一遍。多个名字规范化之后撞在一起时取
    排序后的第一条（见 :func:`name_table`）。
    """
    if not isinstance(name, str) or not name:
        return None
    if name in known:
        return name
    return (table or name_table(known)).get(normalise(name))


def lookup_unique(known, name, *, table: dict[str, str] | None = None) -> str | None:
    """同 :func:`lookup`，但**对不上唯一一条就给 ``None``**。

    数学通道表达式用这条：写 ``Vx KF`` 而场次里既有 ``Vx KF`` 又有 ``Vx_KF`` 时，
    与其挑一条算下去（算错了没人看得出来），不如让"本场次没有这个通道"那条报错去说。
    """
    if not isinstance(name, str) or not name:
        return None
    if name in known:
        return name
    key = normalise(name)
    if not key:
        return None
    hits = [item for item in known if normalise(item) == key]
    return hits[0] if len(hits) == 1 else None


def suffix_pair(
    known: Iterable[str],
    left_heads: Iterable[str],
    right_heads: Iterable[str],
) -> tuple[str, str] | None:
    """找"同后缀的一对"：前缀恰好是认得的词、后缀逐字相同。

    ``latitude_MTI`` / ``longitude_MTI``（Xsens MTi 解出来的经纬度）就是这么配上的；
    只比前缀不比对后缀的话，``Lateral`` / ``Longitudinal`` 会被当成一对。
    词头由调用方给（那是领域词汇，不是通用规则）。
    """
    table = name_table(known)
    for key in sorted(table):
        for head in left_heads:
            if not key.startswith(head):
                continue
            tail = key[len(head):]
            for other_head in right_heads:
                other = table.get(other_head + tail)
                if other and other != table[key]:
                    return table[key], other
    return None


def first_present(candidates: Iterable[str], present: Iterable[str]) -> str | None:
    """有序候选里**第一条在本场次存在的**；一条都不在给 ``None``。

    别名（i2 Pro 的 Channel Aliases）的规则就是这一句，顺序是用户排的。
    """
    known = present if isinstance(present, (set, frozenset)) else set(present)
    for candidate in candidates or ():
        if candidate in known:
            return candidate
    return None
