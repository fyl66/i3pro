"""通道别名：让**同一套工作表**跨场次还能用（ticket #36）。

问题很具体：高避那台车的车速叫 ``Vx KF``，耐久那台叫 ``GPS Speed``。一张"看车速"
的图写死了名字，换一份数据就空着（ticket #34 只能把它**显示成缺失**，不能把它
变成对的）。

i2 Pro 的解法叫 Channel Aliases：一条别名 = **有序候选通道名列表**，解析时取
**第一条在本场次存在的**；别名存在工作表里（不进 ``.ld``、不进日志侧车），所以
它随工作表走、能发给队友。

这里定三件事，都只写一遍：

* :func:`normalise` —— 别名表的形状（工作表文件里 ``aliases`` 那一段）；
* :func:`status` —— 每条别名**这一场**落到了哪条通道（没落地就明说，不静默）；
* :func:`annotate` —— 把上面那份结果贴到工作表上，页面载荷与 ``/api/worksheets``
  两条路共用。

**规矩**：别名的解析结果**不进配置文件**。工作台里那条通道槽要么原样留着
``@车速``（文件里就写着它，换场次还能用），要么被用户改成一条真通道名——
这两件事前端分得开（见 ``viewer.html`` 的 ``applyAliases`` / ``sheetComponent``）。
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "PREFIX",
    "annotate",
    "is_reference",
    "landing",
    "name_of",
    "normalise",
    "status",
]

#: 引用一条别名就写 ``@车速``。用 ``@`` 起头是因为通道名里不会出现它：
#: 那个字符在 MoTeC / 我们的导入里都不是合法通道名的一部分，所以"这是别名、
#: 不是通道名"不需要靠猜。
PREFIX = "@"


def is_reference(value: Any) -> bool:
    """这一格写的是别名引用吗（``@车速``）？"""
    return (
        isinstance(value, str)
        and value.strip().startswith(PREFIX)
        and len(value.strip()) > 1
    )


def name_of(value: Any) -> str:
    """``@车速`` → ``车速``；不是引用就给空串。"""
    return value.strip()[1:].strip() if is_reference(value) else ""


def normalise(value: Any, where: str = "工作表") -> list[dict]:
    """校验 ``aliases`` 那一段，返回 ``[{"name", "candidates", ...}]``。

    坏形状一律抛 :class:`ValueError`，消息里带"下一步做什么"——别名是**要发给队友
    的文件格式**，读不懂的报错等于没报错。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"{where} 的 aliases 要是一列别名（每条是 "
            '{"name": "车速", "candidates": [...]}），现在是 {value!r}。'
        )
    out: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        spot = f"{where} 的第 {index + 1} 条别名"
        if not isinstance(item, dict):
            raise ValueError(f"{spot} 要是一个对象，现在是 {item!r}。")
        unknown = sorted(set(item) - {"name", "candidates", "note"})
        if unknown:
            raise ValueError(
                f"{spot} 里有认不出来的键 {unknown}；这一版只有 name / candidates / note。"
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError(f'{spot} 没写 name；写上按钮上要显示的名字，例如 "车速"。')
        if name.startswith(PREFIX):
            raise ValueError(
                f"{spot} 的名字 {name!r} 以 {PREFIX} 开头；名字本身不要带 {PREFIX}，"
                f"引用它的地方才写 {PREFIX}{name}。"
            )
        if name in seen:
            raise ValueError(f"{spot} 和前面一条重名（{name}）；换一个名字。")
        seen.add(name)
        raw = item.get("candidates")
        if not isinstance(raw, list):
            raise ValueError(
                f'{spot}（{name}）的 candidates 要是一列通道名；'
                '例如 ["Ground Speed", "GPS Speed", "Vx KF"]。'
            )
        # **空的一列是允许的**：界面上"新建一条别名"和"给它加候选"是两个动作，
        # 中间那一瞬就是空的。把它当坏文件挡掉的话，用户会撞上"保存失败"而不是
        # "还没写候选"——状态视图会把这一条显示成"本场次一条都没落地"。
        candidates: list[str] = []
        for one in raw:
            text = str(one).strip()
            if not text:
                raise ValueError(f"{spot}（{name}）的 candidates 里有一条是空的。")
            if is_reference(text):
                raise ValueError(
                    f"{spot}（{name}）的候选 {text!r} 又是别名引用；候选只能写真通道名。"
                )
            if text not in candidates:
                candidates.append(text)
        entry = {"name": name, "candidates": candidates}
        note = str(item.get("note") or "").strip()
        if note:
            entry["note"] = note
        out.append(entry)
    return out


def landing(entries: Iterable[dict], reference: Any, present: Iterable[str]) -> str | None:
    """一条引用 → 落到的通道名；落不到给 ``None``。

    "第一条在本场次存在的"就是全部规则：顺序是用户排的，所以它说了算。
    别名表里根本没有这个名字也返回 ``None``（那和"候选都不在"在界面上是两件事，
    :func:`status` 负责把两者分开说）。
    """
    wanted = name_of(reference)
    if not wanted:
        return None
    known = present if isinstance(present, (set, frozenset)) else set(present)
    for entry in entries or ():
        if entry.get("name") != wanted:
            continue
        for candidate in entry.get("candidates") or ():
            if candidate in known:
                return candidate
        return None
    return None


def status(entries: Iterable[dict], present: Iterable[str]) -> list[dict]:
    """每条别名**这一场**落到了哪条通道——状态视图的数据（i2 Pro 的 Channel Status）。

    没落地的条目 ``channel`` 是 ``None``，界面按 ticket #34 的同一套三态把它显示成
    "本场次没有"，而不是另写一套"别名失败了"的说法。
    """
    known = present if isinstance(present, (set, frozenset)) else set(present)
    rows: list[dict] = []
    for entry in entries or ():
        hit = next((c for c in entry.get("candidates") or () if c in known), None)
        rows.append(
            {
                "name": entry.get("name", ""),
                "reference": PREFIX + str(entry.get("name", "")),
                "channel": hit,
                "candidates": list(entry.get("candidates") or []),
                "note": entry.get("note", ""),
            }
        )
    return rows


def annotate(sheets: Iterable[dict], present: Iterable[str]) -> list[dict]:
    """把 ``alias_status`` / ``alias_landing`` 贴到每份工作表上（**这一场**的结果）。

    ``alias_landing`` 是给前端查表用的平表（``"@车速" -> "Vx KF"`` 或 ``None``）：
    判定只有 :func:`landing` 一处实现，前端只做查表，不重写一遍规则。
    """
    known = present if isinstance(present, (set, frozenset)) else set(present)
    out = []
    for sheet in sheets:
        rows = status(sheet.get("aliases") or [], known)
        out.append(
            {
                **sheet,
                "alias_status": rows,
                "alias_landing": {row["reference"]: row["channel"] for row in rows},
            }
        )
    return out
