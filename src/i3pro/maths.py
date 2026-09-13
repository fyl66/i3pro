"""数学通道：把一条表达式编译成一条列。

求值是**白名单**的：表达式先被词法分析、转成逆波兰序列，再由一张函数表执行。
过程中完全不碰 ``eval``／``exec``，所以一条式子最多能算错，不能把机器搭进去
（见 ``docs/PLAN.md`` 与 issue #3 的验收点）。

用法（纯函数，不依赖框架）::

    plan = compile_expr("'(刹车压力)' * 2")
    values = plan.eval(ctx, resolve=lambda name: session.values(name))

作用域有本地与全局两种：本地跟着场次（``<场次>.maths.json``），全局在仓库里
（``maths/global.json``）。同名时本地覆盖全局，见 :func:`resolve_all`。

单位：i2 Pro 允许把单位写在通道名后面的方括号里（``'车轮速度'[km/h]``）。i3pro
目前**不做单位换算**，这样的标注会被接受并忽略，同时记进 ``Plan.notes``，由界面
原样告诉用户——静默忽略比报错更容易让人拿着错数字去调车。
"""

from __future__ import annotations

import json
import math as _math
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import derive
from . import timebase

__all__ = [
    "MathError",
    "Context",
    "Plan",
    "Definition",
    "MathSet",
    "FUNCTIONS",
    "MATH_SUFFIX",
    "compile_expr",
    "evaluate",
    "known_names",
    "resolve_all",
    "resolve_available",
    "apply_to_session",
    "attach",
    "detach",
    "config_path",
    "global_path",
    "load_local",
    "save_local",
    "load_global",
    "save_global",
    "load_effective",
    "function_catalogue",
    "DerivedCache",
]


class MathError(ValueError):
    """表达式的编译或求值失败。

    ``ValueError`` 的子类，所以 HTTP 层已有的 400 分支能直接用；消息按
    ``AGENTS.md`` 规则 6 用中文，并且要说**下一步做什么**。
    """


# --------------------------------------------------------------- 表达式编译

#: 二元运算符 -> (优先级, 结合性)。优先级抄 C 语言那一套，i2 Pro 也是这个顺序。
_BINARY: dict[str, tuple[int, str]] = {
    "||": (1, "left"),
    "&&": (2, "left"),
    "|": (3, "left"),
    "&": (4, "left"),
    "==": (5, "left"),
    "!=": (5, "left"),
    "<": (6, "left"),
    "<=": (6, "left"),
    ">": (6, "left"),
    ">=": (6, "left"),
    "+": (7, "left"),
    "-": (7, "left"),
    "*": (8, "left"),
    "/": (8, "left"),
    "%": (8, "left"),
    "^": (10, "right"),
}

_UNARY_PRECEDENCE = 9
_UNARY = {"-": "u-", "!": "u!", "~": "u~", "+": "u+"}

_NUMBER_RE = re.compile(r"(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?")
#: 标识符要能吃中文——车队的通道名就是「车速」「刹车压力」这种，逼用户每条都打
#: 单引号只会让人写错。`[^\W\d]` 是"是一个字、但不是数字"的写法，Python 的 \w
#: 默认是 Unicode 的，所以中文、希腊字母都算。
_IDENT_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)
_MULTI_OPS = ("<=", ">=", "==", "!=", "&&", "||")
_SINGLE_OPS = set("+-*/%^<>=!~&|")

#: 认通道名时"不算数"的字符。名字里的空格／短横线／下划线，在表达式里可以省掉
#: 或者换成另一个分隔符——``FSD13 Distance1`` 写成 ``FSD13Distance1`` 也得认。
_SEPARATOR_CHARS = " -_"


def _squash(text: str) -> str:
    """比名字"长相"用的规范形：大小写、空格、短横线、下划线都不参与比较。"""
    return "".join(ch for ch in text.lower() if ch not in _SEPARATOR_CHARS)


def _match_name_at(name: str, text: str, i: int) -> int | None:
    """``name`` 能不能从 ``text[i:]`` 认出来；能就给结束下标，不能给 ``None``。

    名字里的分隔符在文本里可有可无（可以省掉，也可以换成另一个分隔符），大小写
    也不计较。名字里的字母数字必须逐个对上——所以 ``FSD 13 Distance1`` 不会被
    认成 ``FSD13 Distance1``（数字前面凭空多一个空格，说明用户写的不是它）。
    """
    j = i
    length = len(text)
    for ch in name:
        if ch in _SEPARATOR_CHARS:
            while j < length and text[j] in _SEPARATOR_CHARS:
                j += 1
            continue
        if j >= length or text[j].lower() != ch.lower():
            return None
        j += 1
    return j


def _longest_name_at(text: str, i: int, known: Iterable[str] | None) -> tuple[str, int] | None:
    """``text[i:]`` 开头处最长的那个**真实通道名**，连同它占到的结束下标。

    通道名里有空格、括号、短横线（``Vx KF``、``Distance (2)``、``FSD-Distance1``），
    其中一多半**打不出来**：``Vx KF`` 看起来像两个运算数挨在一起，``FSD-Distance1``
    看起来像减法。调用方把"本场次有哪些通道"告诉编译器，就能在词法阶段认出最长
    的那个名字，用户按自然写法打出来即可，不必知道要加单引号。

    只认"一模一样"是不够的（实测：``FSD13Distance1`` 漏一个空格、``vx kf`` 小了
    两个字母就全都不认）。所以空格／短横线／下划线可以互换或省略，大小写也不计较；
    **精确写法永远优先**，同一个位置认出多条名字（本场次真有两条只差大小写的通道）
    就不猜，直接报错让用户把名字写全。

    返回的结束下标是**文本里的位置**，不是名字的长度——名字少写了空格时两者差一个
    字符，词法器只能按文本位置往前走。

    边界要卡死：名字后面紧跟字母数字或下划线就不算命中（``FSD13 Distance1`` 不能
    匹配掉 ``FSD13 Distance12`` 的前缀）。
    """
    if not known:
        return None
    ends: dict[int, list[str]] = {}
    for name in known:
        if not name:
            continue
        end = _match_name_at(name, text, i)
        if end is None:
            continue
        if end < len(text) and (text[end].isalnum() or text[end] == "_"):
            continue
        ends.setdefault(end, []).append(name)
    if not ends:
        return None
    end = max(ends)
    names = ends[end]
    exact = [item for item in names if text[i:end] == item]
    if exact:
        return exact[0], end
    if len(names) == 1:
        return names[0], end
    shown = "、".join(f"`{item}`" for item in sorted(names))
    raise MathError(
        f"`{text[i:end]}` 在本场次对上不止一条通道：{shown}。"
        f"把名字写全再算一次（含空格／短横线的用单引号写成 `'{sorted(names)[0]}'`）。"
    )


def _canonical_name(name: str, known: Iterable[str] | None) -> str:
    """把用户写的名字对到本场次真实的那一条上；对不上就原样返回。

    单引号里的名字按说应当逐字一致，但用户是从别处复制来的、或者照着自己记的
    写法打的，同样只差一个空格或大小写——那就和没加引号时一个待遇，别逼他改三次。
    对不上（或者对上好几条）就原样返回，交给"本场次没有这个通道"那条报错去解释。
    """
    if not known:
        return name
    if name in known:
        return name
    squashed = _squash(name)
    if not squashed:
        return name
    hits = [item for item in known if _squash(item) == squashed]
    return hits[0] if len(hits) == 1 else name


def _tokenize(
    text: str, known: Iterable[str] | None = None
) -> list[tuple[str, str, object]]:
    """切成 ``(kind, text, value)`` 序列。``kind`` ∈ num/chan/func/op/lparen/rparen/comma。

    ``known`` 是**已知通道名**（本场次的通道 + 已定义的数学通道）。给了它，
    含空格／括号／短横线的名字可以不写单引号直接打。
    """
    known = tuple(known) if known else ()
    tokens: list[tuple[str, str, object]] = []
    units: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            end = text.find("'", i + 1)
            if end < 0:
                raise MathError(
                    "通道名少了一个收尾的单引号：`'刹车压力` 这种写法要写成 `'刹车压力'`。"
                )
            name = text[i + 1 : end].strip()
            if not name:
                raise MathError("有一对空单引号。通道名要写在引号里面，例如 `'Vx KF'`。")
            name = _canonical_name(name, known)
            tokens.append(("chan", name, name))
            i = end + 1
            # '通道'[单位] —— 单位标注接受但不换算
            if i < n and text[i] == "[":
                close = text.find("]", i + 1)
                if close < 0:
                    raise MathError(f"`{name}` 后面的单位方括号没有收尾，补一个 `]`。")
                units.append(text[i + 1 : close].strip())
                i = close + 1
            continue
        if ch == '"':
            raise MathError(
                "暂时不支持双引号文本（i2 Pro 用它比较 `'Driver' == \"Joe\"`）。"
                "请先用 `choose()` 把文本条件换成数值条件。"
            )
        number = _NUMBER_RE.match(text, i)
        if number:
            raw = number.group(0)
            tokens.append(("num", raw, float(raw)))
            i = number.end()
            continue
        ident = _IDENT_RE.match(text, i)
        if ident:
            word = ident.group(0)
            j = ident.end()
            while j < n and text[j].isspace():
                j += 1
            # 常数（pi / e）优先：它们就是被当成数字用的，别被同名的通道抢走
            known_here = () if word in _CONSTANTS else known
            if j < n and text[j] == "(":
                hit = _longest_name_at(text, i, known_here)
                if hit is not None and hit[0] != word and hit[0].endswith((")", "]")):
                    # `Distance (2)` 这种带括号的通道名，别被当成函数调用
                    tokens.append(("chan", hit[0], hit[0]))
                    i = hit[1]
                    continue
                tokens.append(("func", word, word))
            else:
                hit = _longest_name_at(text, i, known_here)
                if hit is not None and hit[0] != word:
                    tokens.append(("chan", hit[0], hit[0]))
                    i = hit[1]
                    continue
                tokens.append(("ident", word, word))
            i = ident.end()
            continue
        if text.startswith(_MULTI_OPS, i):
            op = text[i : i + 2]
            tokens.append(("op", op, op))
            i += 2
            continue
        if ch in _SINGLE_OPS:
            tokens.append(("op", ch, ch))
            i += 1
            continue
        if ch == "(":
            tokens.append(("lparen", ch, ch))
            i += 1
            continue
        if ch == ")":
            tokens.append(("rparen", ch, ch))
            i += 1
            continue
        if ch == ",":
            tokens.append(("comma", ch, ch))
            i += 1
            continue
        raise MathError(f"看不懂的字符 `{ch}`（第 {i + 1} 个字符）。支持的运算符见界面上的函数表。")
    if not tokens:
        raise MathError("表达式是空的。至少写一个通道名或数字。")
    return tokens, units


@dataclass
class Plan:
    """一条编译好的表达式：逆波兰序列 + 它用到的东西。"""

    source: str
    rpn: list[tuple[str, str, object]]
    channels: tuple[str, ...]
    functions: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def eval(self, ctx: "Context", resolve) -> np.ndarray:
        """在 ``ctx`` 上求值；``resolve(name)`` 给出一个通道的样本序列。"""
        stack: list[object] = []
        for kind, text, value in self.rpn:
            if kind == "num":
                stack.append(value)
            elif kind in ("chan", "ident"):
                stack.append(resolve(value))
            elif kind == "call":
                impl = FUNCTIONS.get(value)
                if impl is None:  # pragma: no cover - compile 已经挡过
                    raise MathError(f"未知函数 `{value}`。")
                count = int(text)
                if count > len(stack):
                    raise MathError(
                        f"`{value}(...)` 少了参数（要 {count} 个，只凑出 {len(stack)} 个）。"
                    )
                args = [stack.pop() for _ in range(count)][::-1] if count else []
                stack.append(impl(ctx, *args))
            elif kind == "op":
                impl = _OPERATORS.get(value)
                if impl is None:  # pragma: no cover
                    raise MathError(f"未知运算符 `{value}`。")
                argc, call = impl
                args = [stack.pop() for _ in range(argc)][::-1]
                stack.append(call(ctx, *args))
        if len(stack) != 1:
            raise MathError(
                f"`{self.source}` 运算数对不上（多余了 {len(stack) - 1} 个值）。"
                "检查是不是漏了一个运算符或者括号没配平。"
            )
        out = stack[0]
        if np.isscalar(out) or (isinstance(out, np.ndarray) and out.ndim == 0):
            out = np.full(ctx.time.size, float(out))
        return np.asarray(out)

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "channels": list(self.channels),
            "functions": list(self.functions),
            "notes": list(self.notes),
        }


def compile_expr(
    text: str,
    known: Iterable[str] | None = None,
    strict_channels: bool = False,
) -> Plan:
    """把表达式文本编译成 :class:`Plan`；出错时抛 :class:`MathError`。

    ``known`` 是已知通道名（本场次的通道 + 已定义的数学通道）。给了它，``Vx KF``、
    ``Distance (2)``、``FSD-Distance1`` 这些名字就能直接打，不必套单引号；
    没给也能编译，只是这些名字得写成 ``'Vx KF'``。

    ``strict_channels`` 打开时，表达式里用到的通道必须都在 ``known`` 里，否则
    在这里就报错（"本场次没有这个通道" + 该改成什么）。界面上的「试算」和本地
    定义的保存都打开它——用户不该等到画图时才发现名字打错了。**全局定义不开**：
    它本来就是跨场次复用的，某一场缺那条通道是正常情况，不该拦着不让存。
    """
    if not isinstance(text, str) or not text.strip():
        raise MathError("表达式是空的。至少写一个通道名或数字。")
    tokens, units = _tokenize(text, known)
    rpn: list[tuple[str, str, object]] = []
    # 栈项：["op", 文本, 运算符] / ["func", 名字, 已数到的逗号数] /
    #       ["lparen", 文本, 文本, 进入时的 rpn 长度]
    stack: list[list] = []
    channels: list[str] = []
    functions: list[str] = []
    expecting_operand = True
    #: 上一个写出来的运算数（文本），只为在"两个运算数挨在一起"时给出更好的提示。
    previous_operand: str | None = None

    def flush_operators(precedence: int | None) -> None:
        """把栈上优先级更高的运算符搬到输出（shunting-yard 的核心一步）。"""
        while stack and stack[-1][0] == "op":
            top = str(stack[-1][2])
            top_prec, assoc = (
                (_UNARY_PRECEDENCE, "right") if top.startswith("u") else _BINARY[top]
            )
            if precedence is None or top_prec > precedence or (top_prec == precedence and assoc == "left"):
                item = stack.pop()
                rpn.append((item[0], item[1], item[2]))
            else:
                break

    for kind, token_text, value in tokens:
        if kind == "num":
            if not expecting_operand:
                raise MathError(
                    _two_operands_message(text, token_text, previous_operand, known)
                )
            rpn.append((kind, token_text, value))
            expecting_operand = False
            previous_operand = token_text
        elif kind in ("chan", "ident"):
            if not expecting_operand:
                raise MathError(
                    _two_operands_message(text, token_text, previous_operand, known)
                )
            if kind == "ident" and token_text in _CONSTANTS:
                rpn.append(("num", token_text, _CONSTANTS[token_text]))
            else:
                channels.append(str(value))
                rpn.append(("chan", token_text, value))
            expecting_operand = False
            previous_operand = str(value)
        elif kind == "func":
            name = str(value)
            if name not in FUNCTIONS:
                if name in UNSUPPORTED:
                    raise MathError(f"函数 `{name}` 没有提供。{UNSUPPORTED[name]}")
                lookalike = _looks_like_a_channel(name, known)
                if lookalike is not None:
                    raise MathError(
                        f"`{name}` 不是函数，但本场次有一个通道叫 `{lookalike}`。"
                        f"把它用单引号括起来写成 `'{lookalike}'`，"
                        f"或者从编辑器里的「插入通道」直接选。"
                    )
                raise MathError(
                    f"未知函数 `{name}`。"
                    f"最接近的是 {_suggest(name, FUNCTIONS)}；函数全表见界面上的「函数」按钮。"
                )
            functions.append(name)
            stack.append(["func", name, 0])
            expecting_operand = True
            previous_operand = None
        elif kind == "lparen":
            stack.append(["lparen", token_text, token_text, len(rpn)])
            expecting_operand = True
            previous_operand = None
        elif kind == "rparen":
            flush_operators(None)
            if not stack:
                raise MathError(f"`{text}` 里有一个多余的右括号。删掉它，或者在前面补一个左括号。")
            opened = stack.pop()
            if stack and stack[-1][0] == "func":
                entry = stack.pop()
                empty = len(rpn) == int(opened[3])
                argc = 0 if empty else int(entry[2]) + 1
                _check_arity(str(entry[1]), argc, text)
                rpn.append(("call", str(argc), entry[1]))
            expecting_operand = False
            previous_operand = None
        elif kind == "comma":
            flush_operators(None)
            if not stack or stack[-1][0] != "lparen":
                raise MathError(f"`{text}` 里有一个逗号不在函数调用里面。把它放进 `函数(参数, 参数)` 里。")
            if len(stack) < 2 or stack[-2][0] != "func":
                raise MathError(f"`{text}` 里有一个逗号不在函数调用里面。把它放进 `函数(参数, 参数)` 里。")
            stack[-2][2] = int(stack[-2][2]) + 1
            expecting_operand = True
            previous_operand = None
        else:  # op
            op = str(value)
            if expecting_operand:
                if op not in _UNARY:
                    raise MathError(f"`{text}` 里运算符 `{op}` 少了一个运算数。在它后面补一个通道名或数字。")
                stack.append(["op", _UNARY[op], _UNARY[op]])
                previous_operand = None
                continue
            flush_operators(_BINARY[op][0])
            stack.append(["op", op, op])
            expecting_operand = True
            previous_operand = None
    if expecting_operand:
        raise MathError(f"`{text}` 结尾还缺一个运算数。补一个通道名或数字。")
    while stack:
        item = stack.pop()
        if item[0] in ("lparen", "func"):
            raise MathError(f"`{text}` 里的括号没配平。检查 `(` 和 `)` 的数量。")
        rpn.append((item[0], item[1], item[2]))
    used = list(dict.fromkeys(channels))
    notes = []
    for unit in units:
        if unit:
            notes.append(f"单位标注 [{unit}] 被忽略：i3pro 目前不做单位换算。")
    plan = Plan(
        source=text,
        rpn=rpn,
        channels=tuple(used),
        functions=tuple(dict.fromkeys(functions)),
        notes=tuple(notes),
    )
    if strict_channels:
        check_channels(plan, known)
    return plan


def check_channels(plan: Plan, known: Iterable[str] | None) -> None:
    """表达式用到的通道，本场次是不是都有；缺哪条就说清那条该改成什么。"""
    if not known:
        return
    have = set(known)
    for name in plan.channels:
        if name not in have:
            raise MathError(_unknown_channel_message(name, known))


def _suggest(name: str, table) -> str:
    """给拼错的函数名挑一个最接近的。"""
    best, best_score = None, 0.0
    for candidate in table:
        score = _similarity(name.lower(), candidate.lower())
        if score > best_score:
            best, best_score = candidate, score
    return f"`{best}`" if best and best_score >= 0.5 else "（没找到相近的名字）"


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    common = sum(1 for ch in set(a) if ch in b)
    return common / max(len(set(a)), len(set(b)))


def _looks_like_a_channel(name: str, known: Iterable[str] | None) -> str | None:
    """``name(...)`` 里的 ``name`` 其实是某个通道名的开头时，交出那个通道名。

    ``Distance (2)`` 会被词法当成"调用函数 Distance"，而它只是一个带括号的通道名。
    """
    if not known:
        return None
    hits = [candidate for candidate in known
            if candidate != name and candidate.startswith(name)]
    return min(hits, key=len) if hits else None


def _closest_channel(name: str, known: Iterable[str] | None) -> str | None:
    """拼错的通道名最像哪一条（不像就返回 ``None``）。

    空格、短横线、下划线和大小写的差别要先抹平再比：用户打 ``FSD13Distance1``、
    实际那条叫 ``FSD13 Distance1``，这两个串按原样比是比不上"长相"的。
    相似度用 :class:`difflib.SequenceMatcher`（标准库）而不是"共用字符比例"——
    后者会把 ``notachannel`` 这种完全无关的串也认成 ``Aceinna Roll``，比不给建议更糟。
    """
    if not known:
        return None

    best, best_score = None, 0.0
    matcher = difflib.SequenceMatcher()

    def ratio(a: str, b: str) -> float:
        matcher.set_seqs(a, b)
        return matcher.ratio()

    for candidate in known:
        score = max(ratio(name.lower(), candidate.lower()),
                    ratio(_squash(name), _squash(candidate)))
        # "像"要有下限：开头就对不上、整体也不太像的，不给建议反而更诚实
        # （`notachannel` 与 `Channel 9` 只因为都含 channel 就会被算成"最接近"）。
        if _prefix_len(_squash(name), _squash(candidate)) < 2 and score < 0.85:
            continue
        if score > best_score:
            best, best_score = candidate, score
    return best if best is not None and best_score >= 0.7 else None


def _prefix_len(a: str, b: str) -> int:
    """两个串从头开始相同的字符数。"""
    for index, (left, right) in enumerate(zip(a, b)):
        if left != right:
            return index
    return min(len(a), len(b))


def _unknown_channel_message(name: str, known: Iterable[str] | None) -> str:
    """“本场次没有这个通道”到底该怎么办：把像的名字端出来，而不是只说不认识。

    实测过的难处：用户把 ``FSD13 Distance1`` 记成 ``FSD-Distance1``，表达式会被
    切成 ``FSD`` 减 ``Distance1``，报错只说 ``FSD`` 不存在——他还是不知道那条叫
    什么。所以先列"以他打的字开头的通道"（这一条最贴近本意），再退到"最像的一条"。
    """
    names = [str(item) for item in (known or ())]
    family = _name_family(name, names)
    closest = _closest_channel(name, names)
    if family:
        shown = "、".join(f"`{item}`" for item in family[:3])
        hint = f"本场次以 `{name}` 开头的通道有：{shown}。"
    elif closest:
        hint = f"最接近的是 `{closest}`。"
    else:
        hint = "检查拼写（区分大小写和空格）。"
    return (
        f"表达式里用到通道 `{name}`，本场次没有这个通道。{hint}"
        f"含空格／括号／短横线的名字可以直接打（例如 `Vx KF`），也可以写成 `'Vx KF'`，"
        f"或者从编辑器里的「插入通道」直接选。"
    )


def _name_family(name: str, known: Iterable[str] | None) -> list[str]:
    """以 ``name`` 开头的通道名（短的排前面）。

    多词的名字打错中间一个字母，表达式就会被切成"两个运算数挨在一起"：用户打
    ``G Force Late``（真名是 ``G Force Lat``），报错说的是 ``Force``。这时候把
    "以 ``G Force`` 开头的那几条"列出来，他就知道该点哪一条了。
    """
    squashed = _squash(name)
    if not squashed:
        return []
    return sorted(
        (
            item for item in (known or ())
            if _squash(item) != squashed and _squash(item).startswith(squashed)
        ),
        key=lambda item: (len(item), item),
    )


def _two_operands_message(
    text: str, token_text: str, previous: str | None, known: Iterable[str] | None
) -> str:
    """两个运算数挨在一起：先按语法说，再猜"是不是通道名打错了"。"""
    message = f"`{text}` 里两个运算数挨在一起了（`{token_text}`）。中间补一个运算符。"
    if not known or not previous:
        return message
    joined = f"{previous} {token_text}"
    family = _name_family(joined, known)
    if family:
        shown = "、".join(f"`{item}`" for item in family[:3])
        hint = f"本场次以 `{joined}` 开头的通道有：{shown}。"
    else:
        closest = _closest_channel(joined, known)
        if closest is None:
            return message
        hint = f"最接近的是 `{closest}`。"
    return (
        f"{message}如果这是通道名写错了（多词的名字少一个字母就会变成这样），"
        f"{hint}通道名可以直接打，也可以从编辑器里的「插入通道」选。"
    )


# ------------------------------------------------------------------- 求值


@dataclass(frozen=True)
class Context:
    """求值需要的时间信息。逐样本函数用不到它，积分／滤波用到。"""

    time: np.ndarray
    rate: float

    @property
    def dt(self) -> float:
        return 1.0 / self.rate if self.rate else 0.0

    def size(self) -> int:
        return int(self.time.size)


def _as_array(ctx: Context, value) -> np.ndarray:
    arr = np.asarray(value, dtype=np.float64)
    if arr.ndim == 0:
        return np.full(ctx.size(), float(arr))
    if arr.size == ctx.size():
        return arr
    # 长度对不上：按最后一个样本补齐 / 截断，和 hold_to_master 的处理一致
    if arr.size > ctx.size():
        return arr[: ctx.size()]
    pad = arr[-1] if arr.size else np.nan
    return np.concatenate([arr, np.full(ctx.size() - arr.size, pad)])


def _as_bool(ctx: Context, value) -> np.ndarray:
    return _as_array(ctx, value) != 0


def _as_int(ctx: Context, value) -> np.ndarray:
    return np.asarray(np.rint(_as_array(ctx, value)), dtype=np.int64)


def _window_points(ctx: Context, value, default_points: int) -> int:
    """``smooth(x, 7)`` = 7 个采样；``smooth(x, 0.05)`` = 0.05 秒。

    i2 Pro 靠单位（``[s]``）区分这两种写法，i3pro 不做单位换算，所以用**是不是
    整数**来分：写整数就是点数，写小数就是秒。界面上也这么提示。
    """
    if value is None:
        return default_points
    number = float(np.asarray(value).reshape(-1)[0]) if np.asarray(value).size else float(default_points)
    if number <= 0:
        raise MathError(f"窗口长度要大于 0，收到 {number:g}。")
    points = int(round(number)) if float(number).is_integer() else int(round(number * ctx.rate))
    return max(1, points)


def _moving_average(ctx: Context, x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return x
    if window % 2 == 0:  # 偶数窗口会让结果整体移半个采样，凑成奇数
        window += 1
    half = window // 2
    padded = np.concatenate([np.full(half, x[0]), x, np.full(half, x[-1])])
    kernel = np.ones(window, dtype=np.float64) / window
    return np.convolve(padded, kernel, mode="valid")


def _segments(ctx: Context, reset) -> np.ndarray:
    """把时间轴按 ``reset`` 的上升沿切成若干段，返回每段的下标区间。"""
    flags = _as_bool(ctx, reset) if reset is not None else np.zeros(ctx.size(), dtype=bool)
    if flags.size and flags[0]:
        flags[0] = True
    edges = np.flatnonzero(flags)
    bounds: list[tuple[int, int]] = []
    start = 0
    for edge in edges:
        if edge > start:
            bounds.append((start, int(edge)))
        start = int(edge)
    if start < ctx.size():
        bounds.append((start, ctx.size()))
    return bounds


def _interval_stat(ctx: Context, x: np.ndarray, cond, reset, reducer) -> np.ndarray:
    """区间统计：每一段（由 reset 切分、由 cond 筛选）算一个常数，铺满整段。

    段里一个合格样本都没有时给 NaN，而不是 0——0 会被当成一个真实测量值。

    ``reset`` 的语义是**脉冲**，不是"区间还开着"：``range_change(起, 止)`` 只在这
    个区间开始的那一刻给一个 1，于是它把时间轴切成"从这一刻到下一次脉冲之前"。
    想看一个弯或一圈的统计，就把每一段的起点都做成脉冲（例如
    ``range_change(0, 20) + range_change(60, 80)``），再用 ``cond`` 决定这段里
    哪些样本算数。
    """
    out = np.full(x.size, np.nan)
    for lo, hi in _segments(ctx, reset):
        chunk = x[lo:hi]
        if cond is not None:
            keep = _as_bool(ctx, cond)[lo:hi]
            chunk = chunk[keep]
        chunk = chunk[np.isfinite(chunk)]
        if chunk.size:
            out[lo:hi] = reducer(chunk)
    return out


def _biquad_lowpass(ctx: Context, x: np.ndarray, cutoff: float) -> np.ndarray:
    """二阶巴特沃斯低通（双线性变换），系数由截止频率现算。

    不引入 scipy：``numpy`` 只有 ``convolve``，而 IIR 必须逐样本递推，所以这里
    用一步 ``lfilter`` 的朴素实现。频响与 scipy 的 ``butter(2, ...)`` 一致。
    """
    if cutoff <= 0:
        raise MathError(f"截止频率要大于 0，收到 {cutoff:g}。")
    nyquist = ctx.rate / 2.0
    if cutoff >= nyquist:
        return x.copy()
    w = _math.tan(_math.pi * cutoff / ctx.rate)
    k = w * w
    norm = 1.0 + _math.sqrt(2.0) * w + k
    b = np.array([k, 2 * k, k]) / norm
    a = np.array([1.0, 2.0 * (k - 1.0) / norm, (1.0 - _math.sqrt(2.0) * w + k) / norm])
    out = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i in range(x.size):
        value = x[i]
        if not np.isfinite(value):
            value = 0.0
        y = b[0] * value + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
        out[i] = y
        x2, x1 = x1, value
        y2, y1 = y1, y
    return out


def _biquad_highpass(ctx: Context, x: np.ndarray, cutoff: float) -> np.ndarray:
    low = _biquad_lowpass(ctx, x, cutoff)
    return x - low


@dataclass(frozen=True)
class _Func:
    min_args: int
    max_args: int          # -1 = 不限
    impl: object
    doc: str

    def __call__(self, ctx: Context, *args) -> np.ndarray:
        return self.impl(ctx, *args)


def _f(name: str, nargs, impl, doc: str) -> tuple[str, _Func]:
    lo, hi = (nargs if isinstance(nargs, tuple) else (int(nargs), int(nargs)))
    return name, _Func(lo, hi, impl, doc)


def _check_arity(name: str, argc: int, text: str) -> None:
    func = FUNCTIONS[name]
    too_few = argc < func.min_args
    too_many = func.max_args >= 0 and argc > func.max_args
    if not (too_few or too_many):
        return
    if func.min_args == func.max_args:
        want = f"{func.min_args}"
    elif func.max_args < 0:
        want = f"至少 {func.min_args}"
    else:
        want = f"{func.min_args}~{func.max_args}"
    raise MathError(
        f"`{name}` 需要 {want} 个参数，`{text}` 里给了 {argc} 个。"
        f"用法：{func.doc}"
    )


#: 白名单函数表。键就是表达式里写的名字（大小写敏感，与 i2 Pro 一致）。
FUNCTIONS: dict[str, _Func] = dict(
    [
        # 三角
        _f("sin", 1, lambda c, x: np.sin(_as_array(c, x)), "正弦（弧度）"),
        _f("cos", 1, lambda c, x: np.cos(_as_array(c, x)), "余弦（弧度）"),
        _f("tan", 1, lambda c, x: np.tan(_as_array(c, x)), "正切（弧度）"),
        _f("asin", 1, lambda c, x: np.arcsin(_as_array(c, x)), "反正弦（弧度）"),
        _f("acos", 1, lambda c, x: np.arccos(_as_array(c, x)), "反余弦（弧度）"),
        _f("atan", 1, lambda c, x: np.arctan(_as_array(c, x)), "反正切（弧度）"),
        _f("sinh", 1, lambda c, x: np.sinh(_as_array(c, x)), "双曲正弦"),
        _f("cosh", 1, lambda c, x: np.cosh(_as_array(c, x)), "双曲余弦"),
        _f("tanh", 1, lambda c, x: np.tanh(_as_array(c, x)), "双曲正切"),
        _f("asinh", 1, lambda c, x: np.arcsinh(_as_array(c, x)), "反双曲正弦"),
        _f("acosh", 1, lambda c, x: np.arccosh(_as_array(c, x)), "反双曲余弦"),
        _f("atanh", 1, lambda c, x: np.arctanh(_as_array(c, x)), "反双曲正切"),
        # 对数 / 指数
        _f("ln", 1, lambda c, x: np.log(_as_array(c, x)), "自然对数"),
        _f("log", 1, lambda c, x: np.log10(_as_array(c, x)), "常用对数（以 10 为底）"),
        _f("exp", 1, lambda c, x: np.exp(_as_array(c, x)), "e 的幂"),
        _f("exp10", 1, lambda c, x: np.power(10.0, _as_array(c, x)), "10 的幂"),
        # 幂 / 根
        _f("sqr", 1, lambda c, x: np.square(_as_array(c, x)), "平方"),
        _f("sqrt", 1, lambda c, x: np.sqrt(_as_array(c, x)), "平方根"),
        _f("power", 2, lambda c, x, y: np.power(_as_array(c, x), _as_array(c, y)), "power(x, y) = x 的 y 次方"),
        _f("hypot", 2, lambda c, x, y: np.hypot(_as_array(c, x), _as_array(c, y)), "直角三角形斜边"),
        # 取整
        _f("int", 1, lambda c, x: np.trunc(_as_array(c, x)), "向零取整"),
        _f("round", 1, lambda c, x: np.round(_as_array(c, x)), "四舍五入"),
        _f("round_down", 1, lambda c, x: np.floor(_as_array(c, x)), "向下取整"),
        _f("round_up", 1, lambda c, x: np.ceil(_as_array(c, x)), "向上取整"),
        _f("frac", 1, lambda c, x: _as_array(c, x) - np.trunc(_as_array(c, x)), "小数部分"),
        _f("remainder", 2, lambda c, x, y: np.remainder(_as_array(c, x), _as_array(c, y)), "取余"),
        _f("sgn", 1, lambda c, x: np.sign(_as_array(c, x)), "符号：-1 / 0 / 1"),
        # 统计
        _f("abs", 1, lambda c, x: np.abs(_as_array(c, x)), "绝对值"),
        _f("min", 2, lambda c, x, y: np.minimum(_as_array(c, x), _as_array(c, y)), "两个数的逐样本较小值"),
        _f("max", 2, lambda c, x, y: np.maximum(_as_array(c, x), _as_array(c, y)), "两个数的逐样本较大值"),
        # 区间统计：stat_xxx(x, 条件, 复位)
        _f("stat_min", (1, 3), lambda c, x, cond=None, reset=None: _interval_stat(c, _as_array(c, x), cond, reset, np.min), "stat_min(x, 条件, 复位)：区间最小值，后两个参数可省略"),
        _f("stat_max", (1, 3), lambda c, x, cond=None, reset=None: _interval_stat(c, _as_array(c, x), cond, reset, np.max), "stat_max(x, 条件, 复位)：区间最大值，后两个参数可省略"),
        _f("stat_mean", (1, 3), lambda c, x, cond=None, reset=None: _interval_stat(c, _as_array(c, x), cond, reset, np.mean), "stat_mean(x, 条件, 复位)：区间平均值，后两个参数可省略"),
        _f("stat_std_dev", (1, 3), lambda c, x, cond=None, reset=None: _interval_stat(c, _as_array(c, x), cond, reset, np.std), "stat_std_dev(x, 条件, 复位)：区间标准差，后两个参数可省略"),
        _f("stat_start", (1, 3), lambda c, x, cond=None, reset=None: _interval_stat(c, _as_array(c, x), cond, reset, lambda a: a[0]), "stat_start(x, 条件, 复位)：区间内第一个合格样本"),
        _f("stat_end", (1, 3), lambda c, x, cond=None, reset=None: _interval_stat(c, _as_array(c, x), cond, reset, lambda a: a[-1]), "stat_end(x, 条件, 复位)：区间内最后一个合格样本"),
        # 平滑 / 滤波
        _f("smooth", (1, 2), lambda c, x, w=None: _moving_average(c, _as_array(c, x), _window_points(c, w, 5)), "smooth(x, 窗口)：滑动平均，整数=点数、小数=秒；省略窗口则用 5 点"),
        _f("filter_lp", 2, lambda c, x, f: _biquad_lowpass(c, _as_array(c, x), float(_as_array(c, f)[0])), "二阶巴特沃斯低通：filter_lp(x, 截止频率Hz)"),
        _f("filter_hp", 2, lambda c, x, f: _biquad_highpass(c, _as_array(c, x), float(_as_array(c, f)[0])), "二阶巴特沃斯高通：filter_hp(x, 截止频率Hz)"),
        # 微分 / 积分
        _f("derivative", (1, 2), lambda c, x, w=None: _derivative(c, _as_array(c, x), _window_points(c, w, 3)), "derivative(x, 窗口)：中心差分，整数=点数、小数=秒；省略窗口则用 3 点"),
        _f("integrate", (1, 3), lambda c, x, cond=None, reset=None: _integrate(c, _as_array(c, x), cond, reset), "integrate(x, 条件, 复位)：梯形累积积分，后两个参数可省略"),
        # 逻辑 / 选择
        _f("choose", 3, lambda c, cond, a, b: np.where(_as_bool(c, cond), _as_array(c, a), _as_array(c, b)), "条件选择：choose(条件, 真值, 假值)"),
        _f("invalid", 0, lambda c: np.full(c.size(), np.nan), "无效标记：把这一段标成 NaN，从图表与报表里排除"),
        _f("flip_flop", 2, lambda c, s, r: _flip_flop(c, s, r), "置位／复位触发器：flip_flop(set, reset)"),
        # 时间
        _f("time_shift", 2, lambda c, x, s: _time_shift(c, _as_array(c, x), float(_as_array(c, s)[0])), "整体平移：time_shift(x, 秒)（正数=推后）"),
        _f("time_valid", 2, lambda c, x, d: _time_valid(c, x, float(_as_array(c, d)[0])), "持续判定：x 连续非零达到 N 秒才为真"),
        _f("range_is", 2, lambda c, a, b: ((c.time >= float(_as_array(c, a)[0])) & (c.time <= float(_as_array(c, b)[0]))).astype(np.float64), "时间区间判定：range_is(起秒, 止秒)，用于 gating"),
        _f("range_change", 2, lambda c, a, b: _range_change(c, a, b), "range_is 的上升沿，用作 reset 参数"),
        # 位运算
        _f("bit_and", 2, lambda c, x, y: np.bitwise_and(_as_int(c, x), _as_int(c, y)).astype(np.float64), "按位与"),
        _f("bit_or", 2, lambda c, x, y: np.bitwise_or(_as_int(c, x), _as_int(c, y)).astype(np.float64), "按位或"),
        _f("bit_xor", 2, lambda c, x, y: np.bitwise_xor(_as_int(c, x), _as_int(c, y)).astype(np.float64), "按位异或"),
        _f("bit_not", 1, lambda c, x: np.bitwise_not(_as_int(c, x)).astype(np.float64), "按位取反"),
        # 边缘
        _f("edge_delay", (2, 3), lambda c, x, t, kind=-1: _edge_delay(c, x, float(_as_array(c, t)[0]), kind), "edge_delay(x, 秒, 模式)：模式 0=只延迟上升沿、1=只延迟下降沿、省略=整体平移"),
    ]
)


#: MoTeC 有、i3pro 明确不提供的函数。报错时直接说清楚为什么、怎么办。
UNSUPPORTED: dict[str, str] = {
    "filter_cheby_lp": "切比雪夫滤波器需要 scipy 才做得对，而本项目的运行期依赖只有 numpy/pandas/pyarrow。改用 filter_lp(x, 截止频率Hz)。",
    "filter_cheby_hp": "同上：改用 filter_hp(x, 截止频率Hz)。",
    "filter_cheby_bp": "带通需要 scipy。先用 filter_hp 与 filter_lp 串起来。",
    "filter_cheby_bs": "带阻需要 scipy。先用 filter_hp 与 filter_lp 串起来。",
    "rand_chan": "随机通道不可复现，做出来的圈速没有意义，因此不提供。",
    "rand_val": "随机数不可复现，因此不提供。",
}


def _derivative(ctx: Context, x: np.ndarray, window: int) -> np.ndarray:
    half = max(1, window // 2)
    lo = np.clip(np.arange(x.size) - half, 0, x.size - 1)
    hi = np.clip(np.arange(x.size) + half, 0, x.size - 1)
    span = (hi - lo) / ctx.rate
    span = np.where(span <= 0, ctx.dt, span)
    return (x[hi] - x[lo]) / span


def _integrate(ctx: Context, x: np.ndarray, cond, reset) -> np.ndarray:
    dt = ctx.dt
    gate = _as_bool(ctx, cond) if cond is not None else np.ones(x.size, dtype=bool)
    previous = np.concatenate([[x[0]], x[:-1]])
    step = np.where(gate, 0.5 * (x + previous) * dt, 0.0)
    step[0] = 0.0
    total = np.cumsum(step)
    out = total.copy()
    for lo, _hi in _segments(ctx, reset):
        if lo > 0:
            out[lo:] -= total[lo]
    return out


def _flip_flop(ctx: Context, set_, reset) -> np.ndarray:
    set_flags = _as_bool(ctx, set_)
    reset_flags = _as_bool(ctx, reset)
    index = np.arange(ctx.size())
    last_set = np.maximum.accumulate(np.where(set_flags, index, -1))
    last_reset = np.maximum.accumulate(np.where(reset_flags, index, -1))
    return (last_set > last_reset).astype(np.float64)


def _time_shift(ctx: Context, x: np.ndarray, seconds: float) -> np.ndarray:
    """把整条曲线沿时间轴平移：正数表示推后。空出来的两头补 NaN。"""
    if seconds == 0:
        return x.copy()
    offset = seconds * ctx.rate
    target = np.arange(x.size) - offset
    out = np.interp(target, np.arange(x.size), x, left=np.nan, right=np.nan)
    out[(target < 0) | (target > x.size - 1)] = np.nan
    return out


def _time_valid(ctx: Context, x, seconds: float) -> np.ndarray:
    flags = _as_bool(ctx, x)
    index = np.arange(flags.size)
    starts = flags & ~np.concatenate([[False], flags[:-1]])
    run_start = np.maximum.accumulate(np.where(starts, index, 0))
    elapsed = (index - run_start) / ctx.rate
    return (flags & (elapsed >= seconds)).astype(np.float64)


def _range_change(ctx: Context, a, b) -> np.ndarray:
    inside = (ctx.time >= float(_as_array(ctx, a)[0])) & (ctx.time <= float(_as_array(ctx, b)[0]))
    previous = np.concatenate([[False], inside[:-1]])
    return (inside & ~previous).astype(np.float64)


def _edge_delay(ctx: Context, x, seconds: float, kind) -> np.ndarray:
    """把 ``x`` 的值沿时间轴推后 N 秒；``kind`` 保留给"只延迟上升沿/下降沿"。

    0 = 只延迟上升沿（保持高电平更久），1 = 只延迟下降沿，其它值 = 整个信号平移。
    """
    flags = _as_bool(ctx, x)
    try:
        mode = int(float(np.asarray(kind).reshape(-1)[0])) if np.asarray(kind).size else -1
    except (TypeError, ValueError):
        mode = -1
    index = np.arange(flags.size)
    shift = max(0, int(round(seconds * ctx.rate)))
    if shift == 0:
        return flags.astype(np.float64)
    rising = flags & ~np.concatenate([[False], flags[:-1]])
    falling = ~flags & np.concatenate([[False], flags[:-1]])
    if mode == 0:
        on = np.maximum.accumulate(np.where(rising, index + shift, -1))
        off = np.maximum.accumulate(np.where(falling, index, -1))
        return (on > off).astype(np.float64)
    if mode == 1:
        on = np.maximum.accumulate(np.where(rising, index, -1))
        off = np.maximum.accumulate(np.where(falling, index + shift, -1))
        return (on > off).astype(np.float64)
    shifted = np.zeros(flags.size, dtype=bool)
    if shift < flags.size:
        shifted[shift:] = flags[:-shift]
    return shifted.astype(np.float64)


def _op_arithmetic(ctx: Context, a, b, func):
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return func(_as_array(ctx, a), _as_array(ctx, b))


def _unary_arithmetic(ctx: Context, a, func):
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        return func(_as_array(ctx, a))


#: 运算符 -> (参数个数, 实现)
_OPERATORS: dict[str, tuple[int, object]] = {
    "+": (2, lambda c, a, b: _op_arithmetic(c, a, b, np.add)),
    "-": (2, lambda c, a, b: _op_arithmetic(c, a, b, np.subtract)),
    "*": (2, lambda c, a, b: _op_arithmetic(c, a, b, np.multiply)),
    "/": (2, lambda c, a, b: _op_arithmetic(c, a, b, np.divide)),
    "%": (2, lambda c, a, b: _op_arithmetic(c, a, b, np.remainder)),
    "^": (2, lambda c, a, b: _op_arithmetic(c, a, b, np.power)),
    "==": (2, lambda c, a, b: np.equal(_as_array(c, a), _as_array(c, b)).astype(np.float64)),
    "!=": (2, lambda c, a, b: np.not_equal(_as_array(c, a), _as_array(c, b)).astype(np.float64)),
    "<": (2, lambda c, a, b: np.less(_as_array(c, a), _as_array(c, b)).astype(np.float64)),
    "<=": (2, lambda c, a, b: np.less_equal(_as_array(c, a), _as_array(c, b)).astype(np.float64)),
    ">": (2, lambda c, a, b: np.greater(_as_array(c, a), _as_array(c, b)).astype(np.float64)),
    ">=": (2, lambda c, a, b: np.greater_equal(_as_array(c, a), _as_array(c, b)).astype(np.float64)),
    "&&": (2, lambda c, a, b: np.logical_and(_as_bool(c, a), _as_bool(c, b)).astype(np.float64)),
    "||": (2, lambda c, a, b: np.logical_or(_as_bool(c, a), _as_bool(c, b)).astype(np.float64)),
    "&": (2, lambda c, a, b: np.bitwise_and(_as_int(c, a), _as_int(c, b)).astype(np.float64)),
    "|": (2, lambda c, a, b: np.bitwise_or(_as_int(c, a), _as_int(c, b)).astype(np.float64)),
    "u-": (1, lambda c, a: _unary_arithmetic(c, a, np.negative)),
    "u+": (1, lambda c, a: _as_array(c, a)),
    "u!": (1, lambda c, a: np.logical_not(_as_bool(c, a)).astype(np.float64)),
    "u~": (1, lambda c, a: np.bitwise_not(_as_int(c, a)).astype(np.float64)),
}


_CONSTANTS: dict[str, float] = {"pi": _math.pi, "e": _math.e}


# ------------------------------------------------------------------ 作用域

#: 本地定义跟着场次走：``<场次>.ld`` -> ``<场次>.maths.json``。
MATH_SUFFIX = ".maths.json"


@dataclass
class Definition:
    """一条数学通道：一个名字、一条表达式、一个单位、一个作用域。"""

    name: str
    expr: str
    unit: str = ""
    scope: str = "local"          # local | global
    note: str = ""

    def as_dict(self) -> dict:
        out = {"name": self.name, "expr": self.expr, "unit": self.unit}
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, data: dict, scope: str = "local") -> "Definition":
        name = str(data.get("name", "")).strip()
        expr = str(data.get("expr", "")).strip()
        if not name:
            raise MathError("数学通道缺一个名字。给它起一个名字，例如 `滑移率`。")
        if not expr:
            raise MathError(f"数学通道 `{name}` 没有表达式。写一条式子再保存。")
        return cls(
            name=name,
            expr=expr,
            unit=str(data.get("unit", "") or "").strip(),
            scope=scope,
            note=str(data.get("note", "") or ""),
        )


def config_path(session_path: str | Path) -> Path:
    """``<场次>.ld`` / ``<场次>.csv`` -> ``<场次>.maths.json``。"""
    return Path(session_path).with_suffix(MATH_SUFFIX)


def global_path(root: str | Path | None = None) -> Path:
    """全局定义放在仓库里的 ``maths/global.json``。"""
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return base / "maths" / "global.json"


@dataclass
class MathSet:
    """一份作用域里的定义集合（或两者的合并结果）。"""

    definitions: list[Definition] = field(default_factory=list)
    #: 常量表：名字 -> 数值。i2 Pro 允许带单位的常量，这里只有数值。
    constants: dict[str, float] = field(default_factory=dict)
    origin: Path | None = None

    def by_name(self) -> dict[str, Definition]:
        return {d.name: d for d in self.definitions}

    def as_dict(self) -> dict:
        out: dict = {"definitions": [d.as_dict() for d in self.definitions]}
        if self.constants:
            out["constants"] = dict(self.constants)
        return out

    @classmethod
    def from_dict(cls, data: dict, scope: str = "local", origin: Path | None = None) -> "MathSet":
        raw = data.get("definitions") or []
        if not isinstance(raw, list):
            raise MathError("数学通道文件里的 `definitions` 应该是一个列表。")
        return cls(
            definitions=[Definition.from_dict(item, scope) for item in raw if isinstance(item, dict)],
            constants={
                str(k): float(v)
                for k, v in (data.get("constants") or {}).items()
                if _is_number(v)
            },
            origin=origin,
        )

    @classmethod
    def load(cls, path: str | Path, scope: str = "local") -> "MathSet":
        """读一份定义；文件不在就是空集合（不是错误）。"""
        path = Path(path)
        if not path.exists():
            return cls(origin=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MathError(
                f"{path.name} 读不出来（{type(exc).__name__}: {exc}）。"
                f"修好这个 JSON，或者直接删掉它重来。"
            ) from exc
        if not isinstance(data, dict):
            raise MathError(f"{path.name} 的顶层应该是一个 JSON 对象。")
        return cls.from_dict(data, scope=scope, origin=path)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.origin = path
        return path


def _is_number(value) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def load_local(session_path: str | Path) -> MathSet:
    return MathSet.load(config_path(session_path), scope="local")


def save_local(session_path: str | Path, maths: MathSet) -> Path:
    return maths.save(config_path(session_path))


def load_global(root: str | Path | None = None) -> MathSet:
    return MathSet.load(global_path(root), scope="global")


def save_global(maths: MathSet, root: str | Path | None = None) -> Path:
    return maths.save(global_path(root))


@dataclass
class Effective:
    """本地与全局合并之后的结果，外加"这个名字到底是谁的"。"""

    definitions: list[Definition]
    shadowed: list[str]           # 被本地盖住的全局名字

    def by_name(self) -> dict[str, Definition]:
        return {d.name: d for d in self.definitions}

    def scope_of(self, name: str) -> str | None:
        for definition in self.definitions:
            if definition.name == name:
                return definition.scope
        return None

    def as_dict(self) -> dict:
        return {
            "definitions": [d.as_dict() for d in self.definitions],
            "shadowed": list(self.shadowed),
        }


def load_effective(session_path: str | Path, root: str | Path | None = None) -> Effective:
    """全局 + 本地合并；同名时本地赢，赢的那个仍然能看出是本地。"""
    glob = load_global(root)
    local = load_local(session_path)
    local_names = {d.name for d in local.definitions}
    merged = [d for d in glob.definitions if d.name not in local_names] + list(local.definitions)
    shadowed = sorted(d.name for d in glob.definitions if d.name in local_names)
    return Effective(definitions=merged, shadowed=shadowed)


def function_catalogue() -> list[dict]:
    """给界面用的函数表：名字、参数个数、说明。"""
    return [
        {
            "name": name,
            "min_args": func.min_args,
            "max_args": func.max_args,
            "doc": func.doc,
        }
        for name, func in sorted(FUNCTIONS.items())
    ]


# -------------------------------------------------------------------- 求值


class DerivedCache:
    """按 (场次文件, 表达式, 源通道) 缓存算出来的列。

    文件指纹用 ``mtime_ns + size``：源数据要变，只可能是这个文件被重新导入／覆盖，
    而那时这两个值一定变。这样不用为了判断"要不要重算"把 34 万行读一遍。
    """

    def __init__(self, limit: int = 64):
        self.limit = max(1, limit)
        self._items: dict[tuple, np.ndarray] = {}
        self._order: list[tuple] = []

    @staticmethod
    def file_fingerprint(path: str | Path) -> tuple:
        try:
            stat = Path(path).stat()
        except OSError:
            return (str(path), 0, 0)
        return (str(path), stat.st_mtime_ns, stat.st_size)

    @staticmethod
    def key(
        session_path: str | Path,
        definition: Definition,
        sources: tuple[str, ...],
        dependencies: tuple[tuple[str, str], ...] = (),
    ) -> tuple:
        return (
            DerivedCache.file_fingerprint(session_path),
            definition.name,
            definition.expr,
            definition.unit,
            sources,
            # 被引用定义的内容（名字 + 表达式，传递闭包）：光有名字的话，
            # `乙 = 甲 + 1` 在甲改掉之后会一直命中旧列。
            dependencies,
        )

    def get(self, key: tuple) -> np.ndarray | None:
        value = self._items.get(key)
        if value is not None:
            self._touch(key)
        return value

    def put(self, key: tuple, value: np.ndarray) -> None:
        self._items[key] = value
        self._touch(key)
        while len(self._order) > self.limit:
            old = self._order.pop(0)
            self._items.pop(old, None)

    def _touch(self, key: tuple) -> None:
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def clear(self) -> None:
        self._items.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._items)


def _session_names(session) -> tuple[str, ...]:
    """本场次有哪些通道名（.ld 与 CSV 两种会话都有 ``channels``）。"""
    try:
        channels = session.channels
    except AttributeError:              # 只实现了 has/values 的精简会话对象
        return ()
    if not channels:
        return ()
    return tuple(channel.name for channel in channels)


def known_names(session, definitions: Iterable = ()) -> tuple[str, ...]:
    """编译一条表达式时"算得上通道名"的全部名字（定义也可以是纯名字串）。"""
    extra = []
    for item in definitions:
        try:
            extra.append(item.name)
        except AttributeError:
            extra.append(item)
    return (*_session_names(session), *extra)


def _session_series(session, name: str, size: int) -> np.ndarray:
    """取一个源通道在主时间基上的序列。"""
    if not session.has(name):
        raise MathError(_unknown_channel_message(name, _session_names(session)))
    values = derive.hold_to_master(session, name)
    if values.size >= size:
        return values[:size].astype(np.float64)
    pad = values[-1] if values.size else np.nan
    return np.concatenate([values, np.full(size - values.size, pad)]).astype(np.float64)


def resolve_all(
    session,
    definitions: list[Definition],
    cache: DerivedCache | None = None,
    session_path: str | Path | None = None,
    preset: dict[str, np.ndarray] | None = None,
) -> dict[str, np.ndarray]:
    """按依赖顺序求值，返回 ``名字 -> 列``。本地同名覆盖已经在调用前做完。

    自己引用自己（直接或绕一圈）会报错，而不是递归到栈溢出。
    """
    time = timebase.axis(session)
    rate = timebase.rate_of(session)
    ctx = Context(time=time, rate=rate)
    if session_path is not None:
        path: object = session_path
    else:
        path = session.path if hasattr(session, "path") else ""
    pending = {d.name: d for d in definitions}
    known: dict[str, np.ndarray] = dict(preset or {})
    done: dict[str, np.ndarray] = {}
    plans: dict[str, Plan] = {}
    visiting: list[str] = []
    names = known_names(session, definitions)

    def build(name: str) -> Plan:
        if name in plans:
            return plans[name]
        definition = pending[name]
        plan = compile_expr(definition.expr, known=names)
        plans[name] = plan
        return plan

    def dependency_fingerprint(sources: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
        """被引用定义的内容指纹（传递闭包）。

        缓存键里只放源通道的**名字**是不够的：``乙 = 甲 + 1`` 的键在 ``甲`` 的表达式
        被改掉之后必须跟着变，否则乙会一直用旧列算。所以把每一条被引用到的定义
        （连同它引用的那些）的名字 + 表达式一起放进键里。
        """
        seen: dict[str, str] = {}
        stack = [name for name in sources if name in pending]
        while stack:
            name = stack.pop()
            if name in seen:
                continue
            seen[name] = pending[name].expr
            stack.extend(channel for channel in build(name).channels if channel in pending)
        return tuple(sorted(seen.items()))

    def value_of(name: str, sources: tuple[str, ...]) -> np.ndarray:
        if name in pending:
            return resolve_definition(name)
        if name in known:
            return _pad_to(known[name], time.size)
        return _session_series(session, name, time.size)

    def resolve_definition(name: str) -> np.ndarray:
        if name in done:
            return done[name]
        if name in visiting:
            chain = " -> ".join([*visiting, name])
            raise MathError(
                f"数学通道绕成一个圈了：{chain}。"
                f"把其中一条改成不引用另一条，或者删掉一条。"
            )
        definition = pending.get(name)
        if definition is None:  # pragma: no cover - 只由内部调用
            raise MathError(f"未知数学通道 `{name}`。")
        plan = build(name)
        sources = tuple(sorted(plan.channels))
        key = (
            DerivedCache.key(path, definition, sources, dependency_fingerprint(sources))
            if cache is not None else None
        )
        if cache is not None and key is not None:
            hit = cache.get(key)
            if hit is not None and hit.size == time.size:
                done[name] = hit
                return hit
        visiting.append(name)
        try:
            computed = plan.eval(ctx, lambda channel: value_of(channel, sources))
        finally:
            visiting.pop()
        computed = np.asarray(computed, dtype=np.float64)
        if computed.size != time.size:
            computed = _pad_to(computed, time.size)
        done[name] = computed
        if cache is not None and key is not None:
            cache.put(key, computed)
        return computed

    for definition in definitions:
        if definition.name not in done:
            resolve_definition(definition.name)
    return done


def _pad_to(values: np.ndarray, size: int) -> np.ndarray:
    if values.size == size:
        return values
    if values.size > size:
        return values[:size]
    pad = values[-1] if values.size else np.nan
    return np.concatenate([values, np.full(size - values.size, pad)])


def resolve_available(
    session,
    definitions: list[Definition],
    cache: DerivedCache | None = None,
    session_path: str | Path | None = None,
) -> tuple[dict[str, np.ndarray], list[dict]]:
    """尽量都算出来：一条坏了不影响其它条，坏的那条连原因一起报上来。

    顺序上先做一轮一轮的松弛——能被解出来的先解出来，剩下的一起求值以拿到
    真正的错因（这样"绕成环"不会被误报成"本场次没有这个通道"）。
    """
    values: dict[str, np.ndarray] = {}
    remaining = list(definitions)
    while remaining:
        progressed = False
        for definition in list(remaining):
            try:
                got = resolve_all(
                    session,
                    [definition],
                    cache=cache,
                    session_path=session_path,
                    preset=values,
                )
            except MathError:
                continue          # 可能是依赖还没算出来，下一轮再试
            values.update(got)
            remaining.remove(definition)
            progressed = True
        if not progressed:
            break
    errors: list[dict] = []
    if remaining:
        try:
            resolve_all(session, remaining, cache=cache, session_path=session_path, preset=values)
            message = "这些数学通道求值失败，原因不明。"   # pragma: no cover - 理论上到不了
        except MathError as exc:
            message = str(exc)
        errors = [
            {"name": d.name, "expr": d.expr, "scope": d.scope, "error": message}
            for d in remaining
        ]
    return values, errors


def apply_to_session(
    session,
    root: str | Path | None = None,
    cache: DerivedCache | None = None,
) -> tuple[list[str], list[dict]]:
    """把这个场次生效的数学通道算出来挂上去；返回 ``(新增的名字, 错误)``。

    服务端、``render``、``snapshot``、``convert`` 都走这一个入口——否则"网页上
    有这条派生通道、导出的快照里没有"这种不一致迟早会出现。
    """
    try:
        effective = load_effective(session.path, root)
    except MathError as exc:
        return [], [{"name": "", "expr": "", "error": str(exc)}]
    values, errors = resolve_available(session, effective.definitions, cache, session.path)
    return attach(session, values, effective.definitions), errors


def evaluate(
    text: str,
    session,
    extra: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """在 ``session`` 上直接算一条式子（不求值依赖的定义）。调试与预览用。"""
    time = timebase.axis(session)
    rate = timebase.rate_of(session)
    ctx = Context(time=time, rate=rate)
    plan = compile_expr(text, known=known_names(session, tuple(extra or {})))
    extra = extra or {}

    def resolve(name: str) -> np.ndarray:
        if name in extra:
            return _pad_to(np.asarray(extra[name], dtype=np.float64), time.size)
        return _session_series(session, name, time.size)

    return plan.eval(ctx, resolve)


def attach(session, resolved: dict[str, np.ndarray], definitions: list[Definition]) -> list[str]:
    """把算出来的列挂到场次上，让下游（图表、切圈、报表）当原生通道用。

    ``.ld`` 会话把列放进 ``LogFile.derived``，CSV 会话放进 ``CsvSession.columns``
    ——两条来源共用同一套下游代码，不需要各自加分支。**放哪儿由 ``channels.py``
    问会话自己**（``derived_target``），这里不再用 ``hasattr`` 嗅探会话类型。
    """
    from . import channels as channelsmod
    from . import ld as ldmod

    detach(session)
    meta = {d.name: d for d in definitions}
    added: list[str] = []
    existing = {ch.name for ch in session.channels}
    for name, values in resolved.items():
        definition = meta.get(name)
        # 列、名字、单位一起交给 channels：界面角标与下游用的就是这三样
        channelsmod.attach(
            session, name, values, (definition.unit if definition else "") or ""
        )
        if name in existing:
            continue
        session.channels.append(
            ldmod.Channel(
                name=name,
                short_name=name[:8],
                unit=(definition.unit if definition else "") or "",
                sample_rate=float(session.sample_rate),
                sample_count=int(values.size),
                data_offset=0,
                data_type=0,
                bytes_per_sample=0,
                multiplier=1,
                divider=1,
                decimals=3,
                shift=0,
                channel_id=-1,
                index=len(session.channels),
            )
        )
        added.append(name)
    return added


def detach(session) -> None:
    """撤掉上一次挂上去的数学通道。

    删掉一条定义之后不能留下"幽灵通道"：那个名字还在通道列表里、点开却取不到值，
    是比"删了没反应"更难查的毛病。
    """
    from . import channels as channelsmod

    names = set(channelsmod.names(session))
    if not names:
        return
    channelsmod.clear(session)
    # 只删我们自己加进去的：原生通道带着文件里的 channel_id，派生通道是 -1
    session.channels = [
        ch for ch in session.channels if not (ch.name in names and ch.channel_id < 0)
    ]
