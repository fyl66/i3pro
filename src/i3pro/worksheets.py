"""工作表（worksheet）：工作台上方那一排按钮，一个按钮一份 ``worksheets/*.json``。

ticket #30 之前，那 7 套工作表（分析 / 对比 / 动力 / 底盘 / 车手 / 仪表台 / 报表）
是 `viewer.html` 里的一段硬编码：想加一套就得改前端、加一种显示形式就得同时改这里。
现在它们是这个目录里的普通 JSON —— 能 diff、能发给队友、换台机器就有，加一套不用
碰代码。文件名是身份（ASCII，将来要放进 URL），文件里的 ``name`` 是按钮上的字。

这个模块只管**文件**：找、读、校验、排序，以及 ticket #33 的增删改（新建 / 保存 /
另存为 / 改名 / 删除）。它不认识组件类型（那张表在前端 `COMPONENT_TYPES` 里，是这个
仓库唯一的一份），也不认识本场次有哪些通道（那是 `render.build_payload` 的事）——
所以这里一个组件类型名都不写死，也没有"默认工作表长什么样"这种知识（新建时画什么，
由前端把组件发过来）。

坏文件不连累别人：一份读不出来只记进 ``problems``，别的工作表照常加载。每条
``error`` 都按项目规则带"下一步做什么"，因为读不懂的错误信息等于没报错。

写这一层只守两条规矩：**永不覆盖**（撞名加 ``-1`` / `` 2`` 后缀）与**先写 .part
再改名**（断电不留半份）。文件名（身份）会被拼进 URL，所以进来之前一律过
:func:`check_stem`；显示名（按钮上的字）可以是中文。

**故意不做缓存**：一整套工作表只有几 KB，而 ``library._paths()`` 每次请求都在扫数据
目录。加一份缓存就多一个要失效的东西，收益是微秒级。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import aliases as aliasesmod

__all__ = [
    "SCHEMA",
    "WorksheetError",
    "check_stem",
    "create",
    "export_payload",
    "import_text",
    "load_dir",
    "load_file",
    "load_directory",
    "normalise",
    "read_sheet",
    "rename",
    "remove",
    "replace",
    "slug",
    "unique_stem",
    "write_sheet",
    "worksheets_dir",
]

#: 这一版认得的格式版本。以后改格式时靠它认新旧（旧文件报"升级 i3pro"，不是猜着读）。
SCHEMA = 1

#: 没写 ``order`` 的工作表排在哪。够宽，前后都塞得下。
DEFAULT_ORDER = 50

#: 一份工作表顶层认得的键。``aliases``（ticket #36）是"这套表要用哪条车速"那类
#: 有序候选表——它跟着工作表走，不进 ``.ld``、不进日志侧车。
_TOP_KEYS = {"schema", "name", "order", "hints", "components", "aliases"}
_COMPONENT_KEYS = {"type", "x", "y", "w", "h", "config", "pick"}
_PICK_KEYS = {"patterns", "limit", "index", "special"}

#: 文件名（身份）只许这些字符：URL 与文件系统两头都安全，也不给 ``../`` 留缝。
STEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Windows 上这几个名字不能当文件名（``CON.json`` 写不出来，报的错还很难懂）。
_RESERVED_STEMS = {
    "con", "prn", "aux", "nul",
    *("com%d" % n for n in range(1, 10)),
    *("lpt%d" % n for n in range(1, 10)),
}


class WorksheetError(ValueError):
    """一份工作表读不出来。消息里必须带"下一步做什么"。

    是 ``ValueError`` 的子类，所以 HTTP 层已有的 400 分支能直接用。
    """


# ------------------------------------------------------- 文件名（身份）

def check_stem(stem: str) -> str:
    """把一个文件名（身份）验成安全的；不合格抛 :class:`WorksheetError`。

    服务端拿这个字符串**拼路径**（``worksheets/<身份>.json``），所以这一步不是洁癖：
    没有它，``/api/worksheets/..%2F..%2Fetc`` 之类的东西就能写到目录外面去。
    """
    if not isinstance(stem, str) or not STEM_RE.match(stem):
        raise WorksheetError(
            f"{stem!r} 不是一个合法的工作表名；文件名只能是 ASCII 字母、数字、点、"
            "下划线与连字符（64 个以内），而且要以字母或数字开头——它要进网址。"
        )
    if stem.lower() in _RESERVED_STEMS:
        raise WorksheetError(f"{stem!r} 是 Windows 的保留文件名，换一个。")
    return stem


def slug(text: str) -> str:
    """显示名 → 文件名：非 ASCII（中文）当分隔符丢掉，丢空了就叫 ``sheet``。

    显示名是按钮上的字，可以是中文；文件名是身份，只能 ASCII。两者分开是 i2 Pro 的
    做法，也是这个模块一直的约定——所以中文名建出来的工作表叫 ``sheet.json``，
    按钮上写的还是中文。
    """
    raw = re.sub(r"[^a-z0-9._-]+", "-", str(text).strip().lower())
    raw = re.sub(r"-{2,}", "-", raw).strip("-._")
    # 一个字母都不剩（纯中文、纯数字）就用 sheet 起头：文件名里全无字母不好认，
    # 也免得 "分析 2" 这种第二份落到一个光秃秃的 "2.json"。
    if not raw or not re.search(r"[a-z]", raw):
        raw = ("sheet-" + raw) if raw else "sheet"
    raw = raw[:60].rstrip("-._") or "sheet"
    if raw.lower() in _RESERVED_STEMS:
        raw = "sheet-" + raw
    return raw


def unique_stem(directory: str | Path, stem: str, *, exclude: str | None = None) -> str:
    """找一个没被占用的文件名：``stem`` / ``stem-1`` / ``stem-2``……**永不覆盖**。

    ``exclude`` 是"我自己那一份"：改名时目标名和原名相同时，不该把自己当成撞名。
    """
    directory = Path(directory)
    for n in range(0, 1000):
        candidate = stem if n == 0 else f"{stem}-{n}"
        if candidate == exclude:
            continue
        if not (directory / f"{candidate}.json").exists():
            return candidate
    raise WorksheetError(
        f"{stem} 这个名字已经有太多份了（数到 1000）；先清理一下 worksheets/ 再存。"
    )


# ------------------------------------------------------------ 写文件

def export_payload(sheet: dict) -> dict:
    """一份工作表 → 文件里（或发给队友）的那份 JSON；``id`` 是本地身份，不进文件。"""
    body = {
        "schema": SCHEMA,
        "name": sheet["name"],
        "order": sheet["order"],
        "hints": list(sheet.get("hints") or []),
        "components": [dict(one) for one in sheet["components"]],
    }
    # 没有别名的老工作表**一个字节都不多写**：仓库里那七份文件保持原样，
    # 免得"加了一个功能"变成"七份文件全都有 diff"。
    if sheet.get("aliases"):
        body["aliases"] = [dict(one) for one in sheet["aliases"]]
    return body


def write_sheet(directory: str | Path, stem: str, payload: Any) -> dict:
    """把一份工作表写进 ``<directory>/<stem>.json``，返回校验过的那份。

    先写 ``.json.part`` 再改名：写到一半断电／被杀，目录里也不会多出一份半截的工作表
    （改名在同一分区上是原子的）。
    """
    stem = check_stem(stem)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sheet = normalise(payload, stem, where=f"{stem}.json")
    text = json.dumps(export_payload(sheet), ensure_ascii=False, indent=2) + "\n"
    target = directory / f"{stem}.json"
    temp = target.with_name(target.name + ".part")
    try:
        # newline="\n"：写出来的字节与导出接口发出去的那份**一模一样**，
        # 也不会在 Windows 上悄悄变成 CRLF（git 里 diff 干净）。
        temp.write_text(text, encoding="utf-8", newline="\n")
        temp.replace(target)
    except OSError as exc:
        try:
            temp.unlink()
        except OSError:
            pass
        raise WorksheetError(
            f"{stem}.json 写不进去（{exc.strerror or exc}）；"
            "看看 worksheets/ 是不是只读，或这个文件被别的程序占着。"
        ) from exc
    return sheet


def read_sheet(directory: str | Path, stem: str) -> dict:
    """按身份读一份**已经存在**的工作表；找不到就说下一步做什么。"""
    stem = check_stem(stem)
    target = Path(directory) / f"{stem}.json"
    if not target.is_file():
        raise WorksheetError(
            f"找不到工作表 {stem}.json；刷新一下页面（F5）看看它还在不在，"
            "或打开 worksheets/ 目录确认文件没被挪走。"
        )
    return load_file(target)


def create(directory: str | Path, payload: Any, *, name: str | None = None) -> dict:
    """新建一份工作表（界面的"新建" / "另存为" / "从文件导入"都走这里）。

    撞名**永不覆盖**：文件名加 ``-1``、显示名加 `` 2``，两份都在，按钮上也分得清。
    ``order`` 没写就排到最后——新加的那套出现在那一排的最右边，符合直觉。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if not isinstance(payload, dict):
        raise WorksheetError(
            "工作表顶层要是一个对象（见 worksheets/README.md 那份例子）。"
        )
    sheets, _problems = load_directory(directory)
    used = {sheet["name"] for sheet in sheets}
    title = str(name if name is not None else payload.get("name") or "").strip() or "新工作表"
    for n in range(0, 1000):
        candidate = title if n == 0 else f"{title} {n + 1}"
        if candidate in used:
            continue
        stem = unique_stem(directory, slug(candidate))
        break
    else:
        raise WorksheetError(f"「{title}」已经有太多同名工作表了；换一个名字再存。")
    body = dict(payload)
    body["name"] = candidate
    if "order" not in body:
        body["order"] = max(
            [sheet["order"] for sheet in sheets], default=DEFAULT_ORDER - 1
        ) + 1
    return write_sheet(directory, stem, body)


def replace(
    directory: str | Path, stem: str, components: Any, aliases: Any = None
) -> dict:
    """把**已经在文件里的**那套工作表的组件换掉（界面上的"保存"）。

    名字 / 排序 / 提示词都留在文件里不动——"保存"保存的是这一屏的形状，不是改名。
    ``aliases`` 给了就一起换（别名也是"这一屏的形状"的一部分，ticket #36）；
    不给就保持文件里那一份。
    """
    directory = Path(directory)
    sheet = read_sheet(directory, stem)
    body = export_payload(sheet)
    body["components"] = components
    if aliases is not None:
        body["aliases"] = aliases
    return write_sheet(directory, stem, body)


def rename(directory: str | Path, stem: str, new_name: str) -> dict:
    """改名：文件名跟着显示名走，旧文件删掉（界面上"实测文件名变化"就是这条）。

    目标名已经有一套时**报错而不是覆盖**：改名是明确动作，静默盖掉别人那一套不是
    帮忙。要保留两份请用"另存为"。
    """
    directory = Path(directory)
    sheet = read_sheet(directory, stem)
    title = str(new_name or "").strip()
    if not title:
        raise WorksheetError("新名字不能是空的；写一个名字再点确定。")
    if title == sheet["name"]:
        return sheet
    taken = {
        one["name"]: one["id"] for one in load_directory(directory)[0] if one["id"] != stem
    }
    if title in taken:
        raise WorksheetError(
            f"已经有一套叫「{title}」的工作表了（{taken[title]}.json）；"
            "换个名字，或者先把那一套改名 / 删掉。"
        )
    new_stem = stem if slug(title) == stem else unique_stem(directory, slug(title))
    body = export_payload(sheet)
    body["name"] = title
    renamed = write_sheet(directory, new_stem, body)
    if new_stem != stem:
        try:
            (directory / f"{stem}.json").unlink()
        except OSError as exc:
            raise WorksheetError(
                f"新文件写好了（{new_stem}.json），但旧的 {stem}.json 删不掉"
                f"（{exc.strerror or exc}）；手动删掉它，不然两套都在那一排按钮上。"
            ) from exc
    return renamed


def remove(directory: str | Path, stem: str) -> str:
    """删掉一份工作表，返回它原来的显示名（界面要拿它说"删了哪一套"）。"""
    directory = Path(directory)
    sheet = read_sheet(directory, stem)
    try:
        (directory / f"{stem}.json").unlink()
    except OSError as exc:
        raise WorksheetError(
            f"{stem}.json 删不掉（{exc.strerror or exc}）；"
            "看看文件是不是被别的程序占着（编辑器 / 资源管理器预览）。"
        ) from exc
    return sheet["name"]


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
    aliases = aliasesmod.normalise(payload.get("aliases"), spot) \
        if "aliases" in payload else []
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
        #: 通道别名（ticket #36）：有序候选，取第一条在本场次存在的。
        "aliases": aliases,
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
    payload = parse_text(text, path.name, "改好它，或先把这份文件移出 worksheets/。")
    return normalise(payload, path.stem, where=f"{path.name}")


def parse_text(text: str, where: str, fix: str) -> Any:
    """``json.loads`` + 说人话的报错；``fix`` 是"下一步做什么"那半句。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorksheetError(
            f"{where} 不是合法的 JSON：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}；{fix}"
        ) from exc


def import_text(directory: str | Path, text: str) -> dict:
    """收下队友发来的一份工作表 JSON，存成新的一份（**永不覆盖**）。

    走 `create`，所以"撞名加后缀 / 坏文件说清哪一行"这些规矩与界面上那几条是同一套。
    """
    payload = parse_text(
        text,
        "导入的这份工作表",
        "这份文件可能不是工作表；让队友在界面上点「导出」重新发一份过来。",
    )
    return create(directory, payload)


def load_dir(root: str | Path | None = None) -> tuple[list[dict], list[dict]]:
    """读一个目录下的全部工作表，返回 ``(工作表, 问题)``。

    工作表按 ``order`` 再按文件名排。问题里的每一条都是
    ``{"file": 文件名, "error": 一句话 + 下一步}``，**不会**因为一份坏文件就
    一个都不加载。
    """
    return load_directory(worksheets_dir(root))


def load_directory(directory: str | Path) -> tuple[list[dict], list[dict]]:
    """:func:`load_dir` 的内核：认的是**已经展开的那个目录**，不是仓库根。

    写这一层（新建 / 改名 / 删除）要按**真实目录**判断撞名，所以先把它分出来，
    免得每处都在猜"这个参数是根还是目录"。
    """
    directory = Path(directory)
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
