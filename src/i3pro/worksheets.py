"""工作表（worksheet）：工作台上方那一排按钮，一个按钮一份 ``worksheets/*.json``。

ticket #30 之前，那 7 套工作表（分析 / 对比 / 动力 / 底盘 / 车手 / 仪表台 / 报表）
是 `viewer.html` 里的一段硬编码：想加一套就得改前端、加一种显示形式就得同时改这里。
现在它们是这个目录里的普通 JSON —— 能 diff、能发给队友、换台机器就有，加一套不用
碰代码。文件名是身份（ASCII，将来要放进 URL），文件里的 ``name`` 是按钮上的字。

这个模块只管**文件**：找、读、校验、排序。它不认识组件类型（那张表在前端
`COMPONENT_TYPES` 里，是这个仓库唯一的一份），也不认识本场次有哪些通道（那是
`render.build_payload` 的事）——所以这里一个组件类型名都不写死。

坏文件不连累别人：一份读不出来只记进 ``problems``，别的工作表照常加载。每条
``error`` 都按项目规则带"下一步做什么"，因为读不懂的错误信息等于没报错。

**故意不做缓存**：一整套工作表只有几 KB，而 ``library._paths()`` 每次请求都在扫数据
目录。加一份缓存就多一个要失效的东西，收益是微秒级。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA",
    "WorksheetError",
    "load_dir",
    "load_file",
    "normalise",
    "worksheets_dir",
]

#: 这一版认得的格式版本。以后改格式时靠它认新旧（旧文件报"升级 i3pro"，不是猜着读）。
SCHEMA = 1

#: 没写 ``order`` 的工作表排在哪。够宽，前后都塞得下。
DEFAULT_ORDER = 50

_TOP_KEYS = {"schema", "name", "order", "hints", "components"}
_COMPONENT_KEYS = {"type", "x", "y", "w", "h", "config", "pick"}
_PICK_KEYS = {"patterns", "limit", "index", "special"}


class WorksheetError(ValueError):
    """一份工作表读不出来。消息里必须带"下一步做什么"。

    是 ``ValueError`` 的子类，所以 HTTP 层已有的 400 分支能直接用。
    """


def worksheets_dir(root: str | Path | None = None) -> Path:
    """工作表放在仓库里的 ``worksheets/``。

    默认按 ``__file__`` 往上找仓库根目录（和 :func:`i3pro.maths.global_path` 同一个
    约定）；``root`` 给定时换一个根 —— 命令行 ``--worksheets`` 与测试用它。
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return base / "worksheets"


# ------------------------------------------------------------------ 校验

def _number(value: Any, field: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WorksheetError(f"{where} 的 {field} 要是一个数字，现在是 {value!r}。")
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise WorksheetError(f"{where} 的 {field} 不能是 NaN / Inf。")
    return number


def _strings(value: Any, field: str, where: str) -> list[str]:
    if not isinstance(value, list):
        raise WorksheetError(f"{where} 的 {field} 要是一串字符串，现在是 {value!r}。")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WorksheetError(f"{where} 的 {field} 里有一项不是非空字符串：{item!r}。")
        out.append(item.strip())
    return out


def _pick_spec(value: Any, field: str, where: str) -> dict:
    if not isinstance(value, dict):
        raise WorksheetError(
            f"{where} 的 pick.{field} 要是一个对象或一串对象（见 worksheets/README.md），"
            f"现在是 {value!r}。"
        )
    unknown = sorted(set(value) - _PICK_KEYS)
    if unknown:
        raise WorksheetError(
            f"{where} 的 pick.{field} 里有认不出来的键 {unknown}；"
            f"这一版认得的键是 {'、'.join(sorted(_PICK_KEYS))}。"
        )
    out: dict = {}
    if "patterns" in value:
        out["patterns"] = _strings(value["patterns"], f"pick.{field}.patterns", where)
    for key in ("limit", "index"):
        if key in value:
            number = _number(value[key], f"pick.{field}.{key}", where)
            if number != int(number) or number < 0:
                raise WorksheetError(
                    f"{where} 的 pick.{field}.{key} 要是 0 或正整数，现在是 {value[key]!r}。"
                )
            out[key] = int(number)
    if "special" in value:
        if not isinstance(value["special"], str) or not value["special"].strip():
            raise WorksheetError(f"{where} 的 pick.{field}.special 要是非空字符串。")
        out["special"] = value["special"].strip()
    if not out:
        raise WorksheetError(
            f"{where} 的 pick.{field} 是一条空规则，什么都没说；"
            "写上 patterns / limit / index 里的至少一个，或把这个键删掉。"
        )
    return out


def _pick(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise WorksheetError(f"{where} 的 pick 要是一个对象：{{\"配置键\": 规则}}。")
    out = {}
    for field, raw in value.items():
        # 一串规则 = 兜底链：前面挑不到就用后面的。
        if isinstance(raw, list):
            if not raw:
                raise WorksheetError(
                    f"{where} 的 pick.{field} 是空的一串；要么写一条规则，要么把这个键删掉。"
                )
            out[field] = [_pick_spec(one, field, where) for one in raw]
        else:
            out[field] = _pick_spec(raw, field, where)
    return out


def _component(value: Any, index: int, where: str) -> dict:
    spot = f"{where} 的第 {index + 1} 个组件"
    if not isinstance(value, dict):
        raise WorksheetError(f"{spot} 要是一个对象，现在是 {value!r}。")
    unknown = sorted(set(value) - _COMPONENT_KEYS)
    if unknown:
        raise WorksheetError(
            f"{spot} 里有认不出来的键 {unknown}；"
            f"这一版认得的键是 {'、'.join(sorted(_COMPONENT_KEYS))}（见 worksheets/README.md）。"
        )
    type_name = value.get("type")
    if not isinstance(type_name, str) or not type_name.strip():
        raise WorksheetError(f"{spot} 没写 type；写上要哪种显示形式，例如 \"graph\"。")
    out: dict = {"type": type_name.strip()}
    for field in ("x", "y", "w", "h"):
        if field in value:
            number = _number(value[field], field, spot)
            if field in ("w", "h") and number <= 0:
                raise WorksheetError(f"{spot} 的 {field} 要大于 0，现在是 {value[field]!r}。")
            if field in ("x", "y") and number < 0:
                raise WorksheetError(f"{spot} 的 {field} 不能是负数，现在是 {value[field]!r}。")
            out[field] = number
    if "config" in value:
        if not isinstance(value["config"], dict):
            raise WorksheetError(f"{spot} 的 config 要是一个对象。")
        out["config"] = dict(value["config"])
    if "pick" in value:
        out["pick"] = _pick(value["pick"], spot)
    return out


def normalise(payload: Any, name: str, where: str | None = None) -> dict:
    """校验一份工作表并补上缺省值；认不出来的地方抛 :class:`WorksheetError`。

    ``name`` 是文件名（不含后缀），也是这份工作表的身份；``payload`` 里没写
    ``name`` 时显示名就用它。
    """
    spot = where or f"工作表 {name}"
    if not isinstance(payload, dict):
        raise WorksheetError(f"{spot} 顶层要是一个对象（见 worksheets/README.md）。")
    unknown = sorted(set(payload) - _TOP_KEYS)
    if unknown:
        raise WorksheetError(
            f"{spot} 里有认不出来的键 {unknown}；"
            f"这一版认得的键是 {'、'.join(sorted(_TOP_KEYS))}（见 worksheets/README.md）。"
        )
    schema = payload.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise WorksheetError(
            f"{spot} 写的是 schema {schema!r}，这个版本只认 {SCHEMA}；"
            "升级 i3pro，或把 schema 改回 " + str(SCHEMA) + "。"
        )
    title = payload.get("name")
    if title is None or (isinstance(title, str) and not title.strip()):
        title = name
    if not isinstance(title, str):
        raise WorksheetError(f"{spot} 的 name 要是一个字符串，现在是 {title!r}。")
    order = _number(payload.get("order", DEFAULT_ORDER), "order", spot)
    hints = _strings(payload.get("hints", []), "hints", spot) if "hints" in payload else []
    components = payload.get("components")
    if not isinstance(components, list) or not components:
        raise WorksheetError(
            f"{spot} 的 components 要是一列组件，而且至少一个；"
            "照 worksheets/README.md 里那份例子写一个。"
        )
    return {
        "id": name,
        "name": title.strip(),
        "order": int(order),
        "hints": hints,
        "components": [
            _component(item, i, spot) for i, item in enumerate(components)
        ],
    }


def load_file(path: str | Path) -> dict:
    """读一份工作表。读不出来抛 :class:`WorksheetError`（消息里带下一步做什么）。"""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WorksheetError(
            f"{path.name} 读不了（{exc.strerror or exc}）；"
            "看看文件是不是被别的程序占着、或者权限不对。"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorksheetError(
            f"{path.name} 不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}；"
            "改好它，或先把这份文件移出 worksheets/。"
        ) from exc
    return normalise(payload, path.stem, where=f"{path.name}")


def load_dir(root: str | Path | None = None) -> tuple[list[dict], list[dict]]:
    """读一个目录下的全部工作表，返回 ``(工作表, 问题)``。

    工作表按 ``order`` 再按文件名排。问题里的每一条都是
    ``{"file": 文件名, "error": 一句话 + 下一步}``，**不会**因为一份坏文件就
    一个都不加载。
    """
    directory = worksheets_dir(root)
    if not directory.is_dir():
        return [], [{
            "file": str(directory),
            "error": (
                f"找不到工作表目录 {directory}；"
                "建一个并放几份 *.json（照 worksheets/README.md 写），"
                "或用 --worksheets 指定别处。"
            ),
        }]
    sheets: list[dict] = []
    problems: list[dict] = []
    seen: dict[str, str] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            sheet = load_file(path)
        except WorksheetError as exc:
            problems.append({"file": path.name, "error": str(exc)})
            continue
        if sheet["name"] in seen:
            problems.append({
                "file": path.name,
                "error": (
                    f"{path.name} 的显示名「{sheet['name']}」和 {seen[sheet['name']]} 重名；"
                    "改掉其中一个的 name，或删掉一份文件。"
                ),
            })
            continue
        seen[sheet["name"]] = path.name
        sheets.append(sheet)
    if not sheets and not problems:
        problems.append({
            "file": str(directory),
            "error": f"{directory} 里没有 *.json；放一份进这里（照 worksheets/README.md 写）。",
        })
    sheets.sort(key=lambda sheet: (sheet["order"], sheet["id"]))
    return sheets, problems
