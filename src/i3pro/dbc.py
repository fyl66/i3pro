"""DBC 文件：CAN 报文与信号的定义，以及"一帧字节 -> 物理量"的换算（ticket #37）。

这是 CAN 那条线的最底层：**没有框架依赖的纯函数**——读一段文本拿到库，拿一个库
和一段字节拿到若干物理量。读 CSV、拼场次、零阶保持都在 :mod:`i3pro.canlog` 里，
本模块不认识"日志"这个词。

为什么要手写而不是引 ``cantools``：运行期只允许 numpy / pandas / pyarrow（规则 4），
而 ``cantools`` 会拉进 bitstruct 等一串依赖。本模块只实现日志里真实出现过的语法子集，
``cantools`` 在测试里当**独立裁判**逐信号对拍（规则 4 豁免测试工具），和"解析正确性
只认 MoTeC 自己导出的 CSV"是同一个套路。

三处最容易写错、而且错了不会响的地方，都在这里钉死：

1. **字节序**。DBC 里 ``@0`` 是 Motorola / 大端，``@1`` 是 Intel / 小端，两种的起点和
   走法都不一样：

   * 一个字节内部，DBC 的位号 **0 是最低位、7 是最高位**（锯齿编号）。这一条写反了
     （把 0 当最高位）就是"每个值都差一次按位反转"，不会报错，只会静默错。
   * ``@0`` 给的起始位是**最高位**，之后位号**递减**，减到字节边界（``%8==0``）再跳到
     下一个字节的位 7；``@1`` 给的起始位是**最低位**，之后位号递增。

   实测的两份 DBC 里 46 条信号全是 ``@0``，所以"只写小端"的解码器在这台车上**全部
   是错的**；两种都有真用例，并且用 ``cantools`` 逐信号对拍。
2. **用日志里实际的载荷长度**，不是 DBC 里写的 DLC。实测 ``0x66D``：DBC 写 4，
   日志里发的是 8 字节——按 DBC 的长度截，多出来的部分就被当成"下一帧"。
3. **多路复用**。``M`` / ``m<n>`` 标记的信号在同一个报文里分时出现：一条报文里
   byte0 是帧类型（选择子），后 6 字节按帧类型分别是三轴加速度或三轴角速度。
   按普通信号解会把别路的字节当成自己的值——所以 :func:`decode` 先读选择子，
   只解**这一帧该有的**那一路，别的路这一帧干脆不出现（不是 0，也不是上一帧的值）。
   实测 `i2pro_data/dbc/IMU.dbc` 的 0x780–0x783 就是这么发的（ticket #43）。

``BO_`` 的 ID 最高位（``0x80000000``）表示扩展帧，:class:`Message` 把它拆成
``frame_id`` + ``extended`` 两个字段——这样"标准帧 0x123"和"扩展帧 0x123"是两条不同的
报文，查表时不会互相盖住（实测那份 dashboard DBC 的 63 条报文全是扩展 ID ``0x9D22xxxx``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "DbcError", "Database", "Message", "Signal", "branch_of", "decode", "merge",
    "multiplexer_of", "parse",
    "signal_value",
]

#: Vector 的伪报文：一份"没有用到的信号"的容器，不是一个真实报文。盲解会造出
#: 几条永远为空的通道（实测 5 条），所以解析时就丢掉。
INDEPENDENT_SIG_MSG = 0xC0000000

#: 扩展帧那一位置位（DBC 的 ``BO_`` ID 用最高位表示 29 位扩展帧）。
EXTENDED_FLAG = 0x80000000

#: 多路复用标记：``M`` 是选择子，``m3`` 表示"选择子等于 3 时才有这个信号"。
_MUX_RE = re.compile(r"^(M|m\d+)$")

_BO_RE = re.compile(
    r"^BO_\s+(?P<id>\d+)\s+(?P<name>\S+)\s*:\s*(?P<len>\d+)\s*(?P<sender>\S*)"
)
#: 信号行。接收者列表允许为空（有些 DBC 写 ``Vector__XXX``，有些干脆不写）。
_SG_RE = re.compile(
    r"^SG_\s+(?P<name>\S+)\s*(?:(?P<mux>M|m\d+)\s*)?:\s*"
    r"(?P<start>\d+)\|(?P<length>\d+)@(?P<order>[01])(?P<sign>[+-])\s*"
    r"\(\s*(?P<factor>[^,()]+?)\s*,\s*(?P<offset>[^,()]+?)\s*\)\s*"
    r"\[\s*(?P<min>[^|\]]*?)\s*\|\s*(?P<max>[^\]]*?)\s*\]\s*"
    r'"(?P<unit>[^"]*)"\s*(?P<receivers>.*)$'
)
_VAL_RE = re.compile(r'^VAL_\s+(?P<id>\d+)\s+(?P<signal>\S+)\s+(?P<body>.*)$')
_VAL_PAIR_RE = re.compile(r'(-?\d+)\s+"((?:[^"\\]|\\.)*)"')


class DbcError(ValueError):
    """DBC 读不动，或者这条报文解不了。消息按项目规则说清"下一步做什么"。"""


@dataclass(frozen=True)
class Signal:
    """一条信号在报文里的位置与换算。"""

    name: str
    start_bit: int
    length: int
    #: ``"big"`` = DBC 的 ``@0``（Motorola），``"little"`` = ``@1``（Intel）。
    byte_order: str
    signed: bool
    factor: float
    offset: float
    unit: str
    minimum: float | None
    maximum: float | None
    #: 值表（``VAL_``）：整数 -> 人话。没有就是空字典。
    choices: dict = field(default_factory=dict)
    #: ``"M"``（选择子）/ ``"m3"``（受 3 号选择子管辖）/ ``None``（普通信号）。
    multiplexer: str | None = None

    @property
    def physical_range(self) -> tuple[float, float] | None:
        if self.minimum is None or self.maximum is None:
            return None
        return (self.minimum, self.maximum)


@dataclass(frozen=True)
class Message:
    """一条报文（DBC 的 ``BO_``）。"""

    name: str
    frame_id: int
    extended: bool
    length: int
    sender: str
    signals: tuple[Signal, ...]

    @property
    def multiplexed(self) -> bool:
        """有没有信号参与多路复用（含"有选择子但没人用"的半成品）。"""
        return any(signal.multiplexer for signal in self.signals)

    def signal(self, name: str) -> Signal:
        for signal in self.signals:
            if signal.name == name:
                return signal
        raise KeyError(f"{self.name} 里没有信号 {name!r}")


@dataclass(frozen=True)
class Database:
    """一份 DBC。报文按 ``(extended, frame_id)`` 分开存，标准帧与扩展帧不会互相盖住。"""

    messages: dict
    #: 被丢掉的东西，用来在导入报告里说实话（现在是伪报文）。
    skipped: tuple[str, ...] = ()
    source: str = ""

    def find(self, frame_id: int, extended: bool = False) -> Message | None:
        """按日志里的帧格式查报文；格式没写对时也认另一档（日志里只有一种）。"""
        found = self.messages.get((bool(extended), frame_id))
        if found is not None:
            return found
        return self.messages.get((not bool(extended), frame_id))

    def covers(self, frame_id: int, extended: bool = False) -> bool:
        return self.find(frame_id, extended) is not None

    @property
    def messages_only(self) -> list[Message]:
        return list(self.messages.values())

    @property
    def signal_count(self) -> int:
        return sum(len(message.signals) for message in self.messages.values())


def merge(
    sources: list[tuple[str, "Database"]],
    coverage: dict[str, int] | None = None,
) -> tuple["Database", dict, list[dict]]:
    """多份 DBC 取**并集**：一份日志配好几份 DBC 时，每一条报文都要能解。

    为什么不是"挑覆盖最多的一份"：实测这台车把 CAN 布局拆成了 13 份小 DBC
    （仪表一份、传感器一份、每个 ECU 一份），任何单独一份都只覆盖 0–22% 的帧。
    只挑一份会让用户后来补上的 DBC **一条都不参与解码**，而且界面上看不出少了什么
    ——那正是这次要修的 bug。

    同一条 ID 被两份 DBC **定义成不同样子**时不许静默合并：按 ``coverage``
    （每份 DBC 各自覆盖的帧数）选一份，并记进冲突列表；两边一样时按文件名定序，
    保证"同样的输入永远给同样的结果"。调用方要把冲突写进导入报告，用户可以用侧车
    的 ``dbc`` 字段指名用哪一份。

    返回 ``(并集, 每条报文来自哪份文件, 冲突列表)``。
    """
    coverage = coverage or {}
    merged: dict[tuple[bool, int], Message] = {}
    origin: dict[tuple[bool, int], str] = {}
    conflicts: list[dict] = []
    for name, database in sources:
        for key, message in database.messages.items():
            existing = merged.get(key)
            if existing is None:
                merged[key] = message
                origin[key] = name
                continue
            if existing.signals == message.signals and existing.length == message.length:
                # 两份文件写了同一条报文、定义逐字相同：不是冲突，留一份就够。
                continue
            # 定义不一样：谁都能解出数，但解出来的是两回事，必须选一个。
            first = origin[key]
            score_first, score_new = coverage.get(first, 0), coverage.get(name, 0)
            keep_first = score_first > score_new or (
                score_first == score_new and first <= name
            )
            winner, loser = (first, name) if keep_first else (name, first)
            if winner != first:
                merged[key] = message
                origin[key] = winner
            kept = merged[key]
            conflicts.append({
                "id": f"0x{key[1]:X}",
                "extended": key[0],
                "chosen": winner,
                "chosen_message": kept.name,
                "chosen_signals": len(kept.signals),
                "rejected": loser,
                "rejected_message": message.name if loser == name else existing.name,
                "rejected_signals": len(message.signals) if loser == name else len(existing.signals),
                "reason": f"两份 DBC 对同一条 ID 给了不同定义，用了覆盖帧数更多的"
                          f"{winner}；要改用 {loser} 就把侧车的 dbc 写成它的文件名。",
            })
    skipped = tuple(
        f"{name}：{text}" for name, database in sources for text in database.skipped
    )
    return Database(messages=merged, skipped=skipped, source="+".join(
        name for name, _database in sources
    )), origin, conflicts


def _number(text: str, what: str, line: str) -> float:
    try:
        return float(text.strip().rstrip(","))
    except ValueError:
        raise DbcError(
            f"DBC 里 {what} 不是一个数：{text.strip()!r}（这一行是 {line.strip()!r}）。"
            "用 CANdb++ 或 cantools 重新导出这份 DBC。"
        ) from None


def _message_frame_id(raw_id: int) -> tuple[int, bool]:
    """DBC 的 ``BO_`` ID -> （29 位以内的 ID，是不是扩展帧）。"""
    if raw_id & EXTENDED_FLAG:
        return raw_id & 0x1FFFFFFF, True
    return raw_id, False


def parse(text: str, source: str = "") -> Database:
    """把一份 DBC 文本读成 :class:`Database`。

    只认日志里真实出现过的语法：``BO_`` / ``SG_`` / ``VAL_``。``CM_``（注释）、``BA_``
    （属性）、``NS_`` 那一块（缩进的符号清单）一律跳过——它们不影响解码。
    """
    messages: dict[tuple[bool, int], Message] = {}
    skipped: list[str] = []
    order: list[tuple[bool, int]] = []
    current: Message | None = None
    signals: list[Signal] = []
    pending_val: list[tuple[tuple[bool, int], str, dict]] = []

    def flush() -> None:
        nonlocal current, signals
        if current is not None:
            final = Message(
                name=current.name, frame_id=current.frame_id, extended=current.extended,
                length=current.length, sender=current.sender, signals=tuple(signals),
            )
            key = (final.extended, final.frame_id)
            if key in messages:                       # 后一份定义覆盖前一份
                order.remove(key)
            messages[key] = final
            order.append(key)
        current, signals = None, []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 按**整个词**分派，不能按前缀：`BO_TX_BU_` / `SG_MUL_VAL_` / `VAL_TABLE_`
        # 都以 `BO_` / `SG_` / `VAL_` 开头，前缀匹配会把它们当成报文/信号/值表。
        keyword = line.split(None, 1)[0]
        if keyword == "BO_":
            match = _BO_RE.match(line)
            if not match:
                raise DbcError(
                    f"读不懂这一行报文定义：{line!r}。"
                    "DBC 里 BO_ 的写法是 `BO_ <id> <名字>: <长度> <发送者>`，"
                    "用 CANdb++ 或 cantools 重新导出。"
                )
            raw_id = int(match.group("id"))
            if raw_id == INDEPENDENT_SIG_MSG:
                # Vector 的伪报文：里面是"没被用到的信号"，不是真实报文。
                skipped.append(f"{match.group('name')}（0x{raw_id:08X}，Vector 伪报文）")
                # 也要把"当前报文"清掉：伪报文后面跟着它自己的 SG_ 行，不清的话
                # 那些信号会挂到上一个真实报文上（实测会多出 5 条永远为空的通道）。
                flush()
                continue
            flush()
            frame_id, extended = _message_frame_id(raw_id)
            current = Message(
                name=match.group("name"), frame_id=frame_id, extended=extended,
                length=int(match.group("len")), sender=match.group("sender"), signals=(),
            )
            continue
        if keyword == "SG_" and current is not None:
            signals.append(_parse_signal(line))
            continue
        if keyword == "VAL_":
            match = _VAL_RE.match(line)
            if match:
                choices = {
                    int(number): _unescape(meaning)
                    for number, meaning in _VAL_PAIR_RE.findall(match.group("body"))
                }
                if choices:
                    # 值表带自己的报文 ID：DBC 里 VAL_ 出现在报文之后，可能已经翻到
                    # 下一条报文了，所以不能拿"当前报文"当归属。
                    frame_id, extended = _message_frame_id(int(match.group("id")))
                    pending_val.append(
                        ((extended, frame_id), match.group("signal"), choices)
                    )
            continue
    flush()

    if not messages:
        raise DbcError(
            "这份 DBC 里一条报文定义（BO_）都没有。"
            "确认选的是 DBC 文件而不是别的文本；用 CANdb++ 或 cantools 重新导出。"
        )

    # 值表要在所有报文都建好之后再加：DBC 的 VAL_ 行出现在报文之后，而 dataclass 是
    # 冻结的，所以这里重建那几条带的信号。
    for key, signal_name, choices in pending_val:
        message = messages.get(key)
        if message is None:
            continue
        rebuilt = []
        for signal in message.signals:
            if signal.name == signal_name and not signal.choices:
                rebuilt.append(Signal(**{**_as_kwargs(signal), "choices": choices}))
            else:
                rebuilt.append(signal)
        messages[key] = Message(
            name=message.name, frame_id=message.frame_id, extended=message.extended,
            length=message.length, sender=message.sender, signals=tuple(rebuilt),
        )

    ordered = {key: messages[key] for key in order}
    return Database(messages=ordered, skipped=tuple(skipped), source=source)


def _as_kwargs(signal: Signal) -> dict:
    return {
        "name": signal.name, "start_bit": signal.start_bit, "length": signal.length,
        "byte_order": signal.byte_order, "signed": signal.signed,
        "factor": signal.factor, "offset": signal.offset, "unit": signal.unit,
        "minimum": signal.minimum, "maximum": signal.maximum,
        "choices": signal.choices, "multiplexer": signal.multiplexer,
    }


def _unescape(text: str) -> str:
    return text.replace('\\"', '"').replace("\\\\", "\\")


def _parse_signal(line: str) -> Signal:
    match = _SG_RE.match(line)
    if not match:
        raise DbcError(
            f"读不懂这条信号定义：{line!r}。"
            "DBC 里 SG_ 的写法是 "
            "`SG_ <名字> [M|m<n>] : <起始位>|<长度>@<0|1><+|-> (factor,offset) "
            "[min|max] \"单位\" <接收者>`；用 CANdb++ 或 cantools 重新导出。"
        )
    multiplexer = match.group("mux")
    if multiplexer is not None and not _MUX_RE.match(multiplexer):
        multiplexer = None
    minimum = match.group("min").strip()
    maximum = match.group("max").strip()
    return Signal(
        name=match.group("name"),
        start_bit=int(match.group("start")),
        length=int(match.group("length")),
        byte_order="little" if match.group("order") == "1" else "big",
        signed=match.group("sign") == "-",
        factor=_number(match.group("factor"), "factor", line),
        offset=_number(match.group("offset"), "offset", line),
        unit=match.group("unit").strip(),
        minimum=None if minimum == "" else _number(minimum, "最小值", line),
        maximum=None if maximum == "" else _number(maximum, "最大值", line),
        multiplexer=multiplexer,
    )


def raw_value(signal: Signal, payload: bytes) -> int | None:
    """从一帧字节里取出这条信号的**原始整数**；字节不够长返回 ``None``。

    ``payload`` 是日志里那一帧的实际字节，长度可能与 DBC 写的 DLC 不同——以日志为准。
    """
    need_bits = signal.length
    if need_bits <= 0:
        return 0
    if signal.byte_order == "little":
        if signal.start_bit + need_bits > len(payload) * 8:
            return None
        raw = 0
        for step in range(need_bits):
            position = signal.start_bit + step
            bit = (payload[position // 8] >> (position % 8)) & 1
            raw |= bit << step
    else:
        raw = 0
        position = signal.start_bit
        for _ in range(need_bits):
            index, offset = divmod(position, 8)
            if index >= len(payload):
                return None
            # 字节内位号 0 是最低位、7 是最高位；@0 从最高位往下走。
            raw = (raw << 1) | ((payload[index] >> offset) & 1)
            position = position + 15 if offset == 0 else position - 1
    if signal.signed:
        sign_bit = 1 << (need_bits - 1)
        if raw & sign_bit:
            raw -= 1 << need_bits
    return raw


def signal_value(signal: Signal, payload: bytes) -> float | None:
    """这条信号在这一帧里的物理量；字节不够长返回 ``None``。"""
    raw = raw_value(signal, payload)
    if raw is None:
        return None
    return raw * signal.factor + signal.offset


def branch_of(signal: Signal) -> int | None:
    """这条信号属于哪一路：``m3`` → ``3``；选择子 ``M`` 与普通信号 → ``None``。

    不叫 ``multiplexer_of``：``M`` 那条自己也是"多路复用"的一部分，但它说的是
    "**我**是选择子"，不是"我属于第几路"。
    """
    marker = signal.multiplexer or ""
    if not marker.startswith("m") or not marker[1:].isdigit():
        return None
    return int(marker[1:])


def multiplexer_of(message: Message) -> Signal | None:
    """这条报文的选择子（标 ``M`` 的那条）；没有多路复用就是 ``None``。"""
    for signal in message.signals:
        if signal.multiplexer == "M":
            return signal
    return None


def decode(message: Message, payload: bytes) -> dict[str, float]:
    """解这一帧里**该有的**信号，返回 ``{信号名: 物理量}``。

    多路复用（``M`` / ``m<n>``）按标准语义解：先读选择子那一条的**原始整数**，
    再只解"选择子等于这个分支"的信号——别的分支这一帧**不出现在结果里**
    （不是 0，也不是上一帧的值）。调用方拿它当"这一帧没有这条"，例如
    `canlog` 就只把这一路的帧保持到主时间基上。

    解不了就抛 :class:`DbcError`，交给调用方记进导入报告——**不返回 0 冒充成功**。

    **与 cantools 的一处故意不同**：选择子取到一个 DBC 里没定义的分支时，
    cantools 抛 ``DecodeError``，我们返回"只有选择子"的字典。理由是这里的调用方是
    导入管线——一条坏帧不该把**这条报文的所有分支**都拖成导不进来（实测车上会发
    DBC 里还没有的帧类型）。那几帧不属于任何一路，会在导入报告里单独计数。
    """
    selector: int | None = None
    selector_signal = multiplexer_of(message)
    if selector_signal is not None:
        raw = raw_value(selector_signal, payload)
        if raw is None:
            raise DbcError(
                f"报文 {message.name}（0x{message.frame_id:X}）的这一帧只有 "
                f"{len(payload)} 字节，读不出选择子 {selector_signal.name}"
                f"（起始位 {selector_signal.start_bit}、{selector_signal.length} 位）。"
                "核对 DBC 与日志是不是同一版。"
            )
        selector = int(raw)
    values: dict[str, float] = {}
    for signal in message.signals:
        branch = branch_of(signal)
        if branch is not None:
            if selector is None:
                raise DbcError(
                    f"报文 {message.name}（0x{message.frame_id:X}）的信号 "
                    f"{signal.name} 标了分支 {signal.multiplexer}，可这条报文里"
                    "没有选择子（M 标记）。这份 DBC 是坏的："
                    "下一步用 CANdb++ / cantools 重新导出，"
                    "或给选择子那一条补上 M 标记。"
                )
            if branch != selector:
                continue
        value = signal_value(signal, payload)
        if value is None:
            raise DbcError(
                f"报文 {message.name}（0x{message.frame_id:X}）的这一帧只有 "
                f"{len(payload)} 字节，放不下信号 {signal.name}"
                f"（起始位 {signal.start_bit}、{signal.length} 位）。"
                "核对 DBC 与日志是不是同一版；拿不准就把这一帧的字节与 DBC 一起发给作者。"
            )
        values[signal.name] = value
    return values
