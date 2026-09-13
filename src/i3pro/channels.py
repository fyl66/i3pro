"""通道接缝：调用方不再需要问「这条通道是不是算出来的」（ticket #18）。

词汇表（``CONTEXT.md``）对**数学通道**的定义只有一句要紧的话：它没有原始整数样本，
只有主时间基上的浮点列，**除此之外与原生通道完全一样**（可画图、可散点、可切圈、可进报表）。

这句话如果散在画图、散点、频谱、切圈、Parquet 五个地方各写一遍，迟早会漏一处——
本项目已经因此出过两次真错：

* 一条数学通道和一条**慢**的原生通道同名时（本地定义覆盖原生通道），下游仍按原生那档
  采样率再 repeat 一遍，曲线被整段毁掉而且不报错；
* 频谱按原生采样率切窗口，慢通道的台阶被当成高频。

所以两类通道之间的差别只在本模块里实现一次：

| 问题 | 原生通道 | 数学通道 |
| --- | --- | --- |
| 采样率 | 文件里那一档 | 主时间基 |
| 单位 | 文件里那一档 | 定义里写的（``derived_units``） |
| 原始样本 | mmap 里 | 没有，``LogFile.raw`` 拒绝 |
| 列放在哪 | 文件里 | 一个 dict（``derived_target``） |

会话类型必须**显式声明**这三样：``derived_target``（放列的 dict）、``derived_names``
（名字集合）、``derived_units``（名字到单位）。本模块不去嗅探对象有哪些属性：没声明就
报错并说清下一步，这样「多了一种会话」是一件看得见的事，而不是悄悄少一支分支。
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "DERIVED_TARGET",
    "attach",
    "clear",
    "hold_factor",
    "is_derived",
    "names",
    "sample_rate",
    "slot",
    "unit",
    "units",
]

#: 会话类上那个「数学通道的列放哪」的属性名。
DERIVED_TARGET = "derived_target"


def _declared(session, attribute: str, consequence: str):
    """拿一个会话**声明过**的能力，没声明就报错并说下一步。

    这里刻意不用 ``getattr(..., default)``：那种写法的失败方式是"少一支分支、静默算错"，
    而我们要的是"新加一种会话，忘了声明就立刻红"。
    """
    try:
        return getattr(session, attribute)
    except AttributeError:
        raise TypeError(
            f"{type(session).__name__} 没有声明 {attribute}——{consequence}。"
            f"在会话类上补一个同名属性即可（见 ld.LogFile / csvlog.CsvSession）。"
        ) from None


def slot(session) -> dict[str, np.ndarray]:
    """数学通道的列放在哪个字典里（``.ld`` 会话是 ``derived``，CSV 会话是 ``columns``）。"""
    target = _declared(session, DERIVED_TARGET, "数学通道算出来的列没有地方挂")
    if not isinstance(target, dict):
        raise TypeError(
            f"{type(session).__name__}.{DERIVED_TARGET} 应该是一个 dict，"
            f"实际是 {type(target).__name__}。"
        )
    return target


def names(session) -> set[str]:
    """这个场次上挂着哪些数学通道（没挂过就是空集合）。

    返回的是**活集合**：``attach`` / ``clear`` 直接改它，调用方不用写回。
    """
    return _declared(session, "derived_names", "数学通道的名字没有地方记")


def units(session) -> dict[str, str]:
    """数学通道的单位（来自定义，可能与同名的原生通道不一样）。"""
    return _declared(session, "derived_units", "数学通道的单位没有地方记")


def is_derived(session, channel) -> bool:
    """这条通道是算出来的吗？``channel`` 可以是 ``Channel``，也可以是名字。"""
    name = channel if isinstance(channel, str) else channel.name
    return name in names(session)


def unit(session, channel) -> str:
    """这条通道的单位：数学通道以它的定义为准，其余看文件。"""
    if is_derived(session, channel):
        return units(session).get(channel.name, channel.unit)
    return channel.unit


def sample_rate(session, channel) -> float:
    """这条通道**实际落在哪条时间基上**，单位 Hz。

    数学通道在主时间基上，所以它和一条慢的原生通道同名时也不算原生那一档。
    """
    if is_derived(session, channel):
        return float(session.sample_rate)
    return float(channel.sample_rate)


def hold_factor(session, channel, rate: float) -> int:
    """把这条通道「保持」到 ``rate`` 这条时间基上，要重复几次采样。

    数学通道的列本来就在主时间基上，答案是 1——**这条规则以前在五个地方各写了一遍**，
    现在只写在这里。``rate`` 是目标时间基（画图用主采样率，Parquet 可以用 ``--rate``）。
    """
    if is_derived(session, channel):
        return 1
    source = float(channel.sample_rate) or 1.0
    target = float(rate) or 1.0
    return max(1, int(round(target / source)))


def info(session, channel) -> dict:
    """界面要的那几项元数据（通道索引 ``render.channel_index`` 就用它）。"""
    return {
        "name": channel.name,
        "unit": unit(session, channel),
        "rate": sample_rate(session, channel),
        "samples": channel.sample_count,
        "derived": is_derived(session, channel),
    }


def attach(session, name: str, values, unit_text: str = "") -> None:
    """把一列算出来的数挂到场次上，让下游当原生通道用（见 ``maths.attach``）。"""
    slot(session)[name] = np.asarray(values, dtype=np.float64)
    names(session).add(name)
    units(session)[name] = unit_text or ""


def clear(session) -> None:
    """撤掉场次上所有数学通道的列、名字与单位。

    通道列表（``session.channels``）由调用方自己收拾——那是"界面上看得见的通道"，
    与"列放在哪"是两件事，``maths.detach`` 负责那一半。
    """
    target = slot(session)
    known = names(session)
    known_units = units(session)
    for name in list(known):
        target.pop(name, None)
        known_units.pop(name, None)
    known.clear()
